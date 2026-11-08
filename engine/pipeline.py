#!/usr/bin/env python3
"""
Audio YtDl — ENGINE (toàn bộ phần xử lý).

App Android tải file này từ GitHub mỗi lần chạy (có bản dự phòng đóng gói trong APK), rồi chạy bằng
Python có sẵn trong app. Sửa file này + push là app dùng code mới, KHÔNG cần build lại APK.

Giao tiếp với app qua stdout, mỗi dòng 1 lệnh:
  @@LOG <text>          ghi nhật ký
  @@STATUS <text>       dòng trạng thái
  @@PROGRESS <0..1>     thanh tiến độ
  @@FILE <path>\t<name>\t<mime>[\t<đích>]   file kết quả -> app lưu rồi xoá.
                        đích: bỏ trống = thư mục lưu người dùng chọn; "uri:<content-uri>" = ghi đè file gốc
  @@ERROR <text>        lỗi (kết thúc)
  @@AAC <id>\t<pcm>\t<out>\t<rate>\t<ch>\t<bitrate>   nhờ app mã hoá PCM s16le -> m4a bằng MediaCodec;
                        app trả 1 dòng "AAC <id> ok|err <lý do>" vào stdin
Tham số: argv[1] = file JSON = tham số giao diện (web/index.html) gửi sang + đường dẫn app thêm vào.
  mode: "playlist" (mặc định) | "tag" (gắn ảnh bìa / thông tin cho file có sẵn)
        | "speedFile" (tăng tốc 1 file có sẵn)
        | "addAd" (lấy mẫu quảng cáo từ 1 khoảng thời gian của video YouTube)
        | "cutAds" (cắt mọi đoạn giống mẫu trong 1 file có sẵn)
"""
ENGINE_API = 3          # phải khớp Engine.ENGINE_API trong app
ENGINE_VERSION = "0.1.0"

import collections
import hashlib
import json
import math
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

P = json.load(open(sys.argv[1], encoding="utf-8"))
for _k, _d in (("from", 1), ("count", 0), ("group", 10), ("partStart", 1), ("chapPer", 0)):
    P[_k] = int(P.get(_k) or _d)
for _k, _d in (("trimStart", 0.0), ("trimEnd", 0.0), ("speed", 1.0), ("adMatch", 85.0)):
    P[_k] = float(P.get(_k) or _d)
P.setdefault("url", "")
sys.path.insert(0, P["ytdlp"])          # file yt-dlp (zipapp) do app quản lý / tự cập nhật
import yt_dlp                            # noqa: E402

# ---------------------------------------------------------------- giao tiếp
_out = threading.Lock()


def emit(kind, *parts):
    with _out:
        sys.stdout.write("@@" + kind + " " + "\t".join(str(x).replace("\n", " ") for x in parts) + "\n")
        sys.stdout.flush()


def log(s): emit("LOG", s)
def status(s): emit("STATUS", s)
def progress(f): emit("PROGRESS", "%.4f" % max(0.0, min(1.0, f)))


# Tải / tìm mẫu / mã hoá chạy chồng nhau -> gom trạng thái + tiến độ của từng bước vào 1 dòng.
_UI = {"dl": "", "ad": "", "enc": "", "p_dl": 0.0, "p_enc": 0.0}


def ui(**kw):
    _UI.update(kw)
    status(" · ".join(_UI[k] for k in ("dl", "ad", "enc") if _UI[k]))
    progress(0.35 * _UI["p_dl"] + 0.65 * _UI["p_enc"])


T_ALL = time.time()
def secs(t0): return time.time() - t0


# ---------------------------------------------------------------- huỷ: giết cả nhóm tiến trình
CANCEL = threading.Event()
try:
    os.setpgrp()                         # ffmpeg / aria2c con cùng nhóm -> giết 1 lần là hết
    OWN_PGRP = True
except Exception:
    OWN_PGRP = False


def quit_now(code):
    """Thoát ngay cả khi còn luồng tải / tìm mẫu chạy nền (giết luôn ffmpeg, aria2c con)."""
    sys.stdout.flush()
    if OWN_PGRP:
        os.killpg(os.getpgrp(), signal.SIGKILL)
    os._exit(code)


def _on_term(*_):
    CANCEL.set()
    try:
        os.killpg(os.getpgrp(), signal.SIGKILL)
    finally:
        os._exit(130)


signal.signal(signal.SIGTERM, _on_term)


class Cancelled(Exception):
    pass


def check_cancel():
    if CANCEL.is_set():
        raise Cancelled()


# ---------------------------------------------------------------- tiện ích
CORES = max(1, os.cpu_count() or 1)


def human(bps):
    if bps >= 1048576: return "%.1f MB" % (bps / 1048576)
    if bps >= 1024: return "%.0f KB" % (bps / 1024)
    return "%.0f B" % bps


def sanitize(s):
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]', " ", s or "")
    return re.sub(r"\s+", " ", s).strip() or "audio"


def q(path):                              # escape cho file danh sách của concat demuxer
    return path.replace("'", "'\\''")


def atempo(speed):
    parts = []
    while speed > 2.0: parts.append(2.0); speed /= 2.0
    while speed < 0.5: parts.append(0.5); speed /= 0.5
    parts.append(speed)
    return ",".join("atempo=%.6f" % x for x in parts)


# Việc nặng chạy nền (mã hoá, đo sóng) hạ ưu tiên CPU để lấy thông tin YouTube (QuickJS) + tải (aria2c) không bị chậm;
# CPU rảnh thì việc nặng vẫn dùng hết. Chỉ hạ được (tăng nice) với tiến trình / luồng của chính mình, không cần quyền.
LOW_NICE = 15


def lower(pid=0):
    """Hạ ưu tiên tiến trình pid; 0 = luồng đang chạy (Linux: nice tính theo từng luồng)."""
    try:
        os.setpriority(os.PRIO_PROCESS, pid or threading.get_native_id(), LOW_NICE)
    except Exception:  # noqa: BLE001
        pass


def ffmpeg(args, on_time=None, low=False):
    """Chạy ffmpeg. on_time(giây_đã_xuất) nếu cần tiến độ. low: ưu tiên CPU thấp."""
    cmd = [P["ffmpeg"], "-hide_banner", "-nostdin", "-y", "-loglevel", "error"]
    if on_time:
        cmd += args[:-1] + ["-progress", "pipe:1", "-nostats", args[-1]]
    else:
        cmd += args
    pr = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
    if low: lower(pr.pid)
    tail = []
    for line in pr.stdout:
        line = line.strip()
        if line.startswith("out_time_us="):
            if on_time:
                try: on_time(int(line[12:]) / 1e6)
                except ValueError: pass
        elif "=" not in line and line:
            tail = (tail + [line])[-6:]
    pr.wait()
    check_cancel()
    if pr.returncode != 0:
        raise RuntimeError("ffmpeg lỗi (%d): %s" % (pr.returncode, " | ".join(tail)[:400]))


# ---- mã hoá AAC bằng MediaCodec của máy (qua app): gửi @@AAC, chờ dòng trả lời trên stdin
_AAC = {"next": 0, "wait": {}, "reader": None}
_AAC_LOCK = threading.Lock()


def _aac_reader():
    for line in sys.stdin:
        p = line.rstrip("\n").split(" ", 2)
        if len(p) >= 2 and p[0] == "AAC":
            with _AAC_LOCK:
                w = _AAC["wait"].pop(p[1], None)
            if w:
                w[1].append(p[2] if len(p) > 2 else "")
                w[0].set()


def app_aac(pcm, out, rate=44100, ch=2, bitrate=128000):
    with _AAC_LOCK:
        if not _AAC["reader"]:
            _AAC["reader"] = threading.Thread(target=_aac_reader, daemon=True)
            _AAC["reader"].start()
        _AAC["next"] += 1
        rid = str(_AAC["next"])
        ev, box = threading.Event(), []
        _AAC["wait"][rid] = (ev, box)
    emit("AAC", rid, pcm, out, rate, ch, bitrate)
    while not ev.wait(0.5):
        check_cancel()
    if box[0] != "ok":
        raise RuntimeError("MediaCodec: " + box[0])


def encoder():
    """fast | twoloop (ffmpeg AAC) | mediacodec (bộ mã hoá của máy, cần APK có hỗ trợ)."""
    e = P.get("encoder") or ("fast" if P.get("fastAac", True) else "twoloop")
    if e == "mediacodec" and not P.get("mediacodec"):
        log("App chưa hỗ trợ MediaCodec (cần APK mới) → dùng ffmpeg AAC nhanh")
        e = "fast"
    return e if e in ("fast", "twoloop", "mediacodec") else "fast"


def probe(path):
    """(thời lượng giây, codec audio, có ảnh bìa) đọc từ ffmpeg -i."""
    r = subprocess.run([P["ffmpeg"], "-hide_banner", "-i", path], stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, text=True, errors="replace")
    m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", r.stdout)
    dur = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3]) if m else 0.0
    c = re.search(r"Audio: (\w+)", r.stdout)
    return dur, (c[1] if c else ""), " Video: " in r.stdout


# ---------------------------------------------------------------- yt-dlp
def P_get(k, d=None):
    v = P.get(k)
    return d if v in (None, "") else v


def base_opts():
    o = {
        "quiet": True, "no_warnings": True, "noprogress": True,
        "cachedir": P["cache"], "socket_timeout": 20, "retries": 10, "fragment_retries": 10,
    }
    if P.get("qjs") and os.path.exists(P["qjs"]):
        o["js_runtimes"] = {"quickjs": {"path": P["qjs"]}}
    if P.get("url", "").startswith("file://"):
        o["enable_file_urls"] = True     # chỉ dùng khi thử nghiệm
    return o


def is_bot(msg):
    return "not a bot" in msg or "Sign in to confirm" in msg


def short(e):
    lines = [x for x in str(e).splitlines() if x.strip()]
    return (lines[-1] if lines else repr(e))[:250]


def list_entries():
    t0 = time.time()
    status("Đọc danh sách video…")
    frm, cnt = P["from"], P["count"]
    items = "%d:%d" % (frm, frm + cnt - 1) if cnt > 0 else "%d:" % frm
    with yt_dlp.YoutubeDL({**base_opts(), "extract_flat": "in_playlist", "playlist_items": items,
                           "noplaylist": False, "ignoreerrors": True}) as y:
        info = y.extract_info(P["url"], download=False)
    if not info:
        raise RuntimeError("Không đọc được link")
    is_list = info.get("_type") == "playlist"
    raw = [e for e in (info.get("entries") or []) if e] if is_list else [info]
    entries = []
    for i, e in enumerate(raw):
        entries.append({
            "n": i, "index": frm + i, "id": e.get("id"),
            "url": e.get("url") if is_list else (e.get("webpage_url") or P["url"]),
            "title": e.get("title") or e.get("id") or "audio",
            "duration": float(e.get("duration") or 0),
        })
    meta = {
        "title": info.get("title") if is_list else "",
        "uploader": info.get("uploader") or info.get("channel") or "",
    }
    if not entries:
        raise RuntimeError("Không tìm thấy video nào. Kiểm tra lại link.")
    log("Playlist: %s — %d video (%.1fs)" % (meta["title"] or "(video lẻ)", len(entries), secs(t0)))
    return entries, meta


# ---- tải: lấy thông tin lần lượt (chậm rãi như YTDLnis, tránh bị coi là bot), truyền dữ liệu song song
EXTRACT_SEM = threading.Semaphore(1)
BOT = threading.Event()
# Chế độ nhanh = client visionos (không cần JS/QuickJS, ~3 lần nhanh hơn). Video nào lỗi thì riêng video đó dùng chế độ
# đầy đủ; chỉ khi lỗi liên tiếp FAST_MAX_FAILS video mới tắt chế độ nhanh cho tới hết lần chạy này (không khoá lâu dài).
FAST_MAX_FAILS = 3
FAST = {"fails": 0, "off": False}


def download_all(entries, wdir, on_done):
    """Tải song song; mỗi video xong (được / lỗi / bỏ qua) gọi on_done(entry, kết quả hoặc None) ngay."""
    total = len(entries)
    results = [None] * total
    expected = {}            # n -> số byte dự kiến
    done = set()
    lock = threading.Lock()
    t0 = time.time()
    first_req = [True]

    def files_of(idx):
        return [os.path.join(wdir, f) for f in os.listdir(wdir) if f.startswith("raw_%d." % idx)]

    def one(e):
        try:
            fetch(e)
        finally:
            on_done(e, results[e["n"]])

    def fetch(e):
        n, idx = e["n"], e["index"]
        tag = "[%d/%d]" % (n + 1, total)
        side = os.path.join(wdir, "raw_%d.json" % idx)
        if os.path.exists(side):
            try:
                r = json.load(open(side))
                if os.path.exists(r["path"]):
                    log("%s Đã có sẵn: %s" % (tag, e["title"]))
                    with lock: done.add(n)
                    results[n] = r
                    return
            except Exception:
                pass
        if BOT.is_set() or CANCEL.is_set():
            return
        opts = {**base_opts(), "format": "bestaudio[ext=m4a]/bestaudio/best", "noplaylist": True, "continuedl": True, "updatetime": False,
                "concurrent_fragment_downloads": 4, "writethumbnail": True,
                "outtmpl": {"default": os.path.join(wdir, "raw_%d.%%(ext)s" % idx),
                            "thumbnail": os.path.join(wdir, "thumb_%d.%%(ext)s" % idx)}}
        if P.get("aria2c"):
            # aria2c: 16 kết nối / file (mặc định của yt-dlp) -> vượt giới hạn tốc độ từng kết nối
            opts["external_downloader"] = {"default": P["aria2c"]}
            opts["external_downloader_args"] = {"aria2c": ["--console-log-level=error",
                                                           "--ca-certificate=" + P["cert"]]}
        t1 = time.time()
        fast = not FAST["off"] and not P["url"].startswith("file://")
        info = None
        while info is None:
            check_cancel()
            if BOT.is_set():
                return
            o = dict(opts)
            with EXTRACT_SEM:
                if not first_req[0]:
                    time.sleep(1 + random.random() * 2)   # nghỉ 1–3 s giữa các lần hỏi YouTube
                first_req[0] = False
                fast = fast and not FAST["off"]           # có thể vừa bị tắt trong lúc chờ lượt
                # nhanh: chỉ visionos; đầy đủ: client mặc định trừ visionos (vừa lỗi) -> bớt 1 lần hỏi
                o["extractor_args"] = {"youtube": {"player_client": ["visionos"] if fast else ["default", "-visionos"]}}
                t1 = time.time()                          # chỉ tính thời gian hỏi YouTube (không tính lúc chờ lượt)
                try:
                    with yt_dlp.YoutubeDL(o) as y:
                        info = y.extract_info(e["url"], download=False)
                    if fast: FAST["fails"] = 0
                except Exception as ex:   # noqa: BLE001
                    if fast:
                        fast = False
                        FAST["fails"] += 1
                        if FAST["fails"] >= FAST_MAX_FAILS and not FAST["off"]:
                            FAST["off"] = True
                            log("Chế độ nhanh lỗi %d video liền → dùng chế độ đầy đủ tới hết lần chạy này" % FAST_MAX_FAILS)
                        log("%s Lấy thông tin nhanh lỗi → video này dùng chế độ đầy đủ (%s)" % (tag, short(ex)))
                        continue
                    if is_bot(str(ex)):
                        if not BOT.is_set():
                            log("%s YouTube bắt xác minh bot → dừng gửi yêu cầu mới. Đổi mạng (wifi ↔ 4G) "
                                "hoặc chờ vài giờ rồi chạy lại." % tag)
                        BOT.set()
                        return
                    log("%s LỖI, bỏ qua: %s" % (tag, short(ex)))
                    return
            log("%s Lấy thông tin: %.1fs%s" % (tag, secs(t1), "" if fast else " (chế độ đầy đủ)"))
        fs = info.get("filesize") or info.get("filesize_approx")
        if not fs:
            for rd in info.get("requested_formats") or info.get("requested_downloads") or []:
                fs = rd.get("filesize") or rd.get("filesize_approx")
        expected[n] = fs or 0
        while True:
            try:
                with yt_dlp.YoutubeDL(o) as y:
                    info = y.process_ie_result(info, download=True)
                break
            except Exception as ex:  # noqa: BLE001
                if CANCEL.is_set(): return
                if not o.pop("external_downloader", None):
                    log("%s LỖI tải, bỏ qua: %s" % (tag, short(ex)))
                    return
                log("%s aria2c lỗi → trình tải thường (%s)" % (tag, short(ex)))
        rd = (info.get("requested_downloads") or [{}])[0]
        path = rd.get("filepath") or info.get("filepath")
        if not path or not os.path.exists(path):
            c = [f for f in files_of(idx) if not f.endswith((".part", ".aria2", ".json", ".ytdl"))]
            path = c[0] if c else None
        if not path:
            log("%s LỖI: không thấy file đã tải" % tag)
            return
        dur = float(info.get("duration") or 0) or probe(path)[0]
        r = {"path": path, "duration": dur}
        json.dump(r, open(side, "w"))
        results[n] = r
        with lock: done.add(n)
        log("%s Tải xong %.1fMB (%s) trong %.1fs: %s" % (
            tag, os.path.getsize(path) / 1048576, os.path.splitext(path)[1][1:], secs(t1), e["title"]))

    # theo dõi tốc độ bằng dung lượng file đang ghi (đúng cho cả aria2c lẫn trình tải thường)
    stop = threading.Event()

    def monitor():
        last_b, last_t = 0, time.time()
        while not stop.wait(0.7):
            b, part = 0, 0.0
            with lock:
                dn = set(done)
            for e in entries:
                n = e["n"]
                if n in dn:
                    continue
                s = sum(os.path.getsize(f) for f in files_of(e["index"])
                        if os.path.exists(f) and not f.endswith(".json"))
                b += s
                if expected.get(n):
                    part += min(1.0, s / expected[n])
            now = time.time()
            sp = max(0, b - last_b) / max(now - last_t, 0.1)
            last_b, last_t = b, now
            ui(dl="Tải %d/%d · %s/s" % (len(dn), total, human(sp)), p_dl=(len(dn) + part) / total)

    ui(dl="Tải %d video…" % total)
    th = threading.Thread(target=monitor, daemon=True)
    th.start()
    with ThreadPoolExecutor(max_workers=min(10, total)) as ex:
        list(ex.map(one, entries))
    stop.set()
    ok = [r for r in results if r]
    mb = sum(os.path.getsize(r["path"]) for r in ok) / 1048576
    log("Tải xong %d/%d video, %.0fMB trong %.1fs" % (len(ok), total, mb, secs(t0)))
    ui(dl="", p_dl=1.0)


# ---------------------------------------------------------------- mẫu quảng cáo: so sánh sóng
# Kênh truyện audio hay chèn cùng 1 đoạn thu sẵn (quảng cáo, câu quảng bá) nhiều lần. Người dùng lấy 1 mẫu
# (khoảng thời gian trong video YouTube) -> so đường năng lượng dB (mỗi 32 ms) của mẫu với từng video ->
# chỗ nào giống ≥ adMatch % thì cắt đúng bằng độ dài mẫu (khi ghép, -c copy, không mã hoá lại).
AHOP = 0.032


def envelope(path, ss=0.0, to=None, speed=1.0):
    """Mức năng lượng (dB) mỗi khung 32 ms của đoạn [ss, to] (giây). speed ≠ 1: co giãn trước khi đo."""
    rng = ["-ss", "%.3f" % ss] + (["-to", "%.3f" % to] if to else [])
    af = (atempo(speed) + ",") if abs(speed - 1) > 0.005 else ""
    pr = subprocess.Popen([P["ffmpeg"], "-v", "error", *rng, "-i", path, "-vn", "-af",
                           af + "aformat=channel_layouts=mono,aresample=8000,asetnsamples=n=256:p=0,astats=metadata=1:reset=1:"
                           "measure_perchannel=none:measure_overall=RMS_level,ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-",
                           "-f", "null", "-"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    lower(pr.pid)
    out = pr.communicate()[0]
    return [max(-70.0, float(x)) if x not in ("-inf", "inf", "nan") else -70.0
            for x in re.findall(r"RMS_level=(\S+)", out)]


def _corr(x, y):
    n = len(x)
    mx = sum(x) / n; my = sum(y) / n; sxy = sx = sy = 0.0
    for a, b in zip(x, y):
        a -= mx; b -= my; sxy += a * b; sx += a * a; sy += b * b
    return sxy / math.sqrt(sx * sy + 1e-9)


def _avg4(x):
    return [(x[i] + x[i + 1] + x[i + 2] + x[i + 3]) / 4 for i in range(0, len(x) - 3, 4)]


def find_matches(e, t, thr):
    """Các chỗ (khung đầu, khung cuối) trong e giống mẫu t: quét thô 128 ms, chỉnh ±6 khung, kiểm tra cả đoạn."""
    m = len(t)
    E, T = _avg4(e), _avg4(t)
    mc = len(T)
    if mc < 8 or len(E) < mc: return []
    score = [_corr(T, E[j:j + mc]) for j in range(len(E) - mc + 1)]
    cand = sorted((j for j, sc in enumerate(score) if sc > thr - 0.1), key=lambda j: -score[j])
    out = []
    for j in cand:
        if any(abs(j * 4 - a) < m for a, _ in out): continue      # đã có chỗ khớp tốt hơn đè lên
        best, f0 = max((_corr(t, e[f:f + m]), f) for f in range(max(0, j * 4 - 6), min(len(e) - m, j * 4 + 6) + 1))
        if best >= thr: out.append((f0, f0 + m))
    return sorted(out)


def ads_dir():
    """Thư viện mẫu do app chọn (Download/AudioYtDl/Mẫu quảng cáo, có .nomedia). Mỗi file audio = 1 mẫu."""
    d = P["ads"]
    os.makedirs(d, exist_ok=True)
    return d


def load_templates(speed=1.0):
    """Đường năng lượng các mẫu đã chọn (bỏ khoảng lặng 2 đầu). speed: tốc độ của file sẽ đem so."""
    temps = []
    for name in P.get("adIds") or []:
        f = os.path.join(ads_dir(), os.path.basename(str(name)))
        if not os.path.exists(f):
            log("Không thấy mẫu \"%s\" (đã xoá?) — bỏ qua" % name); continue
        ev = envelope(f, speed=speed)
        while ev and ev[0] < -62: ev.pop(0)
        while ev and ev[-1] < -62: ev.pop()
        if len(ev) * AHOP >= 2: temps.append(ev)
    return temps


def ad_cuts(env, temps, ss, thr):
    """Các khoảng cần cắt (giây, theo file gốc) của 1 file có đường năng lượng env bắt đầu tại giây ss."""
    cuts = sorted((ss + a * AHOP, ss + b * AHOP) for t in temps for a, b in find_matches(env, t, thr))
    merged = []
    for x, y in cuts:
        if merged and x < merged[-1][1]: merged[-1] = (merged[-1][0], max(y, merged[-1][1]))
        else: merged.append((x, y))
    return merged


def fmt_t(x):
    return "%d:%02d" % divmod(int(x), 60)


def keep_parts(r, a, end):
    """Các khoảng giữ lại trong [a, end] sau khi bỏ r["cuts"]."""
    out = []
    for x, y in r.get("cuts", []):
        if x > a + 0.05: out.append((a, min(x, end)))
        a = max(a, y)
    if end > a + 0.05: out.append((a, end))
    return [(x, y) for x, y in out if y - x > 0.05]


def concat_list(path, items):
    """File danh sách concat demuxer: items = [(file, đầu, cuối)]."""
    open(path, "w", encoding="utf-8").write("".join(
        "file '%s'\ninpoint %.3f\noutpoint %.3f\n" % (q(f), x, y) for f, x, y in items))


def fetch_audio(link, index, wdir):
    """Tải audio 1 video (link video, hoặc link playlist + số thứ tự) vào wdir. Giữ lại để lấy nhiều mẫu."""
    o = {**base_opts(), "format": "bestaudio[ext=m4a]/bestaudio/best", "continuedl": True, "updatetime": False,
         "outtmpl": {"default": os.path.join(wdir, "%(id)s.%(ext)s")}}
    if "list=" in link and index:
        o.update({"noplaylist": False, "playlist_items": str(index)})
    else:
        o["noplaylist"] = True
    if P.get("aria2c"):
        o["external_downloader"] = {"default": P["aria2c"]}
        o["external_downloader_args"] = {"aria2c": ["--console-log-level=error", "--ca-certificate=" + P["cert"]]}
    last = None
    for client in (["visionos"], None):          # visionos nhanh (không cần JS), lỗi thì chế độ đầy đủ
        oo = dict(o)
        if client: oo["extractor_args"] = {"youtube": {"player_client": client}}
        for dl in (True, False):                  # aria2c lỗi -> trình tải thường
            if not dl: oo.pop("external_downloader", None)
            try:
                with yt_dlp.YoutubeDL(oo) as y:
                    info = y.extract_info(link, download=True)
                if info.get("_type") == "playlist":
                    info = next(e for e in info["entries"] if e)
                rd = (info.get("requested_downloads") or [{}])[0]
                path = rd.get("filepath") or info.get("filepath")
                if path and os.path.exists(path): return path, info.get("title") or ""
            except Exception as ex:  # noqa: BLE001
                check_cancel()
                last = ex
                if is_bot(str(ex)): raise RuntimeError("YouTube bắt xác minh bot. Đổi mạng hoặc chờ vài giờ.")
    raise RuntimeError("Không tải được video: %s" % (short(last) if last else "?"))


def add_ad_mode():
    """Lấy mẫu quảng cáo = đoạn [start, end] của 1 video YouTube -> lưu vào thư viện mẫu (tên file = tên mẫu)."""
    name = sanitize(os.path.splitext(os.path.basename(str(P.get("file") or "")))[0])[:80] + ".m4a"
    ss, to = float(P.get("start") or 0), float(P.get("end") or 0)
    if to - ss < 2: raise RuntimeError("Đoạn mẫu phải dài ít nhất 2 giây (Đến > Từ)")
    wdir = os.path.join(P["work"], "sample")
    os.makedirs(wdir, exist_ok=True)
    status("Tải audio video chứa mẫu…")
    t0 = time.time()
    src, title = fetch_audio(P.get("link", ""), int(P.get("index") or 1), wdir)
    for f in os.listdir(wdir):                     # chỉ giữ video vừa dùng (lấy tiếp mẫu khác khỏi tải lại)
        if os.path.join(wdir, f) != src: os.remove(os.path.join(wdir, f))
    log("Audio: %s (%.1fs)" % (title, secs(t0)))
    dur = probe(src)[0]
    if dur and to > dur: raise RuntimeError("Video chỉ dài %s, đoạn mẫu vượt quá" % fmt_t(dur))
    out = os.path.join(ads_dir(), name)
    ffmpeg(["-ss", "%.3f" % ss, "-to", "%.3f" % to, "-i", src, "-vn", "-ac", "1", "-ar", "16000",
            "-c:a", "aac", "-b:a", "48k", out])
    if sum(1 for x in envelope(out) if x > -50) * AHOP < 1.5:
        os.remove(out)
        raise RuntimeError("Đoạn mẫu gần như im lặng, kiểm tra lại thời gian")
    log("Đã lưu mẫu \"%s\": %s–%s (%.1f giây) của %s" % (name, fmt_t(ss), fmt_t(to), to - ss, title))
    progress(1.0)


def cut_ads_mode():
    """Cắt mọi đoạn giống mẫu trong 1 file có sẵn (copy, không mã hoá lại; giữ ảnh bìa + thông tin)."""
    src = P["input"]
    temps = load_templates(float(P.get("fileSpeed") or 1) or 1.0)
    if not temps: raise RuntimeError("Chưa chọn mẫu nào")
    wdir = os.path.join(P["work"], "cut")
    shutil.rmtree(wdir, ignore_errors=True)
    os.makedirs(wdir, exist_ok=True)
    os.makedirs(P["out"], exist_ok=True)
    status("Tìm đoạn giống mẫu…")
    t0 = time.time()
    dur, _, pic = probe(src)
    r = {"cuts": ad_cuts(envelope(src), temps, 0.0, P["adMatch"] / 100)}
    check_cancel()
    if not r["cuts"]:
        raise RuntimeError("Không thấy đoạn nào giống mẫu (thử giảm Độ giống hoặc kiểm tra Tốc độ file)")
    log("Cắt %d đoạn (%.0fs): %s · tìm %.1fs" % (len(r["cuts"]), sum(y - x for x, y in r["cuts"]),
        ", ".join("%s–%s" % (fmt_t(x), fmt_t(y)) for x, y in r["cuts"]), secs(t0)))
    status("Cắt (không mã hoá)…")
    lst = os.path.join(wdir, "list.txt")
    concat_list(lst, [(src, x, y) for x, y in keep_parts(r, 0.0, dur)])
    joined = os.path.join(wdir, "joined.mka")
    ffmpeg(["-f", "concat", "-safe", "0", "-i", lst, "-map", "0:a", "-c", "copy", joined])
    name = P.get("name") or os.path.basename(src)
    ext = os.path.splitext(name)[1][1:].lower() or "m4a"
    out = os.path.join(P["out"], "cut." + ext)
    args = ["-i", joined, "-i", src, "-map", "0:a"] + (["-map", "1:v"] if pic else [])
    args += ["-map_metadata", "1", "-c", "copy"] + (["-disposition:v", "attached_pic"] if pic else [])
    args += ["-id3v2_version", "3"] if ext == "mp3" else []
    ffmpeg(args + [out])
    emit("FILE", out, name, MIME.get(ext, "audio/mpeg"),
         ("uri:" + P["inputUri"]) if P.get("overwrite") and P.get("inputUri") else "")
    progress(1.0)


# ---------------------------------------------------------------- xử lý
def keep_range(r):
    """Khoảng giữ lại (giây, theo file gốc) sau khi cắt đầu/cuối."""
    return P["trimStart"], r["duration"] - P["trimEnd"]


def keep_of(r):
    a, b = keep_range(r)
    return b - a


def to_jpg(src, out):
    """Ảnh bất kỳ -> JPEG rộng tối đa 800px (dùng lại nếu đã có)."""
    if not os.path.exists(out):
        ffmpeg(["-i", src, "-frames:v", "1", "-vf", "scale='min(800,iw)':-2", "-q:v", "3", out])
    return out


def make_cover(wdir, idx):
    c = P.get("cover")
    if c and os.path.exists(c):
        try:
            return to_jpg(c, os.path.join(wdir, "cover_custom.jpg"))
        except Exception as e:  # noqa: BLE001
            log("Không dùng được ảnh bìa riêng: %s" % short(e))
    src = [f for f in os.listdir(wdir) if f.startswith("thumb_%d." % idx) and not f.endswith(".part")]
    if not src: return None
    try:
        return to_jpg(os.path.join(wdir, src[0]), os.path.join(wdir, "cover_%d.jpg" % idx))
    except Exception as e:  # noqa: BLE001
        log("Không tạo được ảnh bìa: %s" % short(e))
        return None


def cover_args(cover):
    return ["-disposition:v", "attached_pic", "-metadata:s:v", "title=Album cover",
            "-metadata:s:v", "comment=Cover (front)"] if cover else []


def speed_up(src, dur, wdir, g, ngroups, speed=None):
    """1 file -> chia đều N đoạn (≈3 đoạn/nhân: CPU 1+3+4 nhân như Dimensity 9500s thì nhân lớn nhận thêm đoạn),
    CORES luồng song song: giải mã + atempo (ffmpeg, ưu tiên thấp) + mã hoá AAC (ffmpeg hoặc MediaCodec), nối lại bằng copy."""
    speed = speed or P["speed"]
    n = max(1, min(CORES * 3, math.ceil(dur / 40)))
    ln = dur / n
    total_out = dur / speed
    done = {}
    t0 = time.time()
    last = [0.0]
    flt = atempo(speed)
    coder = encoder()
    mc_bad = []                                  # MediaCodec lỗi 1 lần -> các đoạn sau dùng ffmpeg
    ui(enc="Tăng tốc %d/%d…" % (g + 1, ngroups))
    parts = [os.path.join(wdir, "sp_%d_%02d.m4a" % (g, i)) for i in range(n)]

    def job(i):
        def on_t(s):
            done[i] = s
            now = time.time()
            if now - last[0] > 0.4:
                last[0] = now
                d = sum(done.values())
                ui(enc="Tăng tốc %d/%d: %.0f%% · %.0fx realtime" % (g + 1, ngroups, 100 * d / total_out, d / max(secs(t0), 0.1)),
                   p_enc=(g + 0.9 * d / total_out) / ngroups)
        inp = ["-threads", "1", "-ss", "%.3f" % (i * ln), "-t", "%.3f" % ln, "-i", src, "-vn", "-af", flt]
        if coder == "mediacodec" and not mc_bad:
            pcm = parts[i][:-4] + ".pcm"
            try:
                ffmpeg(inp + ["-f", "s16le", "-ac", "2", "-ar", "44100", pcm], on_t, low=True)
                app_aac(pcm, parts[i])
                return
            except Cancelled:
                raise
            except Exception as e:  # noqa: BLE001
                if not mc_bad: log("MediaCodec lỗi → ffmpeg AAC nhanh (%s)" % short(e))
                mc_bad.append(1)
            finally:
                if os.path.exists(pcm): os.remove(pcm)
        ffmpeg(inp + ["-c:a", "aac", "-aac_coder", "twoloop" if coder == "twoloop" else "fast", "-b:a", "128k", parts[i]],
               on_t, low=True)

    with ThreadPoolExecutor(max_workers=min(n, CORES)) as ex:
        list(ex.map(job, range(n)))
    lst = os.path.join(wdir, "sp_%d.txt" % g)
    open(lst, "w", encoding="utf-8").write("".join("file '%s'\n" % q(x) for x in parts))
    out = os.path.join(wdir, "sped_%d.m4a" % g)
    ffmpeg(["-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", out])
    for x in parts: os.remove(x)
    name = {"fast": "ffmpeg AAC nhanh", "twoloop": "ffmpeg AAC chất lượng",
            "mediacodec": "MediaCodec %s" % P.get("mediacodec")}[coder] + (" (một phần ffmpeg)" if mc_bad else "")
    log("Tăng tốc ×%g %.0f phút audio: %.1fs (%.0fx realtime, %s, %d đoạn)" % (
        speed, total_out / 60, secs(t0), total_out / max(secs(t0), 0.1), name, n))
    return out


# ---------------------------------------------------------------- tên file + metadata hàng loạt
# Audiobook không có chuẩn riêng -> mượn tag nhạc theo quy ước (beets-audible, Audiobookshelf):
#   Tên sách -> album (ID3 TALB / MP4 ©alb)
#   Tác giả  -> album_artist (TPE2 / aART) và artist (TPE1 / ©ART)
#   Thể loại -> genre (TCON / ©gen)
# Mỗi phần: title = "{phan}. {gon}", track = phần/tổng để trình phát xếp đúng thứ tự.
# App Voice: 1 thư mục = 1 sách, file = chương (xếp theo tên file), tên sách = album của file đầu, tác giả = artist,
# tên chương = title.
# Vì vậy tên sách chỉ dùng biến chung của cả sách (giống nhau ở mọi phần) và mỗi sách lưu 1 thư mục con.
# Số phần bắt đầu từ partStart (vd 11 khi sách đã có phần 1–10).
DEFAULT_TPL = {
    "nameTpl": "{sach}_{phan}_v{tu}-{den}",       # tên file (không cần đuôi)
    "titleTpl": "{phan}. {gon}",                   # tên từng phần (TIT2) = tên chương trong app nghe
    "bookTpl": "{playlist}",                       # tên sách
    "authorTpl": "{kenh}",                         # tác giả
}


class _Keep(dict):
    def __missing__(self, k):            # biến không có thì giữ nguyên "{k}"
        return "{" + k + "}"


def short_titles(titles):
    """Bỏ phần lặp lại ở mọi video (tên truyện, "audio"…): "Truyện X - Tập 002 (Chương 11-20) audio" -> "Tập 002 (Chương 11-20)"."""
    def common(a, b):                      # phần chung ở đầu, cắt tại dấu cách
        p = os.path.commonprefix([a, b])
        if len(p) < min(len(a), len(b)):
            p = p[:p.rfind(" ") + 1]
            if a[len(p):len(p) + 1].isdigit():   # "… Tập 002": giữ chữ "Tập" đứng trước số
                p = p[:p.rstrip().rfind(" ") + 1]
        return p
    pre = collections.Counter(common(a, b) for a, b in zip(titles, titles[1:]))
    suf = collections.Counter(common(a[::-1], b[::-1])[::-1] for a, b in zip(titles, titles[1:]))
    pre = max((p for p in pre if len(p.strip()) >= 6), key=lambda p: (pre[p], len(p)), default="")
    suf = max((p for p in suf if len(p.strip()) >= 3), key=lambda p: (suf[p], len(p)), default="")
    out = []
    for t in titles:
        x = t.split(pre, 1)[1] if pre and pre in t else t
        x = x[:-len(suf)] if suf and x.endswith(suf) else x
        out.append(x.strip(" -–—|:·,") or t)
    return out


def render(tpl, vals):
    try:
        return tpl.format_map(_Keep(vals))
    except Exception:  # noqa: BLE001  (người dùng gõ sai ngoặc...)
        return tpl


def main():
    log("Engine %s · CPU %d nhân" % (ENGINE_VERSION, CORES))
    entries, meta = list_entries()
    check_cancel()
    key = hashlib.md5(P["url"].encode()).hexdigest()[:10]
    wdir = os.path.join(P["work"], key)
    os.makedirs(wdir, exist_ok=True)
    for d in os.listdir(P["work"]):           # dọn thư mục của lần chạy khác
        full = os.path.join(P["work"], d)
        if d != key and os.path.isdir(full): shutil.rmtree(full, ignore_errors=True)

    # Chạy chồng: tải (chờ mạng) ở luồng nền; video nào tải xong thì tìm đoạn giống mẫu ngay;
    # nhóm nào đủ video (theo thứ tự trong playlist) thì ghép + tăng tốc luôn trong lúc các video sau còn tải.
    # Tìm mẫu + tăng tốc chạy ưu tiên CPU thấp (lower) nên không làm chậm bước lấy thông tin / tải.
    temps = []
    if P.get("adIds"):
        try:
            temps = load_templates()
        except Exception as e:  # noqa: BLE001
            log("Bỏ qua cắt đoạn giống mẫu (lỗi đọc mẫu: %s)" % short(e))
    thr = P["adMatch"] / 100
    plan = [entries[i:i + P["group"]] for i in range(0, len(entries), P["group"])]
    ready, reported, got = {}, set(), [0]
    cond = threading.Condition()
    ad_pool = ThreadPoolExecutor(max_workers=max(1, CORES // 2))    # chừa nhân cho bước tăng tốc chạy cùng lúc
    ad = {"todo": 0, "done": 0, "cut": 0.0, "t": 0.0}

    def finish(n, r):
        with cond:
            ready[n] = r
            cond.notify_all()

    def detect(r):
        lower()                                   # luồng tìm mẫu (so sóng bằng Python) cũng ưu tiên thấp
        t0 = time.time()
        try:
            ss, to = keep_range(r)
            r["cuts"] = ad_cuts(envelope(r["path"], ss, to), temps, ss, thr)
            if r["cuts"]:
                log("[%d] Cắt %d đoạn giống mẫu: %s" % (r["entry"]["n"] + 1, len(r["cuts"]),
                    ", ".join(fmt_t(x) for x, _ in r["cuts"])))
            ad["cut"] += sum(y - x for x, y in r["cuts"])
        except Exception as e:  # noqa: BLE001
            if not CANCEL.is_set(): log("[%d] Bỏ qua tìm mẫu (lỗi: %s)" % (r["entry"]["n"] + 1, short(e)))
        finally:
            with cond:
                ad["done"] += 1; ad["t"] += secs(t0)
                ui(ad="Tìm mẫu %d/%d" % (ad["done"], ad["todo"]) if ad["done"] < ad["todo"] else "")
            finish(r["entry"]["n"], r)

    def on_done(e, r):
        reported.add(e["n"])
        if r:
            got[0] += 1
            r = dict(r, entry=e)
            if keep_of(r) <= 1:
                log("[%d] Bỏ qua: video dài %ds, cắt xong không còn gì" % (e["n"] + 1, r["duration"])); r = None
        if r and temps:
            with cond: ad["todo"] += 1
            ad_pool.submit(detect, r)
        else:
            finish(e["n"], r)

    dl_err = []

    def dl():
        try:
            download_all(entries, wdir, on_done)
        except BaseException as ex:  # noqa: BLE001
            dl_err.append(ex)
        finally:
            for e in entries:                 # phòng hờ: video chưa được báo -> coi như lỗi để không chờ mãi
                if e["n"] not in reported: finish(e["n"], None)

    th = threading.Thread(target=dl, daemon=True)
    th.start()

    start = max(0, P["partStart"])                    # đánh số phần bắt đầu từ
    last_part = len(plan) + start - 1
    width = max(2, len(str(last_part)))
    tpl = {k: P_get(k, d) for k, d in DEFAULT_TPL.items()}
    if P["chapPer"] > 0 and not P_get("titleTpl"):
        tpl["titleTpl"] = "{ch_tu}~{ch_den}"
    playlist = meta["title"] or entries[0]["title"]
    channel = meta["uploader"] or playlist
    genre = P_get("genre", "Audiobook")
    today = time.strftime("%Y-%m-%d")
    book_vals = {"kenh": channel, "playlist": playlist, "the_loai": genre, "ngay": today}
    author = render(tpl["authorTpl"], book_vals) or channel
    book_vals["tac_gia"] = author
    book = render(tpl["bookTpl"], book_vals) or playlist      # giống nhau ở mọi phần (app nghe lấy từ file đầu)
    folder = ("dir:" + sanitize(book)[:100]) if P.get("bookFolder", True) else ""
    gon = dict(zip((e["n"] for e in entries), short_titles([e["title"] for e in entries])))
    # số đếm theo thứ tự video (không đọc từ tên video): video "Từ video" bắt đầu ở số chapStart, mỗi video chapPer số.
    # chapStart trống = video 1 bắt đầu từ 1. Chữ đi kèm (Chương, Tập…) người dùng tự gõ trong mẫu.
    per = max(0, P["chapPer"])
    ch0 = int(P_get("chapStart", (P["from"] - 1) * per + 1))

    def ch_vals(e1, e2):
        if not per: return {"ch_tu": "", "ch_den": ""}
        return {"ch_tu": ch0 + (e1["index"] - P["from"]) * per, "ch_den": ch0 + (e2["index"] - P["from"] + 1) * per - 1}
    os.makedirs(P["out"], exist_ok=True)

    made = 0
    for g, want in enumerate(plan):
        with cond:                                # chờ các video của nhóm này tải (+ tìm mẫu) xong
            while not all(e["n"] in ready for e in want):
                cond.wait(0.5)
                check_cancel()
        check_cancel()
        vids = [ready[e["n"]] for e in want if ready[e["n"]]]
        if not vids:
            log("Phần %d: không có video nào dùng được — bỏ qua" % (g + start)); continue
        first, last = vids[0]["entry"], vids[-1]["entry"]
        tg = time.time()
        # 1) Cắt + ghép KHÔNG mã hoá (concat demuxer inpoint/outpoint, -c copy) -> 1 file .mka
        ui(enc="Ghép %d/%d…" % (g + 1, len(plan)))
        lst = os.path.join(wdir, "concat_%d.txt" % g)
        items = [(r["path"], x, y) for r in vids for x, y in keep_parts(r, *keep_range(r))]
        concat_list(lst, items)
        joined = os.path.join(wdir, "joined_%d.mka" % g)
        ffmpeg(["-f", "concat", "-safe", "0", "-i", lst, "-map", "0:a", "-c", "copy", joined])
        jdur = sum(y - x for _, x, y in items)
        log("Ghép %d video (%.0f phút) không mã hoá: %.1fs" % (len(vids), jdur / 60, secs(tg)))

        # 2) Tốc độ (bắt buộc mã hoá lại AAC). Tốc độ 1 và nguồn đã là AAC thì giữ nguyên, khỏi mã hoá.
        if abs(P["speed"] - 1) < 0.005 and probe(joined)[1] == "aac":
            audio = joined
        else:
            audio = speed_up(joined, jdur, wdir, g, len(plan))

        # 3) Ảnh bìa + thông tin (copy)
        part = g + start
        vals = {"playlist": playlist, "kenh": channel, "tac_gia": author, "the_loai": genre, "phan": "%0*d" % (width, part), "so": part,
                "tong": last_part, "tu": first["index"], "den": last["index"],
                "ten_dau": first["title"], "ten_cuoi": last["title"],
                "ten": first["title"] + (" → " + last["title"] if len(vids) > 1 else ""),
                "gon_dau": gon[first["n"]], "gon_cuoi": gon[last["n"]],
                "gon": gon[first["n"]] + (" → " + gon[last["n"]] if len(vids) > 1 else ""),
                "ngay": today, "ext": "m4a", "sach": book, **ch_vals(first, last)}
        name = sanitize(render(tpl["nameTpl"], vals))[:150]
        if not name.lower().endswith(".m4a"):
            name += ".m4a"
        out = os.path.join(P["out"], "final_%d.m4a" % g)
        cover = make_cover(wdir, first["index"])
        args = ["-i", audio] + (["-i", cover] if cover else [])
        args += ["-map", "0:a"] + (["-map", "1:v"] if cover else []) + ["-c", "copy"]
        tags = [("title", render(tpl["titleTpl"], vals)), ("album", book), ("album_artist", author), ("artist", author),
                ("genre", genre), ("track", "%d/%d" % (part, last_part))]
        for k, v in tags:
            args += ["-metadata", "%s=%s" % (k, v)]
        args += cover_args(cover)
        try:
            ffmpeg(args + [out])
        except Exception as e:  # noqa: BLE001
            if not cover: raise
            log("Không gắn được ảnh bìa: %s" % short(e))
            ffmpeg(["-i", audio, "-map", "0:a", "-c", "copy", out])
        emit("FILE", out, name, "audio/mp4", "", folder)     # app lưu file (vào thư mục con của sách) rồi xoá
        for x in {joined, audio}:
            if os.path.exists(x): os.remove(x)
        made += 1
        log("Phần %d xong: %.1fs" % (g + 1, secs(tg)))
        ui(enc="", p_enc=(g + 1) / len(plan))

    th.join()
    ad_pool.shutdown()
    if dl_err: raise dl_err[0]
    if temps:
        log("Cắt đoạn giống mẫu (%d mẫu): tổng %.0fs, tìm %.1fs (chạy song song lúc tải)" % (len(temps), ad["cut"], ad["t"]))
    if not made:
        raise RuntimeError("YouTube đang chặn (bắt xác minh bot). Đổi mạng (wifi ↔ 4G) hoặc chờ vài giờ rồi chạy lại."
                           if BOT.is_set() and not got[0] else
                           "Không tải được video nào" if not got[0] else "Không còn video nào sau khi cắt")
    shutil.rmtree(wdir, ignore_errors=True)
    progress(1.0)
    log("TỔNG THỜI GIAN: %.1fs" % secs(T_ALL))


MIME = {"mp3": "audio/mpeg", "m4a": "audio/mp4", "mp4": "audio/mp4", "opus": "audio/ogg", "ogg": "audio/ogg",
        "flac": "audio/flac", "wav": "audio/wav", "mka": "audio/x-matroska", "webm": "audio/webm"}


def speed_file_mode():
    """Tăng tốc 1 file có sẵn -> m4a, giữ ảnh bìa + thông tin. Ghi đè file gốc chỉ khi gốc là m4a/m4b/mp4."""
    src = P["input"]
    sp = float(P.get("fileSpeed") or 0)
    if not 0.5 <= sp <= 4 or abs(sp - 1) < 0.005: raise RuntimeError("Tốc độ nên trong khoảng 0.5 – 4 (khác 1)")
    wdir = os.path.join(P["work"], "speed")
    shutil.rmtree(wdir, ignore_errors=True)
    os.makedirs(wdir, exist_ok=True)
    os.makedirs(P["out"], exist_ok=True)
    dur, _, pic = probe(src)
    if dur <= 0: raise RuntimeError("Không đọc được thời lượng file")
    audio = speed_up(src, dur, wdir, 0, 1, sp)
    name = P.get("name") or os.path.basename(src)
    stem, ext = os.path.splitext(name)
    over = P.get("overwrite") and P.get("inputUri") and ext.lower() in (".m4a", ".m4b", ".mp4")
    if P.get("overwrite") and not over: log("File gốc không phải m4a → lưu bản mới thay vì ghi đè")
    if not over: name = "%s_x%g.m4a" % (stem, sp)
    out = os.path.join(P["out"], "sped.m4a")
    args = ["-i", audio, "-i", src, "-map", "0:a"] + (["-map", "1:v"] if pic else [])
    args += ["-map_metadata", "1", "-c", "copy"] + (["-disposition:v", "attached_pic"] if pic else [])
    ffmpeg(args + [out])
    emit("FILE", out, name, "audio/mp4", ("uri:" + P["inputUri"]) if over else "")
    progress(1.0)


def tag_mode():
    """Gắn ảnh bìa + thông tin cho 1 file có sẵn (chỉ copy, không mã hoá lại)."""
    src = P["input"]
    wdir = os.path.join(P["work"], "tag")
    shutil.rmtree(wdir, ignore_errors=True)
    os.makedirs(wdir, exist_ok=True)
    os.makedirs(P["out"], exist_ok=True)
    cover_src = None
    cs = P.get("coverSource", "keep")
    if cs == "yt":
        status("Lấy thumbnail từ YouTube…")
        link = P.get("link", "")
        o = {**base_opts(), "skip_download": True, "writethumbnail": True,
             "outtmpl": {"default": os.path.join(wdir, "v.%(ext)s"), "thumbnail": os.path.join(wdir, "thumb.%(ext)s")},
             "extractor_args": {"youtube": {"player_client": ["visionos"]}}}
        if "list=" in link:
            o.update({"noplaylist": False, "playlist_items": str(int(P.get("index") or 1))})
        else:
            o["noplaylist"] = True
        with yt_dlp.YoutubeDL(o) as y:
            y.extract_info(link, download=True)
        c = [f for f in os.listdir(wdir) if f.startswith("thumb.")]
        if not c:
            raise RuntimeError("Không lấy được thumbnail")
        cover_src = os.path.join(wdir, c[0])
    elif cs == "image":
        cover_src = P.get("coverPath")
    cover = to_jpg(cover_src, os.path.join(wdir, "cover.jpg")) if cover_src else None
    name = P.get("name") or os.path.basename(src)
    ext = os.path.splitext(name)[1][1:].lower() or "mp3"
    out = os.path.join(P["out"], "tagged." + ext)
    args = ["-i", src]
    args += ["-i", cover, "-map", "0:a", "-map", "1:v"] if cover else ["-map", "0"]
    args += ["-c", "copy"] + (["-id3v2_version", "3"] if ext == "mp3" else [])
    for k in ("title", "album", "artist", "track", "genre"):
        if k in P:
            args += ["-metadata", "%s=%s" % (k, P.get(k, ""))]
    if "artist" in P:                     # tác giả -> cả album_artist (TPE2) theo quy ước audiobook
        args += ["-metadata", "album_artist=%s" % P.get("artist", "")]
    args += cover_args(cover)
    status("Ghi thông tin…")
    ffmpeg(args + [out])
    emit("FILE", out, name, MIME.get(ext, "audio/mpeg"), ("uri:" + P["inputUri"]) if P.get("inputUri") else "")
    progress(1.0)
    log("Đã gắn ảnh bìa / thông tin: %s" % name)


if __name__ == "__main__":
    try:
        {"tag": tag_mode, "addAd": add_ad_mode, "cutAds": cut_ads_mode, "speedFile": speed_file_mode}.get(P.get("mode"), main)()
    except Cancelled:
        emit("ERROR", "Đã dừng")
        quit_now(130)
    except Exception as e:  # noqa: BLE001
        emit("ERROR", short(e))
        quit_now(1)

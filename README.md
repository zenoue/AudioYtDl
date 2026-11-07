# Audio YtDl (Android)

App Android: dán link playlist YouTube → tải audio → **cắt đầu / cắt cuối** → **tăng tốc** → **gộp mỗi N video thành 1 file M4A (AAC)**. Mọi thứ chạy trên điện thoại (dùng mạng của máy nên không bị YouTube chặn như máy chủ).

Tách từ [Playlist_ytdl](https://github.com/zenoue/Playlist_ytdl), bỏ tự tìm quảng cáo, mono, không mã hoá và chọn định dạng; chỉ xuất M4A.

## Mẫu quảng cáo (cắt đoạn giống mẫu)
1. Tab **Mẫu quảng cáo** → dán link video (hoặc playlist + số video), nhập **Từ – Đến** của đoạn quảng cáo → *Lấy mẫu*. App tải audio video đó và cắt đoạn làm mẫu.
   Mẫu lưu ở **`Download/AudioYtDl/Mẫu quảng cáo/<tên mẫu>.m4a`** (có `.nomedia` nên app nghe nhạc không thấy). Mở bằng MiXplorer để nghe thử, đổi tên, xoá, hoặc chép file audio bất kỳ vào làm mẫu.
2. Tích các mẫu cần dùng. Khi tải playlist, mọi chỗ có dạng sóng năng lượng giống mẫu ≥ *Độ giống* (mặc định 85%) bị cắt đúng bằng độ dài mẫu, khi ghép (không mã hoá lại).
3. **Cắt đoạn giống mẫu trong file có sẵn**: chọn file audio trong máy → *Cắt*. File đã tăng tốc thì nhập tốc độ (vd 1.25). Có thể ghi đè file gốc.

## Tải APK
Vào **[Releases](../../releases/latest)** → tải `AudioYtDl-<phiên bản>.apk` → mở để cài (cho phép "Cài ứng dụng không rõ nguồn gốc").

Mỗi lần push code Kotlin lên `main` (tăng `appVersion`), GitHub Actions tự build và tạo Release mới. Có thể cài đè bản mới lên bản cũ.

## Dùng
| Ô | Ý nghĩa |
|---|---|
| Link | link playlist hoặc video (có thể bấm *Chia sẻ* trong app YouTube → chọn Audio YtDl) |
| Từ video / Số video | vd 1 và 10 = 10 video đầu; số video = 0 → tới hết playlist |
| Cắt đầu / Cắt cuối | giây (`10`) hoặc phút:giây (`1:05`) |
| Tốc độ | `1.25`, `1.5`, `2`… (`1` = giữ nguyên, không mã hoá lại) |
| Gộp N | mặc định 10 |
| Thông tin sách → Đếm phần từ | số của phần đầu tiên, vd `6` → phần 06, 07… (track 6/…, 7/…) |

Kết quả: `Download/Audio/<tên playlist>_01_v1-10.m4a`, … kèm **ảnh bìa** (thumbnail video đầu nhóm, hoặc ảnh tự chọn: bấm ô *Ảnh bìa*) và tag album / tác giả / thể loại / số phần để app nghe xếp đúng.

- Tải song song (hiện tốc độ MB/s, dùng aria2c) → cắt + ghép bằng copy → tăng tốc: chia file thành nhiều đoạn, mã hoá AAC song song trên **tất cả nhân CPU** → nối bằng copy.
- Chạy nền có thông báo, tắt màn hình vẫn chạy; bấm *Dừng* trên thông báo để huỷ.
- Bị ngắt giữa chừng → bấm Bắt đầu lại với cùng link, video đã tải được giữ lại.
- Tự cập nhật yt-dlp (12 giờ/lần).

## Bị YouTube chặn ("Sign in to confirm you're not a bot")
App lấy thông tin lần lượt từng video (chờ ngẫu nhiên 1–3 s) và dừng ngay khi bị chặn. Nếu vẫn bị: đổi mạng (wifi ↔ 4G) hoặc chờ vài giờ.

## Kiến trúc
- **APK (khung)**: WebView + cầu nối (`window.App`), dịch vụ chạy nền, tự cập nhật APK.
- **Giao diện** [`web/index.html`](web/index.html) và **chức năng** [`engine/pipeline.py`](engine/pipeline.py): tải từ nhánh `main` của repo này mỗi lần mở / chạy; mất mạng thì dùng bản đã lưu, cuối cùng là bản đóng gói trong APK.
- Sửa `web/` hoặc `engine/` rồi push lên `main` là xong. Chỉ khi đổi Kotlin mới tăng `appVersion` trong `gradle.properties`.
- `UI_API` / `ENGINE_API`: nếu giao diện/engine mới cần khung mới hơn, app giữ bản cũ.

## Kỹ thuật
- [youtubedl-android](https://github.com/yausername/youtubedl-android) 0.18.1: Python + yt-dlp + ffmpeg + QuickJS + aria2c.
- `app/release.keystore` là khoá ký công khai chỉ để các bản build cài đè được lên nhau — **không dùng để phát hành lên Play Store**.

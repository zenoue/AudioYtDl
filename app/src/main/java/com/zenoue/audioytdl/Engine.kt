package com.zenoue.audioytdl

import android.content.ContentValues
import android.content.Context
import android.os.Build
import android.os.Environment
import android.provider.MediaStore
import com.yausername.ffmpeg.FFmpeg
import com.yausername.youtubedl_android.YoutubeDL
import org.json.JSONObject
import java.io.File
import java.net.HttpURLConnection
import java.net.URL
import java.util.Locale

class CancelledByUser : Exception("Đã dừng")

/**
 * Chạy ENGINE Python (engine/pipeline.py) bằng Python đóng gói sẵn trong youtubedl-android.
 * Engine được tải từ GitHub mỗi lần chạy -> sửa engine + push là app dùng ngay, không cần build lại APK.
 * Thứ tự lấy engine: server -> bản đã lưu lần trước -> bản đóng gói trong APK.
 */
class Engine(private val ctx: Context, private val ui: JSONObject) {
    private val saveTree = ui.optString("saveTree")
    private val saveLabel = ui.optString("saveLabel").ifEmpty { "Download/Audio" }

    @Volatile private var cancelled = false
    @Volatile private var proc: Process? = null
    private val prefs = ctx.getSharedPreferences("pipeline", Context.MODE_PRIVATE)
    private val pk = File(ctx.noBackupFilesDir, "youtubedl-android/packages")
    private val libDir = File(ctx.applicationInfo.nativeLibraryDir)

    private fun log(s: String) = JobState.log(s)
    private fun status(s: String) { JobState.status.value = s; log("» $s") }

    fun cancel() {
        cancelled = true
        proc?.destroy() // SIGTERM -> engine giết cả nhóm tiến trình con (ffmpeg, aria2c)
    }

    fun run(): List<String> {
        val t0 = System.currentTimeMillis()
        status("Khởi tạo (lần đầu hơi lâu)…")
        YoutubeDL.getInstance().init(ctx)
        FFmpeg.getInstance().init(ctx)
        val aria = runCatching { com.yausername.aria2c.Aria2c.getInstance().init(ctx) }.isSuccess
        val ariaExe = if (aria) setupAriaLink() else null

        if (System.currentTimeMillis() - prefs.getLong("ytdlpUpdate", 0) > 12 * 3600_000L) {
            status("Cập nhật yt-dlp…")
            runCatching {
                val r = YoutubeDL.getInstance().updateYoutubeDL(ctx, YoutubeDL.UpdateChannel.STABLE)
                prefs.edit().putLong("ytdlpUpdate", System.currentTimeMillis()).apply()
                log("yt-dlp ${YoutubeDL.getInstance().versionName(ctx)} ($r)")
            }.onFailure { log("Không cập nhật được yt-dlp: ${it.message?.take(150)}") }
        } else log("yt-dlp ${YoutubeDL.getInstance().versionName(ctx)}")
        if (cancelled) throw CancelledByUser()

        status("Lấy engine…")
        val (script, source) = engineScript()
        val ver = Regex("ENGINE_VERSION = \"([^\"]+)\"").find(script.readText())?.groupValues?.get(1) ?: "?"
        log(String.format(Locale.US, "Engine %s (%s) · khởi tạo %.1fs", ver, source, (System.currentTimeMillis() - t0) / 1000.0))

        val work = File(ctx.getExternalFilesDir(null) ?: ctx.filesDir, "work").apply { mkdirs() }
        val out = File(ctx.cacheDir, "engine_out").apply { deleteRecursively(); mkdirs() }
        // tham số giao diện gửi sang + đường dẫn do app cung cấp
        val params = JSONObject(ui.toString()).apply {
            put("ytdlp", File(ctx.noBackupFilesDir, "youtubedl-android/yt-dlp/yt-dlp").absolutePath)
            put("ffmpeg", File(libDir, "libffmpeg.so").absolutePath)
            put("qjs", File(libDir, "libqjs.so").absolutePath)
            put("aria2c", ariaExe ?: "")
            put("cert", File(pk, "python/usr/etc/tls/cert.pem").absolutePath)
            put("cache", File(ctx.filesDir, "yt-dlp-cache").absolutePath)
            put("work", work.absolutePath)
            put("out", out.absolutePath)
            put("ads", AdStore.dir(ctx).absolutePath)
        }
        val pf = File(ctx.cacheDir, "engine_params.json").apply { writeText(params.toString()) }

        val pb = ProcessBuilder(File(libDir, "libpython.so").absolutePath, "-u", script.absolutePath, pf.absolutePath)
            .redirectErrorStream(true)
        pb.environment().apply {
            put("LD_LIBRARY_PATH", "${pk.absolutePath}/python/usr/lib:${pk.absolutePath}/ffmpeg/usr/lib:${pk.absolutePath}/aria2c/usr/lib")
            put("SSL_CERT_FILE", File(pk, "python/usr/etc/tls/cert.pem").absolutePath)
            put("PYTHONHOME", "${pk.absolutePath}/python/usr")
            put("HOME", "${pk.absolutePath}/python/usr")
            put("TMPDIR", ctx.cacheDir.absolutePath)
            put("PATH", System.getenv("PATH") + ":" + libDir.absolutePath)
            put("PYTHONIOENCODING", "utf-8")
        }
        val pr = pb.start()
        proc = pr
        if (cancelled) pr.destroy()

        val saved = mutableListOf<String>()
        var error: String? = null
        val raw = ArrayDeque<String>()
        pr.inputStream.bufferedReader().forEachLine { line ->
            if (!line.startsWith("@@")) {         // dòng lạ (cảnh báo Python, aria2c…) -> giữ lại để báo lỗi
                raw.addLast(line); if (raw.size > 15) raw.removeFirst()
                return@forEachLine
            }
            val sp = line.indexOf(' ')
            val kind = if (sp < 0) line.substring(2) else line.substring(2, sp)
            val body = if (sp < 0) "" else line.substring(sp + 1)
            when (kind) {
                "LOG" -> log(body)
                "STATUS" -> JobState.status.value = body
                "PROGRESS" -> body.toDoubleOrNull()?.let { JobState.progress.value = (it * 1000).toInt() }
                "ERROR" -> error = body
                "FILE" -> {
                    val f = body.split('\t')
                    val src = File(f[0])
                    val name = f.getOrElse(1) { src.name }
                    val target = f.getOrElse(3) { "" }
                    try {
                        saved += if (target.startsWith("uri:")) overwrite(src, target.substring(4), name, f.getOrElse(2) { "audio/mpeg" })
                        else export(src, name, f.getOrElse(2) { "audio/mpeg" })
                    } catch (e: Exception) { log("LỖI lưu file: ${e.message}") }
                    finally { src.delete() }
                }
            }
        }
        val code = pr.waitFor()
        proc = null
        if (cancelled) throw CancelledByUser()
        error?.let { throw Exception(it) }
        if (code != 0) throw Exception("Engine dừng bất thường ($code): " + raw.joinToString(" | ").takeLast(400))
        return saved
    }

    /** Engine mới nhất: thử server (5 s), lỗi thì dùng bản đã lưu, cuối cùng là bản đóng gói trong APK. */
    private fun engineScript(): Pair<File, String> {
        val dir = File(ctx.filesDir, "engine").apply { mkdirs() }
        val cached = File(dir, "pipeline.py")
        try {
            val c = URL("$ENGINE_URL?t=${System.currentTimeMillis() / 60_000}").openConnection() as HttpURLConnection
            c.connectTimeout = 5_000; c.readTimeout = 10_000
            if (c.responseCode == 200) {
                val text = c.inputStream.bufferedReader().readText()
                if (text.contains("ENGINE_API = $ENGINE_API")) {
                    cached.writeText(text)
                    return cached to "từ server"
                }
                log("Engine trên server cần bản app mới hơn (khác API) → dùng bản cũ, hãy cập nhật app")
            }
        } catch (e: Exception) {
            log("Không tải được engine mới (${e.message?.take(80)}), dùng bản lưu")
        }
        if (cached.exists() && cached.readText().contains("ENGINE_API = $ENGINE_API")) return cached to "bản đã lưu"
        ctx.assets.open("pipeline.py").use { i -> cached.outputStream().use { i.copyTo(it) } }
        return cached to "bản đóng gói"
    }

    /**
     * Thư viện tự thêm 2 lần --external-downloader-args khi thấy "libaria2c.so" (lần sau đè lần trước).
     * Gọi aria2c qua symlink tên "aria2c" để tự truyền đủ tham số.
     */
    private fun setupAriaLink(): String? = try {
        val link = File(File(ctx.filesDir, "bin").apply { mkdirs() }, "aria2c")
        val target = File(libDir, "libaria2c.so").absolutePath
        if (runCatching { android.system.Os.readlink(link.absolutePath) }.getOrNull() != target) {
            link.delete()
            android.system.Os.symlink(target, link.absolutePath)
        }
        val pb = ProcessBuilder(link.absolutePath, "-v").redirectErrorStream(true)
        pb.environment()["LD_LIBRARY_PATH"] = "${pk.absolutePath}/aria2c/usr/lib:${pk.absolutePath}/python/usr/lib"
        val pr = pb.start()
        val out = pr.inputStream.bufferedReader().readText()
        if (pr.waitFor() == 0 && out.contains("aria2")) link.absolutePath else null
    } catch (e: Exception) {
        log("aria2c không dùng được (${e.message?.take(80)})"); null
    }

    /** Ghi đè file gốc (sửa tag); không được thì lưu thành file mới. */
    private fun overwrite(src: File, uri: String, name: String, mime: String): String = try {
        ctx.contentResolver.openOutputStream(android.net.Uri.parse(uri), "wt")!!.use { o -> src.inputStream().use { it.copyTo(o) } }
        log("Đã ghi đè file gốc: $name"); name
    } catch (e: Exception) {
        log("Không ghi đè được file gốc (${e.message?.take(80)}) → lưu bản mới")
        export(src, name, mime)
    }

    /** Lưu vào thư mục người dùng chọn, mặc định Download/Audio. */
    private fun export(src: File, name: String, mime: String): String {
        if (saveTree.isNotEmpty()) {
            val tree = androidx.documentfile.provider.DocumentFile.fromTreeUri(ctx, android.net.Uri.parse(saveTree))
            if (tree == null || !tree.canWrite()) throw Exception("Không ghi được vào thư mục $saveLabel. Hãy chọn lại thư mục lưu.")
            tree.findFile(name)?.delete()
            val f = tree.createFile(mime, name) ?: throw Exception("Không tạo được file trong $saveLabel")
            ctx.contentResolver.openOutputStream(f.uri)!!.use { o -> src.inputStream().use { it.copyTo(o) } }
            log("Đã lưu: $saveLabel/${f.name ?: name}")
            return name
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            val values = ContentValues().apply {
                put(MediaStore.Downloads.DISPLAY_NAME, name)
                put(MediaStore.Downloads.MIME_TYPE, mime)
                put(MediaStore.Downloads.RELATIVE_PATH, Environment.DIRECTORY_DOWNLOADS + "/Audio")
                put(MediaStore.Downloads.IS_PENDING, 1)
            }
            val resolver = ctx.contentResolver
            val uri = resolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values)
                ?: throw Exception("Không tạo được file trong Download")
            resolver.openOutputStream(uri)!!.use { o -> src.inputStream().use { it.copyTo(o) } }
            values.clear(); values.put(MediaStore.Downloads.IS_PENDING, 0)
            resolver.update(uri, values, null, null)
        } else {
            @Suppress("DEPRECATION")
            val d = File(Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS), "Audio")
            d.mkdirs()
            src.copyTo(File(d, name), overwrite = true)
        }
        log("Đã lưu: Download/Audio/$name")
        return name
    }

    companion object {
        /** Phải khớp ENGINE_API trong engine/pipeline.py */
        const val ENGINE_API = 3
        const val ENGINE_URL = "https://raw.githubusercontent.com/zenoue/AudioYtDl/main/engine/pipeline.py"
    }
}

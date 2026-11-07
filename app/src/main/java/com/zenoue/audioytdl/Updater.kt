package com.zenoue.audioytdl

import android.app.Activity
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.provider.Settings
import android.widget.Toast
import androidx.appcompat.app.AlertDialog
import androidx.core.content.FileProvider
import androidx.lifecycle.LifecycleCoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.io.File
import java.net.HttpURLConnection
import java.net.URL

/**
 * Tự cập nhật: hỏi GitHub Releases bản mới nhất, so phiên bản với bản đang cài,
 * nếu mới hơn thì hiện danh sách thay đổi -> tải APK -> mở trình cài đặt (cài đè, giữ dữ liệu).
 */
class Updater(private val act: Activity, private val scope: LifecycleCoroutineScope) {

    private data class Rel(val tag: String, val notes: String, val apkUrl: String, val size: Long)

    /** "v1.2.3" / "1.2.3" -> [1,2,3] */
    private fun ver(v: String?): List<Int>? =
        Regex("(\\d+)\\.(\\d+)\\.(\\d+)").find(v ?: return null)?.groupValues?.drop(1)?.map { it.toInt() }

    private fun newer(a: List<Int>, b: List<Int>): Boolean {
        for (i in 0 until 3) if (a[i] != b[i]) return a[i] > b[i]
        return false
    }

    private fun currentVersion() =
        runCatching { act.packageManager.getPackageInfo(act.packageName, 0).versionName }.getOrNull() ?: ""

    /** silent = true: chỉ báo khi có bản mới (dùng lúc mở app). */
    fun check(silent: Boolean) = scope.launch {
        val rel = withContext(Dispatchers.IO) { runCatching { fetchLatest() } }
        val r = rel.getOrElse {
            if (!silent) toast("Không kiểm tra được cập nhật: ${it.message}")
            return@launch
        }
        val cur = ver(currentVersion())
        val new = ver(r.tag)
        if (new == null || (cur != null && !newer(new, cur))) {
            if (!silent) toast("Đang dùng bản mới nhất (${currentVersion()})")
            return@launch
        }
        if (act.isFinishing) return@launch
        AlertDialog.Builder(act)
            .setTitle("Có bản mới: ${r.tag}")
            .setMessage(r.notes.replace(Regex("(?m)^#+ ?"), "").trim().take(3000))
            .setPositiveButton(String.format("Cập nhật (%.0f MB)", r.size / 1048576.0)) { _, _ -> download(r) }
            .setNegativeButton("Để sau", null)
            .show()
    }

    private fun fetchLatest(): Rel {
        val c = URL(API).openConnection() as HttpURLConnection
        c.setRequestProperty("Accept", "application/vnd.github+json")
        c.connectTimeout = 10_000; c.readTimeout = 15_000
        val js = JSONObject(c.inputStream.bufferedReader().readText())
        val assets = js.getJSONArray("assets")
        for (i in 0 until assets.length()) {
            val a = assets.getJSONObject(i)
            val name = a.getString("name")
            if (name.endsWith(".apk")) {
                return Rel(js.getString("tag_name"), js.optString("body"),
                    a.getString("browser_download_url"), a.optLong("size"))
            }
        }
        throw Exception("Release mới nhất không có file APK")
    }

    private fun download(r: Rel) = scope.launch {
        // Android 8+: cần cho phép "Cài ứng dụng không rõ nguồn gốc" cho app này
        if (Build.VERSION.SDK_INT >= 26 && !act.packageManager.canRequestPackageInstalls()) {
            toast("Cho phép cài ứng dụng từ Audio YtDl rồi bấm Cập nhật lại")
            act.startActivity(Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:${act.packageName}")))
            return@launch
        }
        toast("Đang tải ${r.tag}…")
        val file = withContext(Dispatchers.IO) {
            runCatching {
                val dir = File(act.cacheDir, "updates").apply { deleteRecursively(); mkdirs() }
                val f = File(dir, "AudioYtDl-${r.tag}.apk")
                var url = URL(r.apkUrl)
                var conn: HttpURLConnection
                // GitHub chuyển hướng sang máy chủ khác (HttpURLConnection không tự theo khi đổi host)
                while (true) {
                    conn = url.openConnection() as HttpURLConnection
                    conn.instanceFollowRedirects = false
                    conn.connectTimeout = 15_000; conn.readTimeout = 30_000
                    if (conn.responseCode in 300..399) { url = URL(conn.getHeaderField("Location")); continue }
                    break
                }
                conn.inputStream.use { i -> f.outputStream().use { i.copyTo(it) } }
                f
            }
        }.getOrElse { toast("Tải bản cập nhật lỗi: ${it.message}"); return@launch }
        val uri = FileProvider.getUriForFile(act, "${act.packageName}.files", file)
        act.startActivity(Intent(Intent.ACTION_VIEW)
            .setDataAndType(uri, "application/vnd.android.package-archive")
            .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK))
    }

    private fun toast(s: String) = Toast.makeText(act, s, Toast.LENGTH_LONG).show()

    companion object {
        const val API = "https://api.github.com/repos/zenoue/AudioYtDl/releases/latest"
    }
}

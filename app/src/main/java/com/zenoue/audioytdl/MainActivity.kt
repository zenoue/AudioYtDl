package com.zenoue.audioytdl

import android.Manifest
import android.annotation.SuppressLint
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.media.MediaMetadataRetriever
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.OpenableColumns
import android.util.Base64
import android.webkit.JavascriptInterface
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.lifecycleScope
import androidx.lifecycle.repeatOnLifecycle
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.io.File
import java.net.HttpURLConnection
import java.net.URL

/**
 * KHUNG app: chỉ là WebView + cầu nối. Giao diện (web/index.html) và chức năng (engine/pipeline.py)
 * đều tải từ GitHub nhánh main -> sửa & push là app đổi theo, không cần build lại APK.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var web: WebView
    private val prefs by lazy { getSharedPreferences("ui", Context.MODE_PRIVATE) }
    private var pendingPick: String? = null
    private var pendingShare: String? = null
    private var pageReady = false

    // ---------- chọn file (kết quả gửi lại giao diện qua window.__onPick) ----------
    private val pickImage = registerForActivityResult(ActivityResultContracts.GetContent()) { uri -> onPicked(uri, "image") }
    private val pickAudio = registerForActivityResult(ActivityResultContracts.OpenDocument()) { uri -> onPicked(uri, "audio") }
    private val pickFolder = registerForActivityResult(ActivityResultContracts.OpenDocumentTree()) { uri -> onPicked(uri, "folder") }

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        ViewCompat.setOnApplyWindowInsetsListener(findViewById(R.id.root)) { v, insets ->
            val b = insets.getInsets(WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.ime())
            v.setPadding(b.left, b.top, b.right, b.bottom); insets
        }
        web = findViewById(R.id.web)
        web.settings.javaScriptEnabled = true
        web.settings.domStorageEnabled = true
        web.addJavascriptInterface(Bridge(), "App")
        web.webViewClient = object : WebViewClient() {
            override fun onPageFinished(view: WebView?, url: String?) {
                pageReady = true
                pushState()
                pendingShare?.let { js("__onShare", JSONObject.quote(it)); pendingShare = null }
            }
        }
        handleShare(intent)
        // 1) hiện ngay bản giao diện đã lưu (hoặc bản đóng gói)  2) tải bản mới, khác thì nạp lại
        loadUi(UiStore.cachedOrBundled(this))
        lifecycleScope.launch {
            val fresh = withContext(Dispatchers.IO) { UiStore.fetch(this@MainActivity) }
            if (fresh != null) loadUi(fresh)
        }

        if (Build.VERSION.SDK_INT >= 33 &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        ) requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 1)
        if (Build.VERSION.SDK_INT <= 29 &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.WRITE_EXTERNAL_STORAGE) != PackageManager.PERMISSION_GRANTED
        ) requestPermissions(arrayOf(Manifest.permission.WRITE_EXTERNAL_STORAGE), 2)

        if (savedInstanceState == null) Updater(this, lifecycleScope).check(silent = true)

        // đẩy trạng thái (đang chạy / tiến độ / nhật ký) sang giao diện, tối đa ~4 lần/giây
        lifecycleScope.launch {
            repeatOnLifecycle(Lifecycle.State.STARTED) {
                combine(JobState.running, JobState.status, JobState.progress, JobState.log) { _, _, _, _ -> }
                    .collect { pushState(); delay(250) }
            }
        }
    }

    private fun loadUi(html: String) {
        pageReady = false
        web.loadDataWithBaseURL("https://audioytdl.app/", html, "text/html", "utf-8", null)
    }

    private fun js(fn: String, vararg args: String) {
        if (!pageReady) return
        web.evaluateJavascript("window.$fn && window.$fn(${args.joinToString(",")})", null)
    }

    private fun pushState() {
        val s = JSONObject().apply {
            put("running", JobState.running.value)
            put("status", JobState.status.value)
            put("progress", JobState.progress.value)
            put("log", JobState.log.value)
        }
        js("__onState", s.toString())
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handleShare(intent)
    }

    private fun handleShare(i: Intent?) {
        if (i?.action != Intent.ACTION_SEND) return
        val text = i.getStringExtra(Intent.EXTRA_TEXT) ?: return
        if (pageReady) js("__onShare", JSONObject.quote(text)) else pendingShare = text
    }

    private fun onPicked(uri: Uri?, kind: String) {
        val id = pendingPick ?: return
        pendingPick = null
        if (uri == null) return
        lifecycleScope.launch {
            val r = withContext(Dispatchers.IO) { runCatching { describe(uri, kind) }.getOrNull() }
            if (r == null) toast("Không đọc được file") else js("__onPick", JSONObject.quote(id), r.toString())
        }
    }

    /** Chép file đã chọn vào bộ nhớ app (engine Python chỉ đọc được đường dẫn thật) và mô tả cho giao diện. */
    private fun describe(uri: Uri, kind: String): JSONObject {
        val o = JSONObject()
        if (kind == "folder") {
            contentResolver.takePersistableUriPermission(uri,
                Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_GRANT_WRITE_URI_PERMISSION)
            val id = android.provider.DocumentsContract.getTreeDocumentId(uri)
            val path = id.substringAfter(':', "")
            o.put("uri", uri.toString())
            o.put("label", path.ifEmpty { if (id.startsWith("primary")) "Bộ nhớ trong" else "Thẻ nhớ" })
            return o
        }
        var name = if (kind == "image") "image" else "audio.mp3"
        contentResolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME), null, null, null)?.use {
            if (it.moveToFirst()) name = it.getString(0) ?: name
        }
        // ảnh bìa riêng giữ lâu dài (filesDir); file audio sửa tag chỉ cần tạm (cacheDir)
        val dir = File(if (kind == "image") filesDir else cacheDir, "picks").apply { mkdirs() }
        if (kind == "audio") dir.listFiles()?.forEach { it.delete() }
        val f = File(dir, "${kind}_${System.currentTimeMillis()}_" + name.replace(Regex("[^\\w.\\-]"), "_"))
        contentResolver.openInputStream(uri)!!.use { i -> f.outputStream().use { i.copyTo(it) } }
        o.put("path", f.absolutePath); o.put("name", name); o.put("uri", uri.toString())
        if (kind == "image") {
            o.put("preview", thumbDataUrl(BitmapFactory.decodeFile(f.absolutePath, sampled(f))))
        } else {
            val r = MediaMetadataRetriever()
            try {
                r.setDataSource(f.absolutePath)
                o.put("title", r.extractMetadata(MediaMetadataRetriever.METADATA_KEY_TITLE) ?: "")
                o.put("album", r.extractMetadata(MediaMetadataRetriever.METADATA_KEY_ALBUM) ?: "")
                o.put("artist", r.extractMetadata(MediaMetadataRetriever.METADATA_KEY_ARTIST) ?: "")
                o.put("track", r.extractMetadata(MediaMetadataRetriever.METADATA_KEY_CD_TRACK_NUMBER) ?: "")
                r.embeddedPicture?.let { o.put("preview", thumbDataUrl(BitmapFactory.decodeByteArray(it, 0, it.size))) }
            } catch (_: Exception) {
            } finally { runCatching { r.release() } }
        }
        return o
    }

    private fun sampled(f: File): BitmapFactory.Options {
        val b = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        BitmapFactory.decodeFile(f.absolutePath, b)
        var s = 1
        while (b.outWidth / s > 400 || b.outHeight / s > 400) s *= 2
        return BitmapFactory.Options().apply { inSampleSize = s }
    }

    private fun thumbDataUrl(bmp: Bitmap?): String {
        bmp ?: return ""
        val out = ByteArrayOutputStream()
        bmp.compress(Bitmap.CompressFormat.JPEG, 80, out)
        return "data:image/jpeg;base64," + Base64.encodeToString(out.toByteArray(), Base64.NO_WRAP)
    }

    private fun toast(s: String) = Toast.makeText(this, s, Toast.LENGTH_SHORT).show()

    /** Các hàm giao diện web gọi được qua window.App.* (chạy trên luồng nền của WebView). */
    inner class Bridge {
        @JavascriptInterface fun info(): String = JSONObject().apply {
            put("app", runCatching { packageManager.getPackageInfo(packageName, 0).versionName }.getOrNull() ?: "")
            put("ui", UiStore.source)
            put("aacEnc", AacEncoder.describe())        // bộ mã hoá MediaCodec của máy (trống = APK cũ / không có)
            put("adsDir", if (AdStore.isPublic(this@MainActivity)) AdStore.LABEL else "bộ nhớ trong của app (không ghi được vào Download)")
        }.toString()

        @JavascriptInterface fun prefGet(key: String): String = prefs.getString(key, "") ?: ""
        @JavascriptInterface fun prefSet(key: String, value: String) { prefs.edit().putString(key, value).apply() }

        /** Chạy engine với tham số JSON từ giao diện (engine/pipeline.py đọc). */
        @JavascriptInterface fun run(params: String) = runOnUiThread {
            if (JobState.running.value) return@runOnUiThread toast("Đang chạy")
            ContextCompat.startForegroundService(this@MainActivity,
                Intent(this@MainActivity, ProcessService::class.java).putExtra("params", params))
        }

        @JavascriptInterface fun stop() = runOnUiThread {
            startService(Intent(this@MainActivity, ProcessService::class.java).setAction(ProcessService.ACTION_STOP))
        }

        /** kind: "image" | "audio" | "folder"; kết quả -> window.__onPick(id, obj) */
        @JavascriptInterface fun pick(kind: String, id: String) = runOnUiThread {
            pendingPick = id
            when (kind) {
                "image" -> pickImage.launch("image/*")
                "audio" -> pickAudio.launch(arrayOf("audio/*"))
                "folder" -> pickFolder.launch(null)
                else -> pendingPick = null
            }
        }

        /** Thư viện mẫu quảng cáo (AdStore): tên các file mẫu / xoá 1 mẫu. */
        @JavascriptInterface fun adFiles(): String = org.json.JSONArray(AdStore.list(this@MainActivity)).toString()
        @JavascriptInterface fun adDelete(name: String) = AdStore.delete(this@MainActivity, name)

        /** Copy chữ vào clipboard (nút Copy ở nhật ký). */
        @JavascriptInterface fun copy(text: String) = runOnUiThread {
            (getSystemService(CLIPBOARD_SERVICE) as android.content.ClipboardManager)
                .setPrimaryClip(android.content.ClipData.newPlainText("Nhật ký Audio YtDl", text))
            if (Build.VERSION.SDK_INT < 33) this@MainActivity.toast("Đã copy nhật ký")   // Android 13+ tự báo
        }

        @JavascriptInterface fun toast(msg: String) = runOnUiThread { this@MainActivity.toast(msg) }
        @JavascriptInterface fun checkUpdate() = runOnUiThread { Updater(this@MainActivity, lifecycleScope).check(silent = false) }
    }
}

/** Giao diện web: bản trên GitHub (main) -> bản đã lưu -> bản đóng gói trong APK. */
object UiStore {
    const val UI_API = 2
    const val URL_UI = "https://raw.githubusercontent.com/zenoue/AudioYtDl/main/web/index.html"
    @Volatile var source = "đóng gói"

    private fun file(ctx: Context) = File(ctx.filesDir, "web/index.html")
    private fun ok(s: String) = s.contains("UI_API = $UI_API")

    fun cachedOrBundled(ctx: Context): String {
        val f = file(ctx)
        if (f.exists()) runCatching { f.readText() }.getOrNull()?.takeIf(::ok)?.let { source = "bản lưu"; return it }
        source = "đóng gói"
        return ctx.assets.open("index.html").bufferedReader().readText()
    }

    /** Trả về HTML mới nếu khác bản đang có, ngược lại null. */
    fun fetch(ctx: Context): String? = try {
        val c = URL("$URL_UI?t=${System.currentTimeMillis() / 60_000}").openConnection() as HttpURLConnection
        c.connectTimeout = 5_000; c.readTimeout = 10_000
        if (c.responseCode != 200) null else {
            val html = c.inputStream.bufferedReader().readText()
            val f = file(ctx)
            if (!ok(html)) null
            else if (f.exists() && f.readText() == html) { source = "server"; null }
            else { f.parentFile?.mkdirs(); f.writeText(html); source = "server"; html }
        }
    } catch (_: Exception) { null }
}

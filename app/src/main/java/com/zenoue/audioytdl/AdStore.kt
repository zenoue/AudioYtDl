package com.zenoue.audioytdl

import android.content.Context
import android.os.Environment
import java.io.File

/**
 * Thư viện mẫu quảng cáo = thư mục Download/AudioYtDl/Mẫu quảng cáo:
 *  - xem / nghe thử / thêm / xoá được bằng trình quản lý file (MiXplorer…);
 *  - có file .nomedia nên MediaStore và app nghe nhạc bỏ qua.
 * Mỗi file audio trong thư mục là 1 mẫu (tên file = tên mẫu). Không ghi được thì dùng bộ nhớ trong của app.
 */
object AdStore {
    const val LABEL = "Download/AudioYtDl/Mẫu quảng cáo"
    private val AUDIO = setOf("m4a", "mp3", "aac", "opus", "ogg", "wav", "flac", "webm", "mka")

    @Volatile private var cached: File? = null

    fun dir(ctx: Context): File {
        cached?.let { if (it.isDirectory) return it }
        @Suppress("DEPRECATION")
        val pub = File(Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS), "AudioYtDl/Mẫu quảng cáo")
        val d = runCatching {
            pub.mkdirs()
            File(pub, ".nomedia").apply { if (!exists()) createNewFile() }
            File(pub, ".probe").apply { writeText("") }.delete()
            pub
        }.getOrNull()?.takeIf { it.canWrite() } ?: File(ctx.filesDir, "ads").apply { mkdirs() }
        // mẫu của bản 0.0.1 (bộ nhớ trong) -> chuyển sang thư mục mới
        val old = File(ctx.filesDir, "ads")
        if (d != old) old.listFiles()?.forEach { f ->
            runCatching { if (!File(d, f.name).exists()) f.copyTo(File(d, f.name)); f.delete() }
        }
        cached = d
        return d
    }

    fun isPublic(ctx: Context) = dir(ctx) != File(ctx.filesDir, "ads")

    /** Tên các file mẫu (có đuôi). */
    fun list(ctx: Context): List<String> =
        dir(ctx).listFiles()?.filter { it.isFile && it.extension.lowercase() in AUDIO }?.map { it.name }?.sorted() ?: emptyList()

    fun delete(ctx: Context, name: String) {
        if (name.isNotEmpty() && !name.contains('/')) File(dir(ctx), name).delete()
    }
}

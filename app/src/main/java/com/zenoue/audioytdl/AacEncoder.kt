package com.zenoue.audioytdl

import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaCodecList
import android.media.MediaFormat
import android.media.MediaMuxer
import android.os.Build
import java.io.File
import java.io.InputStream

/**
 * Mã hoá AAC bằng MediaCodec (bộ mã hoá của Android / hãng chip) thay cho ffmpeg.
 * Engine giải mã + co giãn tốc độ ra PCM s16le rồi gửi "@@AAC" -> Engine.kt gọi encode() -> trả kết quả qua stdin.
 * GPU không có bộ mã hoá audio; đây là lựa chọn "phần cứng" duy nhất Android cho phép.
 */
object AacEncoder {
    private const val MIME = MediaFormat.MIMETYPE_AUDIO_AAC

    private fun encoders(): List<MediaCodecInfo> =
        MediaCodecList(MediaCodecList.REGULAR_CODECS).codecInfos
            .filter { it.isEncoder && it.supportedTypes.any { t -> t.equals(MIME, ignoreCase = true) } }

    private fun isHw(c: MediaCodecInfo) = Build.VERSION.SDK_INT >= 29 && c.isHardwareAccelerated
    private fun isVendor(c: MediaCodecInfo) = Build.VERSION.SDK_INT >= 29 && c.isVendor

    /** Bộ mã hoá ưu tiên: tăng tốc phần cứng > của hãng chip > của Android. */
    val best: MediaCodecInfo? by lazy {
        runCatching { encoders().maxByOrNull { (if (isHw(it)) 2 else 0) + (if (isVendor(it)) 1 else 0) } }.getOrNull()
    }

    /** Mô tả cho giao diện, vd "c2.android.aac.encoder (phần mềm của Android)". */
    fun describe(): String = best?.let {
        it.name + when {
            isHw(it) -> " (phần cứng)"
            isVendor(it) -> " (của hãng chip)"
            else -> " (phần mềm của Android)"
        }
    } ?: ""

    /** PCM s16le xen kênh -> file m4a (AAC-LC). */
    fun encode(pcm: File, out: File, rate: Int, channels: Int, bitrate: Int) {
        val fmt = MediaFormat.createAudioFormat(MIME, rate, channels).apply {
            setInteger(MediaFormat.KEY_AAC_PROFILE, MediaCodecInfo.CodecProfileLevel.AACObjectLC)
            setInteger(MediaFormat.KEY_BIT_RATE, bitrate)
            setInteger(MediaFormat.KEY_MAX_INPUT_SIZE, 1 shl 16)
        }
        val codec = best?.let { MediaCodec.createByCodecName(it.name) } ?: MediaCodec.createEncoderByType(MIME)
        val mux = MediaMuxer(out.absolutePath, MediaMuxer.OutputFormat.MUXER_OUTPUT_MPEG_4)
        var track = -1
        val frame = 2 * channels
        val tmp = ByteArray(1 shl 16)
        val info = MediaCodec.BufferInfo()
        var total = 0L
        var inDone = false
        var outDone = false
        try {
            codec.configure(fmt, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
            codec.start()
            pcm.inputStream().buffered(1 shl 17).use { inp ->
                while (!outDone) {
                    if (!inDone) {
                        val ii = codec.dequeueInputBuffer(10_000)
                        if (ii >= 0) {
                            val buf = codec.getInputBuffer(ii)!!
                            buf.clear()
                            val n = readFully(inp, tmp, minOf(buf.remaining(), tmp.size) / frame * frame)
                            val pts = total / frame * 1_000_000L / rate
                            if (n <= 0) {
                                codec.queueInputBuffer(ii, 0, 0, pts, MediaCodec.BUFFER_FLAG_END_OF_STREAM)
                                inDone = true
                            } else {
                                buf.put(tmp, 0, n)
                                codec.queueInputBuffer(ii, 0, n, pts, 0)
                                total += n
                            }
                        }
                    }
                    val oi = codec.dequeueOutputBuffer(info, 10_000)
                    if (oi == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED) {
                        track = mux.addTrack(codec.outputFormat)
                        mux.start()
                    } else if (oi >= 0) {
                        val ob = codec.getOutputBuffer(oi)!!
                        if (info.flags and MediaCodec.BUFFER_FLAG_CODEC_CONFIG != 0) info.size = 0
                        if (info.size > 0 && track >= 0) {
                            ob.position(info.offset)
                            ob.limit(info.offset + info.size)
                            mux.writeSampleData(track, ob, info)
                        }
                        codec.releaseOutputBuffer(oi, false)
                        if (info.flags and MediaCodec.BUFFER_FLAG_END_OF_STREAM != 0) outDone = true
                    }
                }
            }
        } finally {
            runCatching { codec.stop() }
            codec.release()
            runCatching { if (track >= 0) mux.stop() }
            mux.release()
        }
    }

    private fun readFully(inp: InputStream, b: ByteArray, len: Int): Int {
        var got = 0
        while (got < len) {
            val n = inp.read(b, got, len - got)
            if (n < 0) break
            got += n
        }
        return got
    }
}

package com.zenoue.audioytdl

import android.util.Log
import kotlinx.coroutines.flow.MutableStateFlow

/** Trạng thái dùng chung giữa Service (chạy nền) và màn hình. */
object JobState {
    val running = MutableStateFlow(false)
    val status = MutableStateFlow("Sẵn sàng")
    val progress = MutableStateFlow(0) // 0..1000
    val log = MutableStateFlow("")

    private val lines = ArrayDeque<String>()

    @Synchronized
    fun log(s: String) {
        Log.i("AudioYtDl", s)
        lines.addLast(s)
        while (lines.size > 300) lines.removeFirst()
        log.value = lines.joinToString("\n")
    }

    @Synchronized
    fun reset() {
        lines.clear()
        log.value = ""
        progress.value = 0
    }
}

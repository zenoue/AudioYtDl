package com.zenoue.audioytdl

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import androidx.core.app.NotificationCompat
import androidx.core.app.ServiceCompat
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.debounce
import kotlinx.coroutines.launch

/** Chạy pipeline ở nền (foreground service) để tắt màn hình vẫn chạy tiếp. */
@OptIn(kotlinx.coroutines.FlowPreview::class)
class ProcessService : Service() {

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private var pipeline: Engine? = null
    private var wakeLock: PowerManager.WakeLock? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) {
            JobState.status.value = "Đang dừng…"
            pipeline?.cancel()
            return START_NOT_STICKY
        }
        if (JobState.running.value) return START_NOT_STICKY
        val params = intent?.getStringExtra("params")?.let { runCatching { org.json.JSONObject(it) }.getOrNull() }
            ?: run { stopSelf(); return START_NOT_STICKY }

        createChannel()
        ServiceCompat.startForeground(
            this, NOTIF_ID, buildNotification("Đang chuẩn bị…", 0),
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC else 0
        )
        wakeLock = (getSystemService(Context.POWER_SERVICE) as PowerManager)
            .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "AudioYtDl:run").apply { acquire(12 * 3600_000L) }

        JobState.reset()
        JobState.running.value = true
        val p = Engine(applicationContext, params)
        pipeline = p

        val nm = getSystemService(NotificationManager::class.java)
        val notifJob = scope.launch {
            combine(JobState.status, JobState.progress) { s, pr -> s to pr }
                .debounce(700)
                .collect { (s, pr) -> nm.notify(NOTIF_ID, buildNotification(s, pr)) }
        }

        scope.launch {
            val msg = try {
                val files = p.run()
                when (params.optString("mode")) {
                    "tag" -> "Đã ghi ảnh bìa / thông tin"
                    "addAd" -> "Đã lưu mẫu"
                    else -> "Hoàn thành ${files.size} file → " + params.optString("saveLabel").ifEmpty { "Download/Audio" }
                }
            } catch (e: CancelledByUser) {
                "Đã dừng"
            } catch (e: Throwable) {
                JobState.log("LỖI: ${e.message}")
                "Lỗi: ${e.message?.lines()?.lastOrNull { it.isNotBlank() }?.take(200)}"
            }
            notifJob.cancel()
            JobState.status.value = msg
            JobState.log("» $msg")
            JobState.running.value = false
            finishNotification(msg)
            wakeLock?.let { if (it.isHeld) it.release() }
            ServiceCompat.stopForeground(this@ProcessService, ServiceCompat.STOP_FOREGROUND_DETACH)
            stopSelf()
        }
        return START_NOT_STICKY
    }

    override fun onDestroy() {
        pipeline?.cancel()
        scope.cancel()
        wakeLock?.let { if (it.isHeld) it.release() }
        super.onDestroy()
    }

    private fun openAppIntent() = PendingIntent.getActivity(
        this, 0, Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP),
        PendingIntent.FLAG_IMMUTABLE
    )

    private fun buildNotification(text: String, progress: Int): Notification {
        val stop = PendingIntent.getService(
            this, 1, Intent(this, ProcessService::class.java).setAction(ACTION_STOP),
            PendingIntent.FLAG_IMMUTABLE
        )
        return NotificationCompat.Builder(this, CHANNEL)
            .setSmallIcon(R.drawable.ic_notify)
            .setContentTitle("Audio YtDl")
            .setContentText(text)
            .setStyle(NotificationCompat.BigTextStyle().bigText(text))
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setProgress(1000, progress, false)
            .setContentIntent(openAppIntent())
            .addAction(0, "Dừng", stop)
            .build()
    }

    private fun finishNotification(text: String) {
        val n = NotificationCompat.Builder(this, CHANNEL)
            .setSmallIcon(R.drawable.ic_notify)
            .setContentTitle("Audio YtDl")
            .setContentText(text)
            .setStyle(NotificationCompat.BigTextStyle().bigText(text))
            .setContentIntent(openAppIntent())
            .setAutoCancel(true)
            .build()
        getSystemService(NotificationManager::class.java).notify(NOTIF_DONE_ID, n)
    }

    private fun createChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            getSystemService(NotificationManager::class.java).createNotificationChannel(
                NotificationChannel(CHANNEL, "Xử lý audio", NotificationManager.IMPORTANCE_LOW)
            )
        }
    }

    companion object {
        const val ACTION_STOP = "stop"
        private const val CHANNEL = "process"
        private const val NOTIF_ID = 1
        private const val NOTIF_DONE_ID = 2
    }
}

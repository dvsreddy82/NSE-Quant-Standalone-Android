package com.ravikiran.nsequant.monitor

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Paint
import android.graphics.Typeface
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat
import androidx.core.app.ServiceCompat
import androidx.core.content.ContextCompat
import androidx.core.graphics.drawable.IconCompat
import com.ravikiran.nsequant.MainActivity
import com.ravikiran.nsequant.R
import com.ravikiran.nsequant.data.Api
import com.ravikiran.nsequant.data.Fmt
import com.ravikiran.nsequant.data.Prefs
import com.ravikiran.nsequant.data.arr
import com.ravikiran.nsequant.data.d
import com.ravikiran.nsequant.data.obj
import com.ravikiran.nsequant.data.objects
import com.ravikiran.nsequant.data.s
import com.ravikiran.nsequant.widget.BnfWidget
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

/**
 * Market monitor: an ongoing notification with live BANKNIFTY / NIFTY / VIX (like the desktop app's
 * menu-bar ticker, every 5 s) and a notification for every new alert from the engine, checked every
 * 30 s: RED risk alerts, level breaks, global moves, the evening setup, BANKNIFTY breadth, EXIT on
 * your positions and HIGH-impact news (high priority), plus the 15-minute direction calls (one
 * per index, replaced each time, with sound only when the direction changes).
 */
class MonitorService : Service() {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) {
            Prefs.setMonitor(this, false)
            stopSelf()
            return START_NOT_STICKY
        }
        channels()
        val n = ticker("Starting the engine…", "")
        ServiceCompat.startForeground(this, TICKER_ID, n,
            if (Build.VERSION.SDK_INT >= 34) ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE else 0)
        if (!running) {
            running = true
            scope.launch { tickerLoop() }
            scope.launch { alertLoop() }
        }
        return START_STICKY
    }

    override fun onDestroy() {
        running = false
        scope.cancel()
        super.onDestroy()
    }

    private suspend fun tickerLoop() {
        val nm = getSystemService(NotificationManager::class.java)
        var n = 0
        while (scope.isActive) {
            if (n++ % 3 == 0 && BnfWidget.hasWidgets(this)) {       // home-screen widget: every ~15 s
                try {
                    BnfWidget.render(this, Api.get(this, "/api/banknifty"))
                } catch (e: Exception) {
                    BnfWidget.renderError(this, e.message ?: "offline")
                }
            }
            try {
                val t = Api.get(this, "/api/ticker")
                val q = t.arr("quotes").objects()
                val bnf = q.firstOrNull { it.s("symbol") == "NIFTY BANK" }
                val iq = q.firstOrNull { it.s("symbol") == Prefs.iconIndex(this@MonitorService) } ?: bnf
                val icon = iq?.let { statusIcon(it.d("last"), it.d("chg"), it.d("pct"), Prefs.iconMode(this@MonitorService)) }
                val title = bnf?.let {
                    "BNF ${Fmt.px(it.d("last"))}  ${arrow(it.d("chg"))}${Fmt.px1(it.d("chg")?.let { c -> kotlin.math.abs(c) })} " +
                        "(${Fmt.pct(it.d("pct"))})"
                } ?: "BANKNIFTY —"
                val rest = q.filter { it.s("symbol") != "NIFTY BANK" }.joinToString("   ") {
                    "${if (it.s("symbol") == "^NSEI") "NIFTY" else "VIX"} ${Fmt.px(it.d("last"))} ${Fmt.pct(it.d("pct"))}"
                }
                val extra = buildList {
                    t.obj("direction")?.let { d ->
                        val m = mapOf("BULLISH" to "▲", "BEARISH" to "▼", "SIDEWAYS" to "↔")
                        listOf("NIFTY" to "N", "BANKNIFTY" to "BNF").mapNotNull { (k, ab) -> d.s(k)?.let { "$ab ${m[it] ?: it}" } }
                            .takeIf { it.isNotEmpty() }?.let { add("Dir " + it.joinToString(" ")) }
                    }
                    t.s("global_read")?.let { add("🌐 $it") }
                    t.d("book_pnl")?.let { add("P&L ${Fmt.rupees(it)}") }
                    (t.d("red") ?: 0.0).toInt().takeIf { it > 0 }?.let { add("⚠ $it RED") }
                    when (t.s("setup")) { "up" -> add("⚑▲ setup"); "down" -> add("⚑▼ setup") }
                }.joinToString(" · ")
                nm.notify(TICKER_ID, ticker(title, listOf(rest, extra).filter { it.isNotBlank() }.joinToString("\n"),
                    icon, rest, bnf?.let { "BNF ${Fmt.pct(it.d("pct"))}" }))
            } catch (e: Exception) {
                nm.notify(TICKER_ID, ticker("Waiting for prices", e.message ?: ""))
            }
            delay(5_000)
        }
    }

    private suspend fun alertLoop() {
        val nm = getSystemService(NotificationManager::class.java)
        while (scope.isActive) {
            try {
                val items = Api.get(this, "/api/alerts").arr("alerts").objects()
                val seen = Prefs.seen(this)
                // oldest first, so the newest lands on top of the notification shade
                val fresh = items.filter { (it.s("id") ?: "") !in seen }.sortedBy { it.s("at") ?: "" }
                // 15-minute direction calls: one notification per index, replaced each time; sound only on a change
                fresh.filter { it.s("level") == "DIRECTION" }.forEach { a ->
                    val id = a.s("id") ?: return@forEach
                    nm.notify(if ((a.s("title") ?: "").startsWith("BANKNIFTY")) DIR_BNF_ID else DIR_NIFTY_ID,
                        NotificationCompat.Builder(this, CH_DIRECTION)
                            .setSmallIcon(R.drawable.ic_stat_ticker)
                            .setContentTitle("➚ ${a.s("title")}")
                            .setContentText(a.s("text"))
                            .setStyle(NotificationCompat.BigTextStyle().bigText(a.s("text")))
                            .setSilent(!a.optBoolean("changed"))
                            .setContentIntent(openApp())
                            .setAutoCancel(true)
                            .build())
                    seen.add(id)
                }
                // many global moves at once (e.g. an overnight sell-off): one summary instead of a flood
                val glb = fresh.filter { it.s("level") == "GLOBAL" }
                if (glb.size > 3) {
                    nm.notify(GLOBAL_SUM_ID, NotificationCompat.Builder(this, CH_ALERTS)
                        .setSmallIcon(R.drawable.ic_stat_ticker)
                        .setContentTitle("🌐 Global markets · ${glb.size} major moves")
                        .setContentText(glb.joinToString(" · ") { it.s("title") ?: "" })
                        .setStyle(NotificationCompat.BigTextStyle().bigText(glb.joinToString("\n") { it.s("title") ?: "" }))
                        .setPriority(NotificationCompat.PRIORITY_HIGH)
                        .setContentIntent(openApp())
                        .setAutoCancel(true)
                        .build())
                    glb.forEach { seen.add(it.s("id") ?: "") }
                }
                fresh.filter { (it.s("id") ?: "") !in seen }.forEach { a ->
                    val id = a.s("id") ?: return@forEach
                    val info = a.s("level") == "WRITER" && a.s("severity") == "INFO"
                    nm.notify(id.hashCode(), NotificationCompat.Builder(this, if (info) CH_WRITER_INFO else CH_ALERTS)
                        .setSmallIcon(R.drawable.ic_stat_ticker)
                        .setWhen(epoch(a.s("at")))
                        .setShowWhen(true)
                        .setContentTitle("${tag(a.s("level"))} ${a.s("title")}")
                        .setContentText(a.s("text"))
                        .setStyle(NotificationCompat.BigTextStyle().bigText("${a.s("text")}\n— ${a.s("source")}"))
                        .setPriority(if (info) NotificationCompat.PRIORITY_DEFAULT else NotificationCompat.PRIORITY_HIGH)
                        .setCategory(if (info) NotificationCompat.CATEGORY_RECOMMENDATION else NotificationCompat.CATEGORY_ALARM)
                        .setContentIntent(a.s("link")?.takeIf { it.startsWith("http") }?.let { openLink(it, id) } ?: openApp())
                        .setAutoCancel(true)
                        .build())
                    seen.add(id)
                    Prefs.setSeen(this, seen)
                }
                Prefs.setSeen(this, seen, items.mapNotNull { it.s("id") })
            } catch (_: Exception) {
            }
            delay(30_000)
        }
    }

    private fun epoch(iso: String?): Long = try {
        java.time.OffsetDateTime.parse(iso).toInstant().toEpochMilli()
    } catch (_: Exception) {
        System.currentTimeMillis()
    }

    private fun tag(level: String?) = when (level) {
        "RED" -> "🔴"; "AMBER" -> "🟠"; "SETUP" -> "⚑"; "NEWS" -> "📰"; "GLOBAL" -> "🌐"; "LEVEL" -> "📉"; "WRITER" -> "✍"; else -> "•"
    }

    private fun openLink(url: String, id: String): PendingIntent = PendingIntent.getActivity(this, id.hashCode(),
        Intent(Intent.ACTION_VIEW, android.net.Uri.parse(url)), PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
    private fun arrow(v: Double?) = if ((v ?: 0.0) >= 0) "▲" else "▼"

    /**
     * The status-bar icon IS the price: a white-on-transparent bitmap redrawn every update, so the
     * move is visible at the top of the screen at all times, including on the lock screen
     * (Android tints small icons, so it's shape-only: no green / red).
     */
    private fun statusIcon(last: Double?, chg: Double?, pct: Double?, mode: String): IconCompat? {
        last ?: return null
        val size = 128
        val bmp = Bitmap.createBitmap(size, size, Bitmap.Config.ARGB_8888)
        val c = Canvas(bmp)
        val p = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            color = android.graphics.Color.WHITE
            typeface = Typeface.create(Typeface.SANS_SERIF, Typeface.BOLD)
            textAlign = Paint.Align.CENTER
        }
        val price = if (last >= 10_000) String.format("%.1fK", last / 1000) else String.format("%.0f", last)
        val pc = pct?.let { String.format("%+.2f", it) } ?: "-"
        val pts = chg?.let { String.format("%+.0f", it) } ?: "-"
        fun fit(text: String, maxH: Float) {
            p.textSize = maxH
            val w = p.measureText(text)
            if (w > size - 4) p.textSize = maxH * (size - 4) / w
        }
        when (mode) {
            "pct", "points" -> {
                val t = if (mode == "pct") pc else pts
                fit(t, 92f)
                c.drawText(t, size / 2f, size / 2f - (p.descent() + p.ascent()) / 2, p)
            }
            else -> {                                   // two lines: price over % change
                fit(price, 62f)
                c.drawText(price, size / 2f, size * 0.47f, p)
                fit(pc, 62f)
                c.drawText(pc, size / 2f, size * 0.97f, p)
            }
        }
        return IconCompat.createWithBitmap(bmp)
    }

    private fun ticker(title: String, text: String, icon: IconCompat? = null, publicText: String = "",
                       chip: String? = null): Notification {
        val small = icon ?: IconCompat.createWithResource(this, R.drawable.ic_stat_ticker)
        // lock screen with "hide sensitive content": prices only, never P&L
        val pub = NotificationCompat.Builder(this, CH_TICKER)
            .setSmallIcon(small)
            .setContentTitle(title)
            .setContentText(publicText)
            .build()
        return NotificationCompat.Builder(this, CH_TICKER)
            .setSmallIcon(small)
            .setVisibility(NotificationCompat.VISIBILITY_PRIVATE)
            .setPublicVersion(pub)
            .setCategory(NotificationCompat.CATEGORY_STATUS)
            .setContentTitle(title)
            .setContentText(text.lineSequence().firstOrNull() ?: "")
            .setStyle(NotificationCompat.BigTextStyle().bigText(text))
            .setOngoing(true)
            .setOnlyAlertOnce(true)                    // no setSilent(): some phones file "silent" ones off the lock screen
            .setShowWhen(false)
            // Android 16 Live Update: pinned at the top of the lock screen with a status-bar chip
            // (EXTRA_REQUEST_PROMOTED_ONGOING / EXTRA_SHORT_CRITICAL_TEXT; ignored by older Android)
            .addExtras(android.os.Bundle().apply {
                putBoolean("android.requestPromotedOngoing", true)
                chip?.let { putString("android.shortCriticalText", it) }
            })
            .setContentIntent(openApp())
            .addAction(0, "Stop monitor", PendingIntent.getService(this, 1,
                Intent(this, MonitorService::class.java).setAction(ACTION_STOP),
                PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT))
            .build()
    }

    private fun openApp(): PendingIntent = PendingIntent.getActivity(this, 0,
        Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP),
        PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)

    private fun channels() {
        val nm = getSystemService(NotificationManager::class.java)
        nm.deleteNotificationChannel("ticker")             // v1 channel: low importance, hidden on lock screens
        // Default importance (so it shows on the lock screen and as a status-bar icon) but silent.
        nm.createNotificationChannel(NotificationChannel(CH_TICKER, "Live ticker", NotificationManager.IMPORTANCE_DEFAULT)
            .apply {
                description = "BANKNIFTY / NIFTY / VIX in the status bar and on the lock screen, every few seconds"
                setShowBadge(false)
                setSound(null, null)
                enableVibration(false)
                enableLights(false)
                lockscreenVisibility = Notification.VISIBILITY_PRIVATE
            })
        nm.createNotificationChannel(NotificationChannel(CH_ALERTS, "Risk alerts", NotificationManager.IMPORTANCE_HIGH)
            .apply { description = "RED risk alerts, writer signals, level breaks, global moves, evening setups, EXIT signals" })
        nm.createNotificationChannel(NotificationChannel(CH_WRITER_INFO, "Writer info", NotificationManager.IMPORTANCE_DEFAULT)
            .apply { description = "Profit targets, writing conditions, max pain, expected move — useful, not urgent" })
        nm.createNotificationChannel(NotificationChannel(CH_DIRECTION, "Direction calls", NotificationManager.IMPORTANCE_DEFAULT)
            .apply { description = "NIFTY / BANKNIFTY direction every 15 minutes in market hours (sound when it changes)" })
    }

    companion object {
        const val TICKER_CHANNEL = "ticker_live"
        private const val CH_TICKER = TICKER_CHANNEL
        private const val CH_ALERTS = "alerts"
        private const val CH_DIRECTION = "direction"
        private const val CH_WRITER_INFO = "writer_info"
        private const val TICKER_ID = 1001
        private const val DIR_NIFTY_ID = 1101
        private const val DIR_BNF_ID = 1102
        private const val GLOBAL_SUM_ID = 1103
        private const val ACTION_STOP = "stop"
        @Volatile private var running = false
        val isRunning: Boolean get() = running

        fun start(ctx: Context) = ContextCompat.startForegroundService(ctx, Intent(ctx, MonitorService::class.java))
        fun stop(ctx: Context) { ctx.stopService(Intent(ctx, MonitorService::class.java)) }
    }
}

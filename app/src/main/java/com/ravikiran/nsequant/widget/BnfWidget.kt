package com.ravikiran.nsequant.widget

import android.app.PendingIntent
import android.appwidget.AppWidgetManager
import android.appwidget.AppWidgetProvider
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.DashPathEffect
import android.graphics.LinearGradient
import android.graphics.Paint
import android.graphics.Path
import android.graphics.Shader
import android.os.Build
import android.os.Bundle
import android.util.SizeF
import android.widget.RemoteViews
import com.ravikiran.nsequant.MainActivity
import com.ravikiran.nsequant.R
import com.ravikiran.nsequant.data.Api
import com.ravikiran.nsequant.data.Fmt
import com.ravikiran.nsequant.data.arr
import com.ravikiran.nsequant.data.d
import com.ravikiran.nsequant.data.doubles
import com.ravikiran.nsequant.data.obj
import com.ravikiran.nsequant.data.objects
import com.ravikiran.nsequant.data.s
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import org.json.JSONObject
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import kotlin.math.abs
import kotlin.math.max
import kotlin.math.min

/**
 * BANKNIFTY home-screen widget: price, change, today's line against the previous close, day
 * range, breadth and the biggest lifter / dragger.  Updated every few seconds by the Market
 * monitor while it runs, every 30 min by the system otherwise, and on tapping ⟳.
 */
class BnfWidget : AppWidgetProvider() {

    override fun onUpdate(ctx: Context, mgr: AppWidgetManager, ids: IntArray) = refresh(ctx)

    override fun onAppWidgetOptionsChanged(ctx: Context, mgr: AppWidgetManager, id: Int, opts: Bundle) {
        last?.let { render(ctx, it) } ?: refresh(ctx)
    }

    override fun onReceive(ctx: Context, intent: Intent) {
        super.onReceive(ctx, intent)
        if (intent.action == ACTION_REFRESH) refresh(ctx)
    }

    private fun refresh(ctx: Context) {
        val pending = goAsync()
        CoroutineScope(Dispatchers.IO).launch {
            try {
                render(ctx, Api.get(ctx, "/api/banknifty"))
            } catch (e: Exception) {
                renderError(ctx, e.message ?: "offline")
            } finally {
                pending.finish()
            }
        }
    }

    companion object {
        const val ACTION_REFRESH = "com.ravikiran.nsequant.widget.REFRESH"
        private val UP = 0xFF22C55E.toInt()
        private val DOWN = 0xFFEF4444.toInt()
        private val TEXT = 0xFFE5E7EB.toInt()
        private val MUTED = 0xFF94A3B8.toInt()
        @Volatile private var last: JSONObject? = null

        fun hasWidgets(ctx: Context): Boolean =
            AppWidgetManager.getInstance(ctx).getAppWidgetIds(ComponentName(ctx, BnfWidget::class.java)).isNotEmpty()

        /** Draw the latest /api/banknifty answer into every BANKNIFTY widget. */
        fun render(ctx: Context, d: JSONObject) {
            if (d.optBoolean("loading")) return
            last = d
            val mgr = AppWidgetManager.getInstance(ctx)
            val ids = mgr.getAppWidgetIds(ComponentName(ctx, BnfWidget::class.java))
            for (id in ids) mgr.updateAppWidget(id, views(ctx, d, mgr.getAppWidgetOptions(id)))
        }

        fun renderError(ctx: Context, msg: String) {
            val mgr = AppWidgetManager.getInstance(ctx)
            val ids = mgr.getAppWidgetIds(ComponentName(ctx, BnfWidget::class.java))
            for (id in ids) {
                val v = last?.let { views(ctx, it, mgr.getAppWidgetOptions(id)) } ?: RemoteViews(ctx.packageName, R.layout.widget_bnf)
                v.setTextViewText(R.id.updated, "⚠ " + msg.take(28))
                wire(ctx, v)
                mgr.updateAppWidget(id, v)
            }
        }

        private fun views(ctx: Context, d: JSONObject, opts: Bundle): RemoteViews {
            val wide = wideViews(ctx, d)
            val small = smallViews(ctx, d)
            return if (Build.VERSION.SDK_INT >= 31) {
                RemoteViews(mapOf(SizeF(110f, 40f) to small, SizeF(200f, 100f) to wide))
            } else {
                val w = opts.getInt(AppWidgetManager.OPTION_APPWIDGET_MIN_WIDTH, 250)
                val h = opts.getInt(AppWidgetManager.OPTION_APPWIDGET_MIN_HEIGHT, 110)
                if (w < 200 || h < 100) small else wide
            }
        }

        private fun header(v: RemoteViews, d: JSONObject) {
            val i = d.obj("index") ?: JSONObject()
            val chg = i.d("chg")
            v.setTextViewText(R.id.price, Fmt.px(i.d("last")))
            v.setTextViewText(R.id.change, "${if ((chg ?: 0.0) >= 0) "▲" else "▼"} ${Fmt.px1(chg?.let { abs(it) })} (${Fmt.pct(i.d("pct"))})")
            v.setTextColor(R.id.change, if (chg == null) TEXT else if (chg >= 0) UP else DOWN)
        }

        private fun smallViews(ctx: Context, d: JSONObject): RemoteViews {
            val v = RemoteViews(ctx.packageName, R.layout.widget_bnf_small)
            header(v, d)
            v.setOnClickPendingIntent(R.id.root, openApp(ctx))
            return v
        }

        private fun wideViews(ctx: Context, d: JSONObject): RemoteViews {
            val v = RemoteViews(ctx.packageName, R.layout.widget_bnf)
            header(v, d)
            val i = d.obj("index") ?: JSONObject()
            val spark = i.arr("spark").doubles()
            if (spark.size >= 2) v.setImageViewBitmap(R.id.spark, sparkline(spark, i.d("prev")))
            val hi = i.d("high"); val lo = i.d("low"); val lastPx = i.d("last")
            val pos = if (hi != null && lo != null && lastPx != null && hi > lo) " · at ${((lastPx - lo) / (hi - lo) * 100).toInt()}% of range" else ""
            v.setTextViewText(R.id.range, "H ${Fmt.px1(hi)} · L ${Fmt.px1(lo)}$pos")
            v.setTextViewText(R.id.breadth, "${d.d("adv")?.toInt() ?: 0}▲ ${d.d("dec")?.toInt() ?: 0}▼ banks · " +
                String.format(Locale.US, "%.0f%% of weight up", d.d("up_weight") ?: 0.0))
            val rows = d.arr("rows").objects().filter { it.d("contrib") != null }
            val lift = rows.maxByOrNull { it.d("contrib")!! }
            val drag = rows.minByOrNull { it.d("contrib")!! }
            v.setTextViewText(R.id.movers, listOfNotNull(
                lift?.takeIf { it.d("contrib")!! > 0 }?.let { "↑ ${it.s("symbol")} ${Fmt.signed(it.d("contrib"), 0)}" },
                drag?.takeIf { it.d("contrib")!! < 0 }?.let { "↓ ${it.s("symbol")} ${Fmt.signed(it.d("contrib"), 0)}" },
            ).joinToString("   "))
            val live = d.optBoolean("live")
            v.setTextViewText(R.id.updated, (if (live) "● " else "") + SimpleDateFormat("HH:mm:ss", Locale.US).format(Date()))
            v.setTextColor(R.id.updated, if (live) UP else MUTED)
            wire(ctx, v)
            return v
        }

        private fun wire(ctx: Context, v: RemoteViews) {
            v.setOnClickPendingIntent(R.id.root, openApp(ctx))
            v.setOnClickPendingIntent(R.id.refresh, PendingIntent.getBroadcast(ctx, 7,
                Intent(ctx, BnfWidget::class.java).setAction(ACTION_REFRESH),
                PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT))
        }

        private fun openApp(ctx: Context): PendingIntent = PendingIntent.getActivity(ctx, 8,
            Intent(ctx, MainActivity::class.java).putExtra("tab", "banks").addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)

        /** Today's line, green above / red below the previous close (dotted), with a soft fill. */
        private fun sparkline(vals: List<Double>, prev: Double?): Bitmap {
            val w = 720; val h = 200
            val bmp = Bitmap.createBitmap(w, h, Bitmap.Config.ARGB_8888)
            val c = Canvas(bmp)
            val lo = min(vals.min(), prev ?: vals.min()); val hi = max(vals.max(), prev ?: vals.max())
            val span = (hi - lo).takeIf { it > 0 } ?: 1.0
            val pad = 8f
            fun y(v: Double) = (pad + (h - 2 * pad) * (1 - (v - lo) / span)).toFloat()
            fun x(i: Int) = i / (vals.size - 1f) * w
            val up = vals.last() >= (prev ?: vals.first())
            val col = if (up) UP else DOWN
            val line = Path().apply { vals.forEachIndexed { i, v -> if (i == 0) moveTo(x(i), y(v)) else lineTo(x(i), y(v)) } }
            val fill = Path(line).apply { lineTo(w.toFloat(), h.toFloat()); lineTo(0f, h.toFloat()); close() }
            c.drawPath(fill, Paint(Paint.ANTI_ALIAS_FLAG).apply {
                shader = LinearGradient(0f, 0f, 0f, h.toFloat(), (col and 0x00FFFFFF) or 0x55000000, col and 0x00FFFFFF,
                    Shader.TileMode.CLAMP)
            })
            prev?.let {
                c.drawLine(0f, y(it), w.toFloat(), y(it), Paint(Paint.ANTI_ALIAS_FLAG).apply {
                    color = 0xFF64748B.toInt(); strokeWidth = 2f; pathEffect = DashPathEffect(floatArrayOf(8f, 8f), 0f)
                })
            }
            c.drawPath(line, Paint(Paint.ANTI_ALIAS_FLAG).apply { color = col; strokeWidth = 4f; style = Paint.Style.STROKE })
            c.drawCircle(x(vals.size - 1), y(vals.last()), 6f, Paint(Paint.ANTI_ALIAS_FLAG).apply { color = col })
            return bmp
        }
    }
}

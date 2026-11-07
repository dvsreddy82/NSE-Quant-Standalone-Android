package com.ravikiran.nsequant.ui

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Rect
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.drawText
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.rememberTextMeasurer
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.ravikiran.nsequant.data.Fmt
import com.ravikiran.nsequant.data.arr
import com.ravikiran.nsequant.data.b
import com.ravikiran.nsequant.data.d
import com.ravikiran.nsequant.data.doubles
import com.ravikiran.nsequant.data.obj
import com.ravikiran.nsequant.data.objects
import com.ravikiran.nsequant.data.s
import org.json.JSONObject
import kotlin.math.abs
import kotlin.math.max
import kotlin.math.min

/** Green / red by % change, saturating at ±3%. */
fun heat(pct: Double?): Color {
    if (pct == null) return Pal.panel2
    val a = (min(abs(pct) / 3.0, 1.0) * 0.75 + 0.25).toFloat()
    val base = if (pct >= 0) Color(0xFF16A34A) else Color(0xFFDC2626)
    val bg = Color(0xFF111827)
    return Color(bg.red + (base.red - bg.red) * a, bg.green + (base.green - bg.green) * a,
        bg.blue + (base.blue - bg.blue) * a)
}

/** Squarified treemap: (key, size) → rectangles filling `area`. */
fun squarify(items: List<Pair<String, Double>>, area: Rect): Map<String, Rect> {
    val list = items.filter { it.second > 0 }.sortedByDescending { it.second }
    val total = list.sumOf { it.second }.takeIf { it > 0 } ?: return emptyMap()
    var x = area.left; var y = area.top; var w = area.width; var h = area.height
    val scale = w * h / total
    val rest = ArrayDeque(list.map { it.first to it.second * scale })
    val out = mutableMapOf<String, Rect>()
    fun worst(row: List<Pair<String, Double>>, side: Double): Double {
        val s = row.sumOf { it.second }
        return row.maxOf { max(side * side * it.second / (s * s), (s * s) / (side * side * it.second)) }
    }
    while (rest.isNotEmpty()) {
        val side = min(w, h).toDouble()
        val row = mutableListOf(rest.removeFirst())
        while (rest.isNotEmpty() && worst(row + rest.first(), side) <= worst(row, side)) row.add(rest.removeFirst())
        val s = row.sumOf { it.second }
        if (w >= h) {
            val cw = (s / h).toFloat(); var cy = y
            row.forEach { (k, a) -> val ch = (a / cw).toFloat(); out[k] = Rect(x, cy, x + cw, cy + ch); cy += ch }
            x += cw; w -= cw
        } else {
            val ch = (s / w).toFloat(); var cx = x
            row.forEach { (k, a) -> val cw = (a / ch).toFloat(); out[k] = Rect(cx, y, cx + cw, y + ch); cx += cw }
            y += ch; h -= ch
        }
    }
    return out
}

@Composable
fun BankNiftyScreen() {
    val st = rememberApi("/api/banknifty", 3_000)
    Loading(st) { d ->
        if (d.b("loading")) {
            Text("Loading the BANKNIFTY constituents (first run downloads their candles)…", color = Pal.muted, modifier = Modifier.padding(16.dp))
            return@Loading
        }
        val rows = d.arr("rows").objects().sortedByDescending { it.d("weight") ?: 0.0 }
        LazyColumn(contentPadding = PaddingValues(vertical = 6.dp)) {
            item { Header(d) }
            item {
                Section("Heatmap · size = weight, colour = move") { Heatmap(rows) }
            }
            item {
                Section("Points added to BANKNIFTY · total ${Fmt.signed(d.d("explained"), 0)}") { Contribution(rows) }
            }
            item {
                val al = d.arr("alerts").objects()
                if (al.isNotEmpty()) Section("Alerts") {
                    al.forEach { a ->
                        AlertCard(if (a.s("level") == "red") "RED" else "AMBER", "BANKNIFTY", listOf(a.s("text") ?: ""))
                    }
                }
            }
            item {
                Row(Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 6.dp)) {
                    listOf("Bank" to 1.4f, "Wt" to 0.6f, "LTP" to 1f, "%" to 0.8f, "Pts" to 0.7f, "Today" to 1f).forEach {
                        Text(it.first, Modifier.weight(it.second), color = Pal.muted, fontSize = 11.sp)
                    }
                }
            }
            items(rows) { q -> BankRow(q) }
            item {
                val fit = d.obj("fit")
                Text("Weights fitted from 1-minute returns" + (fit?.d("r2")?.let { String.format(" (R² %.3f)", it) } ?: "") +
                    " · points = weight × % change × previous close", color = Pal.faint, fontSize = 10.sp,
                    modifier = Modifier.padding(16.dp))
            }
        }
    }
}

@Composable
private fun Header(d: JSONObject) {
    val i = d.obj("index") ?: return
    val rows = d.arr("rows").objects()
    val lift = rows.filter { (it.d("contrib") ?: 0.0) > 0 }.sortedByDescending { it.d("contrib") }.take(3)
    val drag = rows.filter { (it.d("contrib") ?: 0.0) < 0 }.sortedBy { it.d("contrib") }.take(3)
    Section {
        Row(verticalAlignment = Alignment.Bottom) {
            Text("BANKNIFTY ", fontWeight = FontWeight.ExtraBold, fontSize = 20.sp)
            Text(Fmt.px(i.d("last")), fontWeight = FontWeight.ExtraBold, fontSize = 20.sp)
        }
        Text("${Fmt.signed(i.d("chg"))} (${Fmt.pct(i.d("pct"))})" + if (d.b("live")) "  ● live" else "",
            color = Pal.chg(i.d("chg")), fontWeight = FontWeight.Bold)
        Text("O ${Fmt.px1(i.d("open"))} · H ${Fmt.px1(i.d("high"))} · L ${Fmt.px1(i.d("low"))}", color = Pal.muted, fontSize = 12.sp)
        Text("Breadth ${d.d("adv")?.toInt()} up / ${d.d("dec")?.toInt()} down · " +
            String.format("%.0f%% of the weight is up", d.d("up_weight") ?: 0.0), fontSize = 12.sp)
        if (lift.isNotEmpty()) Text("Lifting: " + lift.joinToString { "${it.s("symbol")} ${Fmt.signed(it.d("contrib"), 0)}" },
            color = Pal.up, fontSize = 12.sp)
        if (drag.isNotEmpty()) Text("Dragging: " + drag.joinToString { "${it.s("symbol")} ${Fmt.signed(it.d("contrib"), 0)}" },
            color = Pal.down, fontSize = 12.sp)
    }
}

@Composable
private fun Heatmap(rows: List<JSONObject>) {
    val tm = rememberTextMeasurer()
    Canvas(Modifier.fillMaxWidth().height(260.dp)) {
        val rects = squarify(rows.map { (it.s("symbol") ?: "") to (it.d("weight") ?: 0.0) }, Rect(Offset.Zero, size))
        val by = rows.associateBy { it.s("symbol") ?: "" }
        rects.forEach { (k, r) ->
            val q = by[k] ?: return@forEach
            val box = Rect(r.left + 2, r.top + 2, r.right - 2, r.bottom - 2)
            drawRect(heat(q.d("pct")), box.topLeft, box.size)
            val lines = mutableListOf(k to 13.sp, Fmt.pct(q.d("pct")) to 12.sp)
            if (box.height > 110) lines.add(Fmt.px(q.d("last")) to 11.sp)
            if (box.height > 140) lines.add("${Fmt.signed(q.d("contrib"), 0)} pts" to 11.sp)
            var yy = box.center.y - lines.size * 17f
            lines.forEach { (text, size) ->
                val res = tm.measure(text, TextStyle(color = Color.White, fontSize = size, fontWeight = FontWeight.Bold),
                    maxLines = 1, overflow = TextOverflow.Ellipsis,
                    constraints = androidx.compose.ui.unit.Constraints(maxWidth = max(box.width.toInt() - 6, 1)))
                drawText(res, topLeft = Offset(box.center.x - res.size.width / 2f, yy))
                yy += res.size.height + 2
            }
        }
    }
}

@Composable
private fun Contribution(rows: List<JSONObject>) {
    val tm = rememberTextMeasurer()
    val list = rows.filter { it.d("contrib") != null }.sortedByDescending { it.d("contrib") }
    val m = list.maxOfOrNull { abs(it.d("contrib")!!) }?.takeIf { it > 0 } ?: 1.0
    Canvas(Modifier.fillMaxWidth().height((list.size * 20 + 4).dp)) {
        val label = 90.dp.toPx(); val num = 44.dp.toPx()
        val w = size.width - label - num
        val zero = label + w / 2
        val bh = size.height / max(list.size, 1)
        drawLine(Pal.faint, Offset(zero, 0f), Offset(zero, size.height))
        list.forEachIndexed { i, q ->
            val v = q.d("contrib")!!
            val len = (abs(v) / m * w / 2).toFloat()
            val y = i * bh
            drawRect(if (v >= 0) Pal.up else Pal.down, Offset(if (v >= 0) zero else zero - len, y + bh * 0.2f),
                Size(len, bh * 0.6f))
            val name = tm.measure(q.s("symbol") ?: "", TextStyle(color = Pal.text, fontSize = 11.sp))
            drawText(name, topLeft = Offset(label - name.size.width - 6, y + (bh - name.size.height) / 2))
            val n = tm.measure(Fmt.signed(v, 0), TextStyle(color = Pal.muted, fontSize = 10.sp))
            drawText(n, topLeft = Offset(if (v >= 0) zero + len + 4 else zero - len - n.size.width - 4,
                y + (bh - n.size.height) / 2))
        }
    }
}

@Composable
private fun BankRow(q: JSONObject) {
    Row(Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 5.dp), verticalAlignment = Alignment.CenterVertically) {
        Column(Modifier.weight(1.4f)) {
            Text(q.s("symbol") ?: "", fontSize = 12.sp, fontWeight = FontWeight.SemiBold)
            val vw = q.d("vs_vwap"); val rv = q.d("rvol")
            Text((vw?.let { "VWAP ${Fmt.pct(it)}" } ?: "") + (rv?.let { String.format(" · %.1f× vol", it) } ?: ""),
                color = if ((rv ?: 0.0) >= 2) Pal.warn else Pal.faint, fontSize = 9.sp)
        }
        Text(String.format("%.1f%%", (q.d("weight") ?: 0.0) * 100), Modifier.weight(0.6f), fontSize = 11.sp, color = Pal.muted)
        Text(Fmt.px(q.d("last")), Modifier.weight(1f), fontSize = 12.sp)
        Text(Fmt.pct(q.d("pct")), Modifier.weight(0.8f), fontSize = 12.sp, color = Pal.chg(q.d("pct")))
        Text(Fmt.signed(q.d("contrib"), 0), Modifier.weight(0.7f), fontSize = 12.sp, color = Pal.chg(q.d("contrib")))
        Spark(q.arr("spark").doubles(), q.d("prev"), Modifier.weight(1f).height(26.dp))
    }
}

@Composable
fun Spark(vals: List<Double>, prev: Double?, modifier: Modifier) {
    Canvas(modifier) {
        if (vals.size < 2) return@Canvas
        val lo = min(vals.min(), prev ?: vals.min()); val hi = max(vals.max(), prev ?: vals.max())
        val span = (hi - lo).takeIf { it > 0 } ?: 1.0
        fun y(v: Double) = (size.height - (v - lo) / span * size.height).toFloat()
        prev?.let { drawLine(Pal.faint, Offset(0f, y(it)), Offset(size.width, y(it)), strokeWidth = 1f) }
        val p = Path()
        vals.forEachIndexed { i, v ->
            val x = i / (vals.size - 1f) * size.width
            if (i == 0) p.moveTo(x, y(v)) else p.lineTo(x, y(v))
        }
        drawPath(p, if (vals.last() >= (prev ?: vals.first())) Pal.up else Pal.down, style = Stroke(width = 2f))
    }
}

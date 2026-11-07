package com.ravikiran.nsequant.ui

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.text.font.FontWeight
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
import com.ravikiran.nsequant.data.strings
import org.json.JSONObject
import kotlin.math.abs

/** Global markets: the read for India's open, global alerts and every market by group. */
@Composable
fun GlobalScreen() {
    val st = rememberApi("/api/global", 60_000)
    Loading(st) { g ->
        val quotes = g.arr("quotes").objects()
        LazyColumn(contentPadding = PaddingValues(vertical = 6.dp)) {
            item { CueBlock(g) }
            item {
                val al = g.arr("alerts").objects().sortedByDescending { it.s("raised_at") ?: "" }
                Section("Global alerts (24 h) · newest first") {
                    if (al.isEmpty()) Text("No major global moves in the last 24 hours.", color = Pal.muted, fontSize = 12.sp)
                    al.forEach { a ->
                        AlertCard(if (a.s("level") == "HIGH") "RED" else "AMBER", a.s("title") ?: "",
                            listOfNotNull(a.s("text")), hhmm(a.s("raised_at")?.let { utcToIst(it) }))
                    }
                }
            }
            quotes.groupBy { it.s("group") ?: "" }.forEach { (grp, qs) ->
                item {
                    Section(grp) { qs.forEach { QuoteRow(it) } }
                }
            }
            item {
                Text("GIFT Nifty from sgxnifty.org, the rest from Yahoo Finance, checked every 5 min around the clock. Change is against each " +
                    "market's previous close (US 10Y in basis points); bold = past its alert threshold. Context for " +
                    "the session, not a forecast." +
                    (g.s("checked_at")?.let { "  Checked ${it.substring(11, 16)}." } ?: ""),
                    color = Pal.faint, fontSize = 10.sp, modifier = Modifier.padding(16.dp))
            }
        }
    }
}

/** Mongo hands back naive UTC times; the alert list shows IST. */
private fun utcToIst(iso: String): String = try {
    val hasZone = iso.length > 19 && (iso.contains('+') || iso.endsWith("Z") || iso.lastIndexOf('-') > 9)
    val t = if (hasZone) java.time.OffsetDateTime.parse(iso)
    else java.time.LocalDateTime.parse(iso).atOffset(java.time.ZoneOffset.UTC)
    t.atZoneSameInstant(java.time.ZoneId.of("Asia/Kolkata")).toOffsetDateTime().toString()
} catch (_: Exception) { iso }

@Composable
private fun CueBlock(g: JSONObject) {
    val cue = g.obj("cue") ?: JSONObject()
    val score = cue.d("score") ?: 0.0
    val col = if (score >= 25) Pal.up else if (score <= -25) Pal.down else Pal.warn
    Section("Read for India's next open") {
        Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Text(cue.s("read") ?: "—", color = col, fontWeight = FontWeight.ExtraBold, fontSize = 24.sp)
            Text("score ${Fmt.signed(score, 0)} (−100…+100)", color = Pal.muted, fontSize = 12.sp)
        }
        cue.obj("gift")?.takeIf { !it.b("stale") }?.let { gf ->
            val gp = gf.d("gap_pct")
            Text("GIFT Nifty ${Fmt.px(gf.d("last"))}  ${Fmt.pct(gf.d("pct"))}" +
                (gf.s("open")?.let { o -> "  →  $o open (${Fmt.signed(gf.d("gap"), 0)} pts vs NIFTY close)" } ?: ""),
                color = if ((gp ?: 0.0) >= 0.2) Pal.up else if ((gp ?: 0.0) <= -0.1) Pal.down else Pal.text,
                fontWeight = FontWeight.Bold, fontSize = 14.sp)
        }
        cue.s("writer")?.let { Text("Writer read: $it", fontSize = 13.sp) }
        cue.arr("drivers").strings().filterNot { it.startsWith("GIFT Nifty") }
            .forEach { Text("• $it", color = Pal.muted, fontSize = 12.sp) }
        val failed = g.arr("failed").strings()
        if (failed.isNotEmpty()) Text("Unavailable: ${failed.joinToString(", ")}", color = Pal.warn, fontSize = 11.sp)
    }
}

@Composable
private fun QuoteRow(q: JSONObject) {
    val bps = q.s("unit") == "bps"
    val big = (q.d("move")?.let { abs(it) } ?: 0.0) >= (q.d("threshold") ?: 99.0)
    val c = Pal.chg(q.d("chg"))
    val w = if (big) FontWeight.Bold else FontWeight.Normal
    Row(Modifier.fillMaxWidth().padding(vertical = 3.dp), verticalAlignment = Alignment.CenterVertically) {
        Column(Modifier.weight(1f)) {
            Text(q.s("name") ?: "", fontSize = 13.sp, fontWeight = w)
            Text((q.s("at")?.let { hhmm(it) } ?: "") + if (q.b("offline")) " · stored" else "",
                color = Pal.faint, fontSize = 10.sp)
        }
        Spark(q.arr("spark").doubles(), c, Modifier.width(64.dp).height(22.dp))
        Spacer(Modifier.width(8.dp))
        Column(horizontalAlignment = Alignment.End, modifier = Modifier.width(96.dp)) {
            Text(if (bps) String.format("%.3f%%", q.d("last") ?: 0.0) else Fmt.px(q.d("last")), fontSize = 13.sp)
            Text(if (bps) "${Fmt.signed(q.d("move"), 0)} bps" else "${Fmt.signed(q.d("chg"))}  ${Fmt.pct(q.d("pct"))}",
                color = c, fontSize = 11.sp, fontWeight = w)
        }
    }
}

@Composable
private fun Spark(v: List<Double>, color: Color, modifier: Modifier) {
    if (v.size < 2) { Spacer(modifier); return }
    Canvas(modifier) {
        val lo = v.min(); val hi = v.max(); val span = (hi - lo).takeIf { it > 0 } ?: 1.0
        val p = Path()
        v.forEachIndexed { i, x ->
            val px = i / (v.size - 1f) * size.width
            val py = ((1 - (x - lo) / span) * size.height).toFloat()
            if (i == 0) p.moveTo(px, py) else p.lineTo(px, py)
        }
        drawPath(p, color, style = Stroke(width = 2.dp.toPx()))
        drawCircle(color, 2.5.dp.toPx(), Offset(size.width, ((1 - (v.last() - lo) / span) * size.height).toFloat()))
    }
}

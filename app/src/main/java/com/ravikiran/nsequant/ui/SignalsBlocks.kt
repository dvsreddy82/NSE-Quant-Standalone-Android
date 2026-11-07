package com.ravikiran.nsequant.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.ravikiran.nsequant.data.Fmt
import com.ravikiran.nsequant.data.arr
import com.ravikiran.nsequant.data.d
import com.ravikiran.nsequant.data.obj
import com.ravikiran.nsequant.data.objects
import com.ravikiran.nsequant.data.s
import com.ravikiran.nsequant.data.strings
import org.json.JSONObject
import kotlin.math.abs

private fun dirColors(dir: String?): Pair<Color, Color> = when (dir) {
    "BULLISH" -> Pal.up to Pal.greenBg
    "BEARISH" -> Pal.down to Pal.redBg
    else -> Pal.warn to Pal.amberBg
}

/** The 15-minute direction calls for NIFTY and BANKNIFTY, with the nearest levels. */
@Composable
fun SignalsBlock(sig: JSONObject) {
    Section("Direction · every 15 min") {
        for (und in listOf("NIFTY", "BANKNIFTY")) {
            val u = sig.obj(und) ?: continue
            val c = u.obj("call")
            Column(Modifier.fillMaxWidth().padding(vertical = 4.dp)) {
                Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Text(und, fontWeight = FontWeight.Bold, fontSize = 13.sp)
                    if (c != null) {
                        val (fg, bg) = dirColors(c.s("dir"))
                        Badge(c.s("direction") ?: "", fg, bg)
                        Text("${c.s("confidence")} · ${c.s("slot")?.takeLast(5)}", color = Pal.muted, fontSize = 11.sp)
                    } else {
                        Text("first call at 9:30 am", color = Pal.muted, fontSize = 11.sp)
                    }
                    Spacer(Modifier.weight(1f))
                    val n = u.d("graded")?.toInt() ?: 0
                    if (n > 0) Text("${u.d("hits")?.toInt()}/$n right", color = Pal.faint, fontSize = 10.sp)
                }
                c?.s("idea")?.let { Text(it, fontSize = 12.sp, modifier = Modifier.padding(top = 2.dp)) }
                val spot = u.d("spot") ?: c?.d("spot")
                val lv = u.arr("levels").objects()
                if (spot != null && lv.isNotEmpty()) {
                    val above = lv.filter { (it.d("price") ?: 0.0) > spot }.minByOrNull { it.d("price")!! }
                    val below = lv.filter { (it.d("price") ?: 0.0) <= spot }.maxByOrNull { it.d("price")!! }
                    Text(listOfNotNull(
                        below?.let { "Support ${Fmt.int(it.d("price"))} ${it.s("name")}" + (it.s("status")?.let { s -> " ($s)" } ?: "") },
                        above?.let { "Resistance ${Fmt.int(it.d("price"))} ${it.s("name")}" + (it.s("status")?.let { s -> " ($s)" } ?: "") },
                    ).joinToString("  ·  "), color = Pal.muted, fontSize = 11.sp)
                }
            }
        }
        sig.arr("alerts").objects().take(3).forEach { a ->
            AlertCard(if (a.s("level") == "HIGH") "RED" else "AMBER", a.s("title") ?: "", listOf(a.s("text") ?: ""))
        }
    }
}

/** World markets that move India's open, and the engine's read of them. */
@Composable
fun GlobalBlock(g: JSONObject) {
    val cue = g.obj("cue") ?: JSONObject()
    val score = cue.d("score") ?: 0.0
    val col = if (score >= 25) Pal.up else if (score <= -25) Pal.down else Pal.warn
    Section("Global markets") {
        Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Text(cue.s("read") ?: "—", color = col, fontWeight = FontWeight.ExtraBold, fontSize = 18.sp)
            Text("open read ${Fmt.signed(score, 0)}", color = Pal.muted, fontSize = 11.sp)
        }
        cue.s("writer")?.let { Text(it, fontSize = 12.sp) }
        val drivers = cue.arr("drivers").strings()
        if (drivers.isNotEmpty()) Text(drivers.joinToString(" · "), color = Pal.muted, fontSize = 11.sp)
        Spacer(Modifier.padding(2.dp))
        g.arr("quotes").objects().forEach { q ->
            val bps = q.s("unit") == "bps"
            val big = (q.d("move")?.let { abs(it) } ?: 0.0) >= (q.d("threshold") ?: 99.0)
            Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                Text(q.s("name") ?: "", Modifier.weight(1f), fontSize = 12.sp,
                    fontWeight = if (big) FontWeight.Bold else FontWeight.Normal)
                Text(if (bps) String.format("%.3f%%", q.d("last") ?: 0.0) else Fmt.px(q.d("last")), fontSize = 12.sp)
                Spacer(Modifier.width(10.dp))
                Text(if (bps) Fmt.signed(q.d("move"), 0) + " bps" else Fmt.pct(q.d("pct")),
                    color = Pal.chg(q.d("chg")), fontSize = 12.sp,
                    fontWeight = if (big) FontWeight.Bold else FontWeight.Normal, modifier = Modifier.width(72.dp))
            }
        }
    }
}

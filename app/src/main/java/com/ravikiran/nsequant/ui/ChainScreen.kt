package com.ravikiran.nsequant.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.material3.FilterChip
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.ravikiran.nsequant.data.Fmt
import com.ravikiran.nsequant.data.arr
import com.ravikiran.nsequant.data.d
import com.ravikiran.nsequant.data.obj
import com.ravikiran.nsequant.data.objects
import com.ravikiran.nsequant.data.s
import org.json.JSONObject

private val CE = Color(0xFFEF4444)
private val PE = Color(0xFF22C55E)
private val ITM = Color(0x1AFACC15)

@Composable
fun ChainScreen() {
    var und by rememberSaveable { mutableStateOf("NIFTY") }
    val st = rememberApi("/api/chain?und=$und&n=15", 8_000)
    Column {
        Row(Modifier.padding(horizontal = 12.dp, vertical = 6.dp), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            listOf("NIFTY", "BANKNIFTY").forEach { u ->
                FilterChip(selected = und == u, onClick = { und = u }, label = { Text(u) })
            }
        }
        Loading(st) { d -> if (d.s("underlying") == und) ChainBody(d) }
    }
}

@Composable
private fun ChainBody(d: JSONObject) {
    val s = d.obj("summary") ?: JSONObject()
    val rows = d.arr("rows").objects()
    val spot = d.d("spot") ?: 0.0
    val list = rememberLazyListState()
    val atmIndex = rows.indexOfFirst { (it.d("strike") ?: 0.0) > spot }.coerceAtLeast(0)
    LaunchedEffect(d.s("expiry")) { if (rows.isNotEmpty()) list.scrollToItem(maxOf(atmIndex - 4, 0) + 2) }
    val mxCe = rows.maxOfOrNull { it.d("ce_oi") ?: 0.0 }?.takeIf { it > 0 } ?: 1.0
    val mxPe = rows.maxOfOrNull { it.d("pe_oi") ?: 0.0 }?.takeIf { it > 0 } ?: 1.0
    LazyColumn(state = list, contentPadding = PaddingValues(bottom = 12.dp)) {
        item {
            Section {
                Text("${d.s("underlying")} ${Fmt.px(spot)} · ${d.s("expiry")} · ${String.format("%.1f", d.d("dte") ?: 0.0)}d",
                    fontWeight = FontWeight.Bold)
                Text(if (d.s("source") == "shoonya") "● Shoonya real-time" else "NSE (≈1–2 min delayed)",
                    color = if (d.s("source") == "shoonya") Pal.up else Pal.warn, fontSize = 11.sp)
                Row(horizontalArrangement = Arrangement.spacedBy(16.dp)) {
                    Column(Modifier.weight(1f)) {
                        KV("ATM IV", d.d("atm_iv")?.let { String.format("%.2f%%", it) } ?: "—")
                        KV("Straddle", "${Fmt.px1(d.d("straddle"))} pts")
                        KV("Expected ±1σ", "±${Fmt.int(d.d("sigma_move"))}")
                        KV("Max pain", Fmt.int(s.d("max_pain")))
                    }
                    Column(Modifier.weight(1f)) {
                        KV("PCR (OI)", s.d("pcr")?.let { String.format("%.2f", it) } ?: "—")
                        KV("PCR (Δ OI)", s.d("pcr_chg")?.let { String.format("%.2f", it) } ?: "—")
                        KV("Support (PE OI)", Fmt.int(s.d("support")), PE)
                        KV("Resistance (CE OI)", Fmt.int(s.d("resistance")), CE)
                    }
                }
                val fresh = listOfNotNull(s.d("sup_add")?.let { "puts at ${Fmt.int(it)}" },
                    s.d("res_add")?.let { "calls at ${Fmt.int(it)}" })
                if (fresh.isNotEmpty()) Text("Most OI added today: ${fresh.joinToString(" · ")}", color = Pal.muted, fontSize = 11.sp)
            }
        }
        d.optJSONObject("signal")?.let { sg -> item { SignalCard(sg) } }
        d.optJSONObject("pcr")?.takeIf { it.has("rows") }?.let { b -> item { PcrCard(b) } }
        item {
            Row(Modifier.fillMaxWidth().padding(horizontal = 8.dp, vertical = 4.dp)) {
                listOf("CE OI" to 1.1f, "Chg" to 0.9f, "LTP" to 1f, "Strike" to 1.1f, "LTP" to 1f, "Chg" to 0.9f, "PE OI" to 1.1f)
                    .forEachIndexed { i, (h, w) ->
                        Text(h, Modifier.weight(w), color = if (i < 3) CE else if (i > 3) PE else Pal.text, fontSize = 10.sp,
                            textAlign = TextAlign.Center, fontWeight = FontWeight.Bold)
                    }
            }
        }
        rows.forEachIndexed { idx, r ->
            if (idx == atmIndex) item {
                Text("▶ SPOT ${Fmt.px(spot)}", Modifier.fillMaxWidth().background(Pal.accent).padding(4.dp),
                    color = Color.Black, fontWeight = FontWeight.Bold, fontSize = 11.sp, textAlign = TextAlign.Center)
            }
            item { ChainRow(r, spot, d.d("atm"), mxCe, mxPe) }
        }
    }
}

@Composable
private fun ChainRow(r: JSONObject, spot: Double, atm: Double?, mxCe: Double, mxPe: Double) {
    val k = r.d("strike") ?: 0.0
    val ceItm = k < spot
    val atmRow = atm != null && k == atm
    Row(Modifier.fillMaxWidth().height(30.dp).background(if (atmRow) Color(0x2838BDF8) else Color.Transparent)
        .padding(horizontal = 8.dp), verticalAlignment = Alignment.CenterVertically) {
        Bar(Fmt.short(r.d("ce_oi")), (r.d("ce_oi") ?: 0.0) / mxCe, CE, true, Modifier.weight(1.1f), ceItm)
        Cell(Fmt.shortSigned(r.d("ce_chg_oi")), Pal.chg(r.d("ce_chg_oi")), Modifier.weight(0.9f), ceItm)
        Cell(Fmt.px(r.d("ce_ltp")), Pal.chg(r.d("ce_chg_pct")), Modifier.weight(1f), ceItm)
        Column(Modifier.weight(1.1f), horizontalAlignment = Alignment.CenterHorizontally) {
            Text(Fmt.int(k), fontWeight = FontWeight.Bold, fontSize = 12.sp)
            r.d("pcr")?.let {
                Text(String.format("%.2f", it), fontSize = 9.sp,
                    color = if (it > 1.2) PE else if (it < 0.8) CE else Pal.muted)
            }
        }
        Cell(Fmt.px(r.d("pe_ltp")), Pal.chg(r.d("pe_chg_pct")), Modifier.weight(1f), !ceItm)
        Cell(Fmt.shortSigned(r.d("pe_chg_oi")), Pal.chg(r.d("pe_chg_oi")), Modifier.weight(0.9f), !ceItm)
        Bar(Fmt.short(r.d("pe_oi")), (r.d("pe_oi") ?: 0.0) / mxPe, PE, false, Modifier.weight(1.1f), !ceItm)
    }
}

@Composable
private fun Cell(text: String, color: Color, modifier: Modifier, itm: Boolean) {
    Box(modifier.fillMaxHeight().background(if (itm) ITM else Color.Transparent), contentAlignment = Alignment.Center) {
        Text(text, color = color, fontSize = 11.sp)
    }
}

@Composable
private fun Bar(text: String, frac: Double, color: Color, fromRight: Boolean, modifier: Modifier, itm: Boolean) {
    Box(modifier.fillMaxHeight().background(if (itm) ITM else Color.Transparent),
        contentAlignment = if (fromRight) Alignment.CenterEnd else Alignment.CenterStart) {
        Box(Modifier.fillMaxWidth(frac.toFloat().coerceIn(0.02f, 1f)).height(18.dp).background(color.copy(alpha = 0.3f)))
        Text(text, Modifier.fillMaxWidth(), fontSize = 11.sp, textAlign = TextAlign.Center)
    }
}


/** PCR indicators now / 15 / 30 min ago with trend, the 15-minute OI flow and the combined read (pcr_board). */
@Composable
private fun PcrCard(b: org.json.JSONObject) {
    Section("PCR · positioning and flow") {
        val f2 = { v: Double? -> v?.let { String.format("%.2f", it) } ?: "—" }
        Row(Modifier.fillMaxWidth()) {
            listOf("Indicator" to 2.2f, "Now" to 0.8f, "15m" to 0.8f, "30m" to 0.8f, "Trend" to 1.2f).forEach { (h, w) ->
                Text(h, Modifier.weight(w), color = Pal.faint, fontSize = 10.sp)
            }
        }
        b.arr("rows").objects().filter { it.s("key") != "mom" }.forEach { r ->
            val t = r.s("trend") ?: "—"
            Row(Modifier.fillMaxWidth()) {
                Text((r.s("name") ?: "") + if (r.s("key") == "wtd") " ★" else "", Modifier.weight(2.2f), fontSize = 11.sp,
                    fontWeight = if (r.s("key") == "wtd") FontWeight.Bold else FontWeight.Normal)
                Text(f2(r.d("now")), Modifier.weight(0.8f), fontSize = 11.sp, fontWeight = FontWeight.Bold)
                Text(f2(r.d("m15")), Modifier.weight(0.8f), fontSize = 11.sp, color = Pal.muted)
                Text(f2(r.d("m30")), Modifier.weight(0.8f), fontSize = 11.sp, color = Pal.muted)
                Text(t, Modifier.weight(1.2f), fontSize = 10.sp,
                    color = if (t.contains("ris")) Pal.up else if (t.contains("fall")) Pal.down else Pal.muted)
            }
        }
        val read = b.s("read") ?: ""
        Text("PCR read: $read", fontWeight = FontWeight.Bold, fontSize = 13.sp, modifier = Modifier.padding(top = 4.dp),
            color = if (read.contains("ullish")) Pal.up else if (read.contains("earish")) Pal.down else if (read.contains("sideways")) Pal.warn else Pal.text)
        Text(b.s("why") ?: "", color = Pal.muted, fontSize = 11.sp)
        b.optJSONObject("flow15")?.let { fl ->
            Text("Last 15 min near ATM — put writing ${Fmt.short(fl.d("pe_writing"))}, put buying ${Fmt.short(fl.d("pe_buying"))}, " +
                "call writing ${Fmt.short(fl.d("ce_writing"))}, call buying ${Fmt.short(fl.d("ce_buying"))}", color = Pal.muted, fontSize = 11.sp)
        }
        b.optJSONObject("levels")?.let { lv ->
            Text("Largest put OI ${Fmt.int(lv.d("put_oi"))} · largest call OI ${Fmt.int(lv.d("call_oi"))} · local " +
                "${Fmt.int(lv.d("local_support"))}–${Fmt.int(lv.d("local_resistance"))} (OI concentration, not confirmed S/R)",
                color = Pal.muted, fontSize = 11.sp)
        }
        Text("★ weighted PCR trend is the positioning measure in the read. OI up + premium down = possible writing; " +
            "up + up = possible buying. Not a forecast — confirm with spot, futures OI and IV.", color = Pal.faint, fontSize = 10.sp)
    }
}


/** signal_engine: Strong Bullish … Strong Bearish / No Trade, confidence and the evidence behind it. */
@Composable
private fun SignalCard(sg: org.json.JSONObject) {
    Section("Market signal · PCR + confirmation") {
        val cls = sg.s("class") ?: "No Trade"
        val col = if (cls.contains("Bullish")) Pal.up else if (cls.contains("Bearish")) Pal.down
            else if (cls == "Neutral") Pal.warn else Pal.muted
        Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(10.dp)) {
            Text(cls, color = col, fontWeight = FontWeight.Bold, fontSize = 20.sp)
            Text("confidence ${sg.optInt("confidence")}/100", color = Pal.muted, fontSize = 12.sp)
        }
        val agree = sg.arr("agree").let { a -> (0 until a.length()).map { a.getString(it) }.toSet() }
        sg.arr("evidence").objects().forEach { e ->
            val avail = e.optBoolean("available")
            val v = e.optInt("vote")
            val tag = when {
                !avail -> "— no data"
                e.s("key") == "vol" -> "participation"
                v > 0 -> "▲ bullish"
                v < 0 -> "▼ bearish"
                else -> "● neutral"
            }
            Row(Modifier.fillMaxWidth()) {
                Text((if ((e.s("name") ?: "") in agree) "✓ " else "   ") + (e.s("name") ?: ""), Modifier.weight(1.4f),
                    fontSize = 11.sp, fontWeight = FontWeight.Bold)
                Text(tag, Modifier.weight(1f), fontSize = 11.sp,
                    color = if (!avail) Pal.faint else if (v > 0 && e.s("key") != "vol") Pal.up
                        else if (v < 0) Pal.down else Pal.muted)
            }
            Text(e.s("text") ?: "", color = Pal.muted, fontSize = 10.sp)
        }
        sg.arr("reasons").let { a -> for (i in 0 until a.length()) Text("• ${a.getString(i)}", color = Pal.warn, fontSize = 11.sp) }
        Text("Confidence = share of the 5 evidence items agreeing (equal weights, uncalibrated) — not a probability. " +
            "Analysis, not advice.", color = Pal.faint, fontSize = 10.sp)
    }
}

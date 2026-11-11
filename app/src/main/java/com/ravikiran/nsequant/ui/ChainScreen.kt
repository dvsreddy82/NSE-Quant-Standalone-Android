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
import androidx.compose.foundation.clickable
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
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
import com.ravikiran.nsequant.data.strings
import org.json.JSONObject

private val CE = Color(0xFFEF4444)
private val PE = Color(0xFF22C55E)
private val ITM = Color(0x1AFACC15)

@Composable
fun ChainScreen() {
    var und by rememberSaveable { mutableStateOf("NIFTY") }
    var mode by rememberSaveable { mutableStateOf("2exp") }
    Column {
        Row(Modifier.padding(horizontal = 12.dp, vertical = 6.dp), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            listOf("NIFTY", "BANKNIFTY").forEach { u ->
                FilterChip(selected = und == u, onClick = { und = u }, label = { Text(u) })
            }
            Box(Modifier.weight(1f))
            FilterChip(selected = mode == "2exp", onClick = { mode = if (mode == "2exp") "chain" else "2exp" },
                label = { Text(if (mode == "2exp") "2 expiries" else "Full chain") })
        }
        if (mode == "2exp") {
            ExpiryCompare(und)
        } else {
            val st = rememberApi("/api/chain?und=$und&n=15", 8_000)
            Loading(st) { d -> if (d.s("underlying") == und) ChainBody(d) }
        }
    }
}

/** Call / put premiums of two expiries side by side, strikes around ATM — minimal on purpose. */
@Composable
private fun ExpiryCompare(und: String) {
    var e1 by rememberSaveable(und) { mutableStateOf("") }
    var e2 by rememberSaveable(und) { mutableStateOf("") }
    var mode by rememberSaveable(und) { mutableStateOf("") }        // "" = auto: my strikes when I hold positions
    val q = "/api/expiry_compare?und=$und&n=8" + (if (e1.isNotEmpty()) "&e1=$e1" else "") +
        (if (e2.isNotEmpty()) "&e2=$e2" else "") + (if (mode.isNotEmpty()) "&mode=$mode" else "")
    val st = rememberApi(q.replace(" ", "%20"), 30_000)
    Loading(st) { d ->
        if (d.s("und") != und) return@Loading
        val a = d.obj("e1") ?: JSONObject()
        val b = d.obj("e2") ?: JSONObject()
        val exps = d.arr("expiries").strings()
        val atm = d.d("atm")
        LazyColumn(contentPadding = PaddingValues(bottom = 12.dp)) {
            item {
                Section {
                    Text("$und ${Fmt.px(d.d("spot"))} · ATM ${Fmt.int(atm)}", fontWeight = FontWeight.Bold, fontSize = 17.sp)
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        val cur = d.s("mode") ?: "atm"
                        if (d.optBoolean("has_positions")) {
                            FilterChip(selected = cur == "mine", onClick = { mode = "mine" }, label = { Text("My strikes") })
                        }
                        FilterChip(selected = cur == "atm", onClick = { mode = "atm" }, label = { Text("Around ATM") })
                    }
                    Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                        ExpiryPick("Near", a, exps, Modifier.weight(1f)) { e1 = it }
                        ExpiryPick("Far", b, exps, Modifier.weight(1f)) { e2 = it }
                    }
                }
            }
            item {
                Row(Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 4.dp)) {
                    listOf("CE near", "CE far", "Strike", "PE near", "PE far").forEach {
                        Text(it, Modifier.weight(1f), color = Pal.muted, fontSize = 13.sp, textAlign = TextAlign.Center,
                            fontWeight = FontWeight.Bold)
                    }
                }
            }
            items(d.arr("rows").objects()) { r ->
                val isAtm = r.d("strike") == atm
                val held = r.arr("mine").objects().mapNotNull { m -> m.s("in")?.let { "${m.s("kind")}${it}" } }.toSet()
                Column(Modifier.fillMaxWidth().background(if (isAtm) Pal.panel2 else Color.Transparent)
                    .padding(horizontal = 12.dp, vertical = 6.dp)) {
                    Row(Modifier.fillMaxWidth()) {
                        val thin = r.arr("no_trades").strings().toSet()
                        val cell = @Composable { v: Double?, c: Color, key: String ->
                            val mine = key in held
                            val f = mapOf("CEe1" to "ce1", "CEe2" to "ce2", "PEe1" to "pe1", "PEe2" to "pe2")[key]
                            Text((v?.let { String.format("%,.1f", it) } ?: "—") + (if (f in thin) "*" else ""),
                                Modifier.weight(1f).background(if (mine) Pal.amberBg else Color.Transparent),
                                color = c, fontSize = 15.sp, textAlign = TextAlign.Center,
                                fontWeight = if (mine) FontWeight.Bold else FontWeight.Normal)
                        }
                        cell(r.d("ce1"), CE, "CEe1")
                        cell(r.d("ce2"), CE, "CEe2")
                        Text(Fmt.int(r.d("strike")), Modifier.weight(1f), fontSize = 15.sp, textAlign = TextAlign.Center,
                            fontWeight = FontWeight.Bold)
                        cell(r.d("pe1"), PE, "PEe1")
                        cell(r.d("pe2"), PE, "PEe2")
                    }
                    val mt = r.s("mine_text").orEmpty()
                    if (mt.isNotEmpty() || isAtm) {
                        Text(listOfNotNull(mt.takeIf { it.isNotEmpty() }?.let { "You hold: $it" }, if (isAtm) "ATM" else null)
                            .joinToString(" · "), color = if (mt.isNotEmpty()) Pal.warn else Pal.faint, fontSize = 12.sp,
                            textAlign = TextAlign.Center, modifier = Modifier.fillMaxWidth())
                    }
                }
            }
            item {
                Text("Last traded premiums · highlighted = you hold it · far − near is roughly the credit for " +
                    "rolling a short leg out at the same strike · * under 20 contracts traded today, price may be old · refreshes every 30 s",
                    color = Pal.faint, fontSize = 12.sp, modifier = Modifier.padding(12.dp))
            }
        }
    }
}

@Composable
private fun ExpiryPick(label: String, e: JSONObject, exps: List<String>, modifier: Modifier, onPick: (String) -> Unit) {
    var open by androidx.compose.runtime.remember { mutableStateOf(false) }
    Column(modifier.background(Pal.panel2).clickable { open = true }.padding(8.dp)) {
        Text("$label ▾", color = Pal.muted, fontSize = 12.sp)
        Text(e.s("expiry") ?: "—", fontWeight = FontWeight.Bold, fontSize = 15.sp)
        Text("${String.format("%.0f", e.d("dte") ?: 0.0)} d" +
            (e.d("straddle")?.let { " · straddle ${String.format("%,.0f", it)}" } ?: ""), color = Pal.muted, fontSize = 12.sp)
        DropdownMenu(expanded = open, onDismissRequest = { open = false }) {
            exps.forEach { x -> DropdownMenuItem(text = { Text(x) }, onClick = { open = false; onPick(x) }) }
        }
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

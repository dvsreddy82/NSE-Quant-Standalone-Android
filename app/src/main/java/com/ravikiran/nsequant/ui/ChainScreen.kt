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
                if (fresh.isNotEmpty()) Text("Fresh writing today: ${fresh.joinToString(" · ")}", color = Pal.muted, fontSize = 11.sp)
            }
        }
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

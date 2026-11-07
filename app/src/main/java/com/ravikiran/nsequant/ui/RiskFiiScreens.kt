package com.ravikiran.nsequant.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
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
import org.json.JSONArray
import org.json.JSONObject

// ============================================================ Writer risk monitor
@Composable
fun RiskScreen() {
    val st = rememberApi("/api/risk", 15_000)
    Loading(st) { d ->
        val idx = d.obj("indices") ?: JSONObject()
        LazyColumn(contentPadding = PaddingValues(vertical = 6.dp)) {
            item {
                Column(Modifier.padding(horizontal = 16.dp, vertical = 4.dp)) {
                    Text("Writer risk monitor", fontWeight = FontWeight.Bold, fontSize = 18.sp, color = Pal.text)
                    Text("Squeeze, put-flush, institutional-divergence and negative-gamma checks for NIFTY / BANKNIFTY " +
                        "writers, with prohibitions, safe zones, guidance for your legs and your book's risk" +
                        (d.d("updated")?.takeIf { it > 0 }?.let {
                            " · updated " + java.text.SimpleDateFormat("HH:mm:ss", java.util.Locale.US)
                                .format(java.util.Date((it * 1000).toLong()))
                        } ?: ""), color = Pal.muted, fontSize = 11.sp)
                }
            }
            if (idx.length() == 0) item {
                Text("Running the first risk check — this fills in within a minute or two.", color = Pal.muted,
                    modifier = Modifier.padding(16.dp))
            }
            d.obj("account")?.let { a -> item { AccountBlock(a) } }
            listOf("NIFTY", "BANKNIFTY").forEach { und ->
                val r = idx.obj(und) ?: return@forEach
                item { RiskBlock(und, r) }
                r.arr("books").objects().forEach { b -> item { BookBlock(b) } }
            }
            item {
                Text("Mechanical screens on exchange data using your rule set — not a forecast or advice.",
                    color = Pal.faint, fontSize = 10.sp, modifier = Modifier.padding(16.dp))
            }
        }
    }
}

@Composable
private fun AccountBlock(a: JSONObject) {
    Section("Account limits") {
        KV("Capital", "₹${Fmt.int(a.d("capital"))}")
        KV("Today's P&L (limit −₹${Fmt.int(a.d("daily_limit"))})", Fmt.rupees(a.d("day_pnl")), Pal.chg(a.d("day_pnl")))
        KV("Margin in use", a.d("margin_use_pct")?.let { String.format("%.0f%%", it) } ?: "—")
        a.obj("move_2pct")?.let { m ->
            KV("All books if index −2% / +2% now", "${Fmt.rupees(m.d("down"))} / ${Fmt.rupees(m.d("up"))}")
        }
        KV("Theta (per day) · Vega (per IV pt)", "${Fmt.rupees(a.d("theta_day"))} · ${Fmt.rupees(a.d("vega"))}")
        a.arr("alerts").objects().forEach { x -> AlertCard(x.s("level") ?: "", x.s("title") ?: "", listOfNotNull(x.s("text"))) }
    }
}

@Composable
private fun BookBlock(b: JSONObject) {
    Section("${b.s("und")} ${b.s("expiry")} · my book") {
        Text("${b.s("structure")} · ${b.d("dte")?.toInt()} DTE · credit ₹${Fmt.int(b.d("credit"))}", color = Pal.muted, fontSize = 11.sp)
        KV("P&L now", Fmt.rupees(b.d("pnl_now")), Pal.chg(b.d("pnl_now")))
        KV("Net delta", "${Fmt.int(b.d("delta"))} (${Fmt.signed(b.d("delta_lots"), 1)} lots) → ${Fmt.rupees(b.d("per_100"))}/+100")
        KV("Theta / day · Vega / IV pt", "${Fmt.rupees(b.d("theta"))} · ${Fmt.rupees(b.d("vega"))}")
        KV("Breakeven at expiry", b.arr("breakevens").doubles().joinToString(", ") { Fmt.int(it) }.ifEmpty { "—" })
        KV("Max profit / max loss", "${b.d("max_profit")?.let { Fmt.rupees(it) } ?: "unlimited"} / " +
            (b.d("max_loss")?.let { Fmt.rupees(it) } ?: "unlimited"))
        KV("Chance of profit at expiry", "${b.d("pop")?.toInt()}% · expected ${Fmt.rupees(b.d("expected_pnl"))}",
            if ((b.d("pop") ?: 0.0) >= 50) Pal.up else Pal.down)
        KV("At expiry: −1σ / unchanged / +1σ", "${Fmt.rupees(b.d("pnl_expiry_dn1s"))} / ${Fmt.rupees(b.d("pnl_expiry_spot"))} / " +
            Fmt.rupees(b.d("pnl_expiry_up1s")))
        Text("P&L if the index moves now (IV same)", color = Pal.accent, fontSize = 12.sp, fontWeight = FontWeight.Bold)
        val moves = b.arr("moves").doubles()
        val grid = b.arr("grid").objects().firstOrNull()?.arr("pnl")?.doubles() ?: emptyList()
        moves.zip(grid).chunked(3).forEach { row ->
            Row(Modifier.fillMaxWidth()) {
                row.forEach { (m, v) ->
                    Text("${String.format("%+.0f%%", m * 100)}: ${Fmt.rupees(v)}", Modifier.weight(1f), fontSize = 11.sp,
                        color = Pal.chg(v))
                }
            }
        }
    }
}

private fun pair(a: JSONArray?): Pair<String, String> =
    if (a == null || a.length() < 2) "—" to "" else (if (a.isNull(0)) "—" else Fmt.int(a.optDouble(0))) to a.optString(1)

@Composable
private fun RiskBlock(und: String, r: JSONObject) {
    val regime = r.s("regime") ?: ""
    val regC = when (regime) {
        "Squeeze in Progress" -> Pal.down; "Negative Gamma Trending" -> Pal.warn; else -> Pal.up
    }
    val bias = r.s("bias") ?: ""
    Section("$und · 1. Threat matrix") {
        KV("Market regime", regime, regC)
        KV("Current bias", bias, if (bias.startsWith("Bull")) Pal.up else if (bias.startsWith("Bear")) Pal.down else Pal.text)
        val (uk, uw) = pair(r.optJSONArray("danger_up"))
        val (dk, dw) = pair(r.optJSONArray("danger_down"))
        Text("Max danger (up): $uk CE — $uw", fontSize = 12.sp, color = Pal.down)
        Text("Max danger (down): $dk PE — $dw", fontSize = 12.sp, color = Pal.up)
        Text("Spot ${Fmt.px(r.d("spot"))} · ${r.s("expiry")} · ATM IV ${r.d("atm_iv")?.let { String.format("%.2f%%", it) } ?: "—"} · " +
            "VIX ${r.d("vix") ?: "—"} · 1σ ${Fmt.int(r.d("sigma"))} · walls ${Fmt.int(r.d("put_wall"))} PE / " +
            "${Fmt.int(r.d("call_wall"))} CE · GEX ${r.obj("gex")?.d("net_cr")?.let { String.format("%+.2f Cr/1%%", it) } ?: "—"}",
            color = Pal.muted, fontSize = 11.sp)
    }
    Section("$und · 2. Active alerts") {
        r.arr("alerts").objects().forEach { a ->
            AlertCard(a.s("level") ?: "", a.s("title") ?: "", a.arr("signals").strings(), a.s("action"))
        }
    }
    Section("$und · 3. Directives") {
        Text("Strict prohibitions", color = Pal.down, fontWeight = FontWeight.Bold, fontSize = 12.sp)
        r.arr("prohibitions").strings().forEach { Text("• $it", fontSize = 12.sp) }
        Text("Safe zones (beyond 0.10Δ and 1.5× expected move)", color = Pal.up, fontWeight = FontWeight.Bold, fontSize = 12.sp)
        r.arr("safe").strings().forEach { Text("• $it", fontSize = 12.sp) }
        Text("My positions", color = Pal.accent, fontWeight = FontWeight.Bold, fontSize = 12.sp)
        r.arr("positions").strings().forEach { Text("• $it", fontSize = 12.sp) }
    }
}

// ============================================================ FII / DII
@Composable
fun FiiScreen() {
    val st = rememberApi("/api/fii", 60_000)
    Loading(st) { d ->
        if (!d.b("available")) {
            Text("No FII/DII data yet — the first sync runs at start-up.", color = Pal.muted, modifier = Modifier.padding(16.dp))
            return@Loading
        }
        LazyColumn(contentPadding = PaddingValues(vertical = 6.dp)) {
            item { SetupBlock(d.obj("setup")) }
            item { ReadBlock(d) }
            item { ZonesBlock(d.obj("zones")) }
            item {
                Section("Signals") {
                    d.arr("components").objects().forEach { c ->
                        val sc = c.d("score") ?: 0.0
                        Column {
                            Row(Modifier.fillMaxWidth()) {
                                Text(c.s("label") ?: "", Modifier.weight(1f), fontSize = 12.sp, fontWeight = FontWeight.SemiBold)
                                Text(c.s("value") ?: "", fontSize = 12.sp)
                                Text("  " + String.format("%+.1f", sc), fontSize = 12.sp, fontWeight = FontWeight.Bold,
                                    color = if (sc > 0.25) Pal.up else if (sc < -0.25) Pal.down else Pal.muted)
                            }
                            Text(c.s("reading") ?: "", color = Pal.muted, fontSize = 11.sp)
                        }
                    }
                }
            }
            item {
                Section("Recent sessions") {
                    Row(Modifier.fillMaxWidth()) {
                        listOf("Date", "FII ₹cr", "DII ₹cr", "FII long", "PCR", "Score").forEach {
                            Text(it, Modifier.weight(1f), color = Pal.muted, fontSize = 10.sp)
                        }
                    }
                    d.arr("recent").objects().forEach { x ->
                        Row(Modifier.fillMaxWidth()) {
                            Text(x.s("date")?.substring(5, 10) ?: "", Modifier.weight(1f), fontSize = 11.sp)
                            Text(Fmt.signed(x.d("fii_cash"), 0), Modifier.weight(1f), fontSize = 11.sp, color = Pal.chg(x.d("fii_cash")))
                            Text(Fmt.signed(x.d("dii_cash"), 0), Modifier.weight(1f), fontSize = 11.sp, color = Pal.chg(x.d("dii_cash")))
                            Text(x.d("fii_long_pct")?.let { String.format("%.1f%%", it) } ?: "—", Modifier.weight(1f), fontSize = 11.sp)
                            Text(x.d("pcr")?.let { String.format("%.2f", it) } ?: "—", Modifier.weight(1f), fontSize = 11.sp)
                            Text(Fmt.signed(x.d("score"), 0), Modifier.weight(1f), fontSize = 11.sp, color = Pal.chg(x.d("score")))
                        }
                    }
                }
            }
            item {
                Text("Rule-based screening of published FII/DII data, not investment advice.", color = Pal.faint,
                    fontSize = 10.sp, modifier = Modifier.padding(16.dp))
            }
        }
    }
}

@Composable
private fun SetupBlock(s: JSONObject?) {
    s ?: return
    val dir = s.s("direction")
    Section("Evening setup · institutional squeeze loading") {
        if (dir != null) {
            val up = dir == "up"
            Text((if (up) "▲ UP" else "▼ DOWN") + " SETUP ACTIVE · ${s.s("date")?.take(10)} close",
                color = if (up) Pal.up else Pal.down, fontWeight = FontWeight.ExtraBold, fontSize = 15.sp,
                modifier = Modifier.background(if (up) Pal.greenBg else Pal.redBg, RoundedCornerShape(6.dp)).padding(8.dp))
        } else {
            Text("No setup for ${s.s("date")?.take(10)} (nearest: ${s.s("lean")})", color = Pal.muted)
        }
        s.arr("conditions").objects().forEach { c ->
            Text((if (c.b("ok")) "✅ " else "▫️ ") + "${c.s("name")}: ${c.s("value")}", fontSize = 12.sp)
        }
        val rec = s.obj("record")
        val g = rec?.obj(dir ?: s.s("lean") ?: "up")
        val all = rec?.obj("all")
        if (g != null && (g.d("n") ?: 0.0) > 0) {
            Text("After this setup (${g.d("n")?.toInt()} times) BANKNIFTY was up next session " +
                String.format("%.0f%% (avg %+.2f%%) vs %.0f%% across %d sessions.", g.d("up") ?: 0.0, g.d("avg") ?: 0.0,
                    all?.d("up") ?: 0.0, all?.d("n")?.toInt() ?: 0) +
                if ((g.d("n") ?: 0.0) < 15) " Small sample — a lean, not a forecast." else "", fontSize = 12.sp)
        }
        if (dir != null) Text(if (dir == "up") "Short calls are the exposed side — no naked calls overnight."
            else "Short puts are the exposed side — no naked puts overnight.", fontWeight = FontWeight.Bold, fontSize = 12.sp)
    }
}

@Composable
private fun ReadBlock(d: JSONObject) {
    val sc = d.d("score") ?: 0.0
    val pb = d.obj("playbook") ?: JSONObject()
    Section("Positioning read → writing posture") {
        Row {
            Text(d.s("bias") ?: "", color = if (sc >= 10) Pal.up else if (sc <= -10) Pal.down else Pal.text,
                fontWeight = FontWeight.ExtraBold, fontSize = 22.sp)
            Text("  ${String.format("%+.0f", sc)} / 100 · ${d.s("conviction")}", color = Pal.muted, fontSize = 13.sp,
                modifier = Modifier.padding(top = 8.dp))
        }
        Box(Modifier.fillMaxWidth().height(6.dp).background(Pal.border)) {
            Box(Modifier.fillMaxWidth(((sc + 100) / 200).toFloat().coerceIn(0.01f, 1f)).height(6.dp)
                .background(if (sc >= 0) Pal.up else Pal.down))
        }
        Text("Writing side: ${pb.s("side")} — ${pb.s("skew")}", fontSize = 12.sp, fontWeight = FontWeight.SemiBold)
        Text("Fits: " + pb.arr("structures").strings().joinToString(" · "), fontSize = 12.sp)
        Text("Avoid: ${pb.s("avoid")}", color = Color(0xFFFDBA74), fontSize = 12.sp)
        d.arr("warnings").strings().forEach { AlertCard("AMBER", "Warning", listOf(it)) }
    }
}

@Composable
private fun ZonesBlock(z: JSONObject?) {
    z ?: return
    Section("Strike zones (from the live chain)") {
        listOf("NIFTY", "BANKNIFTY").forEach { u ->
            val x = z.obj(u)
            if (x == null) {
                Text("$u: option chain not loaded yet", color = Pal.muted, fontSize = 12.sp)
            } else {
                Column(verticalArrangement = Arrangement.spacedBy(2.dp)) {
                    Text("$u ${Fmt.int(x.d("spot"))} · ${x.s("expiry")} · 1σ ${Fmt.int(x.d("sigma"))}", fontWeight = FontWeight.Bold,
                        fontSize = 12.sp)
                    Text(if (x.b("use_put")) "Put: ${Fmt.int(x.d("put"))} PE (${Fmt.pct(x.d("put_dist"))}) · wing ${Fmt.int(x.d("put_hedge"))}"
                        else "Put: skip / far only", color = Pal.up, fontSize = 12.sp)
                    Text(if (x.b("use_call")) "Call: ${Fmt.int(x.d("call"))} CE (${Fmt.pct(x.d("call_dist"))}) · wing ${Fmt.int(x.d("call_hedge"))}"
                        else "Call: skip / far only", color = Pal.down, fontSize = 12.sp)
                }
            }
        }
    }
}

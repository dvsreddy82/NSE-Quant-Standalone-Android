package com.ravikiran.nsequant.ui

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalUriHandler
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.ravikiran.nsequant.data.Fmt
import com.ravikiran.nsequant.data.arr
import com.ravikiran.nsequant.data.b
import com.ravikiran.nsequant.data.d
import com.ravikiran.nsequant.data.obj
import com.ravikiran.nsequant.data.objects
import com.ravikiran.nsequant.data.s
import com.ravikiran.nsequant.data.strings
import org.json.JSONObject

@Composable
fun HomeScreen(monitorOn: Boolean, onMonitor: (Boolean) -> Unit) {
    val ticker = rememberApi("/api/ticker", 3_000)
    val overview = rememberApi("/api/overview", 10_000)
    val alerts = rememberApi("/api/alerts", 20_000)
    val pos = rememberApi("/api/positions", 30_000)
    val news = rememberApi("/api/news?hours=12&limit=40", 60_000)
    val signals = rememberApi("/api/signals", 60_000)
    val global = rememberApi("/api/global", 120_000)
    val uri = LocalUriHandler.current

    LazyColumn(contentPadding = androidx.compose.foundation.layout.PaddingValues(vertical = 6.dp)) {
        item {
            Loading(ticker) { t -> TickerBlock(t) }
        }
        item {
            Section {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Column(Modifier.weight(1f)) {
                        Text("Market monitor", fontWeight = FontWeight.Bold)
                        Text("Live BANKNIFTY in the notification bar + alerts: risk, level breaks, " +
                            "15-min direction calls, global moves, news",
                            color = Pal.muted, fontSize = 14.sp)
                    }
                    Switch(checked = monitorOn, onCheckedChange = onMonitor)
                }
            }
        }
        item {
            signals.data?.let { s -> SignalsBlock(s) }
        }
        item {
            alerts.data?.let { a ->
                val items = a.arr("alerts").objects().sortedByDescending { it.s("at") ?: "" }    // newest first
                Section("Active alerts (${items.size}) · newest first") {
                    if (items.isEmpty()) Text("None right now.", color = Pal.muted, fontSize = 14.sp)
                    items.forEach { x ->
                        val wr = x.s("level") == "WRITER"
                        AlertCard(if (wr) x.s("severity") ?: "AMBER" else x.s("level") ?: "",
                            (if (wr) "✍ " else "") + (x.s("title") ?: ""), listOfNotNull(x.s("text")),
                            listOfNotNull(hhmm(x.s("at")), x.s("source")).joinToString(" · "))
                    }
                }
            }
        }
        item {
            news.data?.let { n ->
                val items = n.arr("items").objects().filter { it.s("impact") != "LOW" }.take(12)
                Section("Market news (last 12 h)") {
                    if (items.isEmpty()) Text("No high- or medium-impact headlines.", color = Pal.muted, fontSize = 14.sp)
                    items.forEach { x -> NewsRow(x) { x.s("link")?.let { runCatching { uri.openUri(it) } } } }
                }
            }
        }
        item {
            pos.data?.let { p -> PositionsBlock(p) }
        }
        item {
            global.data?.let { g -> GlobalBlock(g) }
        }
        item {
            overview.data?.let { o ->
                Section("Indices · ${o.s("source") ?: ""}") {
                    o.arr("indices").objects().forEach { r ->
                        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                            Text(r.s("name") ?: "", Modifier.weight(1f), fontSize = 15.sp)
                            Text(Fmt.px(r.d("last")), fontWeight = FontWeight.SemiBold, fontSize = 15.sp)
                            Spacer(Modifier.width(10.dp))
                            Text(Fmt.pct(r.d("pct")), color = Pal.chg(r.d("pct")), fontSize = 15.sp,
                                modifier = Modifier.width(64.dp))
                        }
                    }
                    o.obj("fii")?.let { f ->
                        if (f.has("score")) {
                            Text("FII/DII ${f.s("as_of")?.take(10)}: FII ${Fmt.rupees(f.d("fii_cash"))} cr · DII " +
                                "${Fmt.rupees(f.d("dii_cash"))} cr · read ${f.s("bias")} (${Fmt.signed(f.d("score"), 0)})",
                                color = Pal.muted, fontSize = 13.sp)
                        }
                    }
                }
            }
        }
    }
}

/** "2026-11-06T16:25:00+05:30" → "16:25" today, "06 Nov 16:25" otherwise (the engine sends IST). */
fun hhmm(iso: String?): String? {
    if (iso == null || iso.length < 16) return null
    val today = java.text.SimpleDateFormat("yyyy-MM-dd", java.util.Locale.US).format(java.util.Date())
    val months = listOf("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
    val t = iso.substring(11, 16)
    return if (iso.startsWith(today)) t else "${iso.substring(8, 10)} ${months[iso.substring(5, 7).toInt() - 1]} $t"
}

@Composable
private fun NewsRow(x: JSONObject, onOpen: () -> Unit) {
    val impact = x.s("impact") ?: ""
    val (fg, bg) = if (impact == "HIGH") Pal.down to Pal.redBg else Pal.warn to Pal.amberBg
    Column(Modifier.fillMaxWidth().clickable(onClick = onOpen).padding(vertical = 4.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(6.dp)) {
            Badge(impact, fg, bg)
            val tone = x.s("tone")
            Text(when (tone) { "positive" -> "▲"; "negative" -> "▼"; else -> "•" },
                color = when (tone) { "positive" -> Pal.up; "negative" -> Pal.down; else -> Pal.muted }, fontSize = 14.sp)
            Text(x.s("published")?.let { if (it.length >= 16) it.substring(11, 16) + " UTC" else it } ?: "",
                color = Pal.faint, fontSize = 12.sp)
            Text(x.arr("tags").strings().joinToString(" · "), color = Pal.faint, fontSize = 12.sp)
        }
        Text((if (x.b("alerted")) "🔔 " else "") + (x.s("title") ?: ""), fontSize = 15.sp,
            fontWeight = if (impact == "HIGH") FontWeight.SemiBold else FontWeight.Normal)
        Text(x.arr("sources").strings().joinToString(", "), color = Pal.faint, fontSize = 12.sp)
    }
}

@Composable
private fun TickerBlock(t: JSONObject) {
    Section {
        t.arr("quotes").objects().forEachIndexed { i, q ->
            val big = i == 0
            Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.Bottom) {
                Column(Modifier.weight(1f)) {
                    Text(q.s("name") ?: "", color = Pal.muted, fontSize = if (big) 14.sp else 13.sp)
                    Text(Fmt.px(q.d("last")), fontSize = if (big) 28.sp else 18.sp, fontWeight = FontWeight.ExtraBold)
                }
                Column(horizontalAlignment = Alignment.End) {
                    Text("${Fmt.signed(q.d("chg"))}  ${Fmt.pct(q.d("pct"))}", color = Pal.chg(q.d("chg")),
                        fontWeight = FontWeight.Bold, fontSize = if (big) 16.sp else 15.sp)
                    if (q.d("high") != null) {
                        Text("H ${Fmt.px1(q.d("high"))} · L ${Fmt.px1(q.d("low"))}", color = Pal.muted, fontSize = 13.sp)
                    }
                }
            }
        }
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
            t.d("book_pnl")?.let {
                Badge("Open P&L ${Fmt.rupees(it)}", if (it >= 0) Pal.up else Pal.down, Pal.panel2)
            }
            val red = t.d("red")?.toInt() ?: 0
            if (red > 0) Badge("⚠ $red RED", Pal.down, Pal.redBg)
            when (t.s("setup")) {
                "up" -> Badge("⚑▲ setup", Pal.up, Pal.greenBg)
                "down" -> Badge("⚑▼ setup", Pal.down, Pal.redBg)
            }
        }
        Text(t.s("source") ?: "", color = Pal.faint, fontSize = 12.sp)
    }
}

@Composable
private fun PositionsBlock(p: JSONObject) {
    if (p.b("loading")) {
        Section("My positions") { Text("Loading your positions…", color = Pal.muted, fontSize = 14.sp) }
        return
    }
    val groups = p.arr("positions").objects()
    val today = p.obj("today")
    Section("My positions" + (p.s("synced_at")?.let { " · synced ${it.substring(11, 16)}" } ?: "")) {
        today?.let { t ->
            KV("Open P&L (all positions)", Fmt.rupees(t.d("open")), Pal.chg(t.d("open")))
        }
        p.obj("limits")?.let { l ->
            if (l.d("margin_used") != null) KV("Margin used / available", "₹${Fmt.int(l.d("margin_used"))} / ₹${Fmt.int(l.d("available"))}")
        }
        if (groups.isEmpty()) Text("No open positions.", color = Pal.muted, fontSize = 14.sp)
        groups.forEach { g ->
            Column(Modifier.fillMaxWidth().padding(top = 6.dp)) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text("${g.s("und")} ${g.s("expiry")?.take(10)} · ${g.s("structure")}", Modifier.weight(1f),
                        fontWeight = FontWeight.Bold, fontSize = 15.sp)
                    Text(Fmt.rupees(g.d("open_pnl")), color = Pal.chg(g.d("open_pnl")), fontWeight = FontWeight.Bold)
                }
                Text("Credit ${Fmt.rupees(g.d("credit")).removePrefix("+")} · captured " +
                    (g.d("captured")?.let { String.format("%.0f%%", it * 100) } ?: "—") +
                    (g.d("dte")?.let { " · ${it.toInt()} DTE" } ?: ""), color = Pal.muted, fontSize = 13.sp)
                g.arr("alerts").objects().forEach { a ->
                    val lv = when (a.s("level")) { "stop" -> "RED"; "warn" -> "AMBER"; "book" -> "GREEN"; else -> "" }
                    Spacer(Modifier.padding(2.dp))
                    AlertCard(lv, when (lv) { "RED" -> "EXIT"; "AMBER" -> "WATCH"; else -> "BOOK" }, listOf(a.s("text") ?: ""))
                }
                g.arr("legs").objects().filter { (it.d("qty") ?: 0.0) != 0.0 }.forEach { l ->
                    Row(Modifier.fillMaxWidth().padding(top = 2.dp)) {
                        Text(l.s("tsym") ?: "", Modifier.weight(1f), fontSize = 13.sp, color = Pal.muted)
                        Text("${l.d("qty")?.toInt()} @ ${Fmt.px(l.d("avg"))} → ${Fmt.px(l.d("ltp"))}", fontSize = 13.sp)
                        Spacer(Modifier.width(8.dp))
                        Text(Fmt.rupees(l.d("mtm")), color = Pal.chg(l.d("mtm")), fontSize = 13.sp,
                            modifier = Modifier.width(70.dp))
                    }
                }
            }
        }
    }
}

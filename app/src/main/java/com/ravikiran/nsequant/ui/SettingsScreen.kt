package com.ravikiran.nsequant.ui

import android.content.Intent
import android.net.Uri
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.FilterChip
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.ravikiran.nsequant.data.Api
import com.ravikiran.nsequant.data.Prefs
import com.ravikiran.nsequant.data.arr
import com.ravikiran.nsequant.data.b
import com.ravikiran.nsequant.data.objects
import com.ravikiran.nsequant.data.s
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import org.json.JSONObject

@Composable
fun SettingsScreen(onDone: () -> Unit) {
    val ctx = LocalContext.current
    val scope = rememberCoroutineScope()
    var st by remember { mutableStateOf<JSONObject?>(null) }
    var userId by remember { mutableStateOf("") }
    var clientId by remember { mutableStateOf("") }
    var secret by remember { mutableStateOf("") }
    var totpKey by remember { mutableStateOf("") }
    var paste by remember { mutableStateOf("") }
    var msg by remember { mutableStateOf<String?>(null) }
    var events by remember { mutableStateOf<List<JSONObject>>(emptyList()) }
    var loaded by remember { mutableStateOf(false) }
    LaunchedEffect(Unit) {
        while (true) {
            try {
                val s = Api.act(ctx, "shoonya_settings")
                st = s
                if (!loaded) { userId = s.s("user_id") ?: ""; clientId = s.s("client_id") ?: ""; loaded = true }
                events = Api.act(ctx, "events").arr("events").objects()
            } catch (_: Exception) {
            }
            delay(1_000)
        }
    }
    Column(Modifier.fillMaxWidth().verticalScroll(rememberScrollState()).padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp)) {
        Text("Shoonya (real-time data, read-only)", fontWeight = FontWeight.Bold, fontSize = 18.sp)
        st?.let { s ->
            val state = s.s("state")
            Text("● ${s.s("text")}", color = when (state) { "live" -> Pal.up; "degraded", "connecting" -> Pal.warn; else -> Pal.muted },
                fontWeight = FontWeight.Bold, fontSize = 13.sp)
            s.s("error")?.takeIf { it.isNotBlank() }?.let { Text("Last error: $it", color = Pal.down, fontSize = 12.sp) }
        }
        Text("This app talks to Shoonya itself, only in market hours (9:00 am – 3:40 pm). Outside them, or when " +
            "Shoonya isn't streaming, it uses NSE and Yahoo. Shoonya allows ONE live session per login: while this " +
            "phone is connected, the Mac app's live feed drops (and the other way round) — run one at a time.",
            color = Pal.muted, fontSize = 12.sp)
        Text("On trade.shoonya.com → Profile → API Key, copy the Client ID and Secret code, and set the redirect " +
            "URL to the address below (the same one the Mac app uses). Your password and TOTP are typed on " +
            "Shoonya's own page in the browser — never in this app.", color = Pal.muted, fontSize = 12.sp)
        Text(st?.s("redirect_url") ?: "http://127.0.0.1:8765/shoonya/callback", fontSize = 12.sp, color = Pal.accent)
        OutlinedTextField(userId, { userId = it }, Modifier.fillMaxWidth(), label = { Text("User ID") }, singleLine = true)
        OutlinedTextField(clientId, { clientId = it }, Modifier.fillMaxWidth(), label = { Text("Client ID (API key)") },
            singleLine = true)
        OutlinedTextField(secret, { secret = it }, Modifier.fillMaxWidth(), singleLine = true,
            label = { Text(if (st?.b("has_secret") == true) "Secret code (saved — type to replace)" else "Secret code") },
            visualTransformation = PasswordVisualTransformation())
        OutlinedTextField(totpKey, { totpKey = it }, Modifier.fillMaxWidth(), singleLine = true,
            label = { Text(if (st?.b("has_totp") == true) "TOTP key (saved — optional)" else "TOTP key (optional: shows the code)") },
            visualTransformation = PasswordVisualTransformation())
        Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
            OutlinedButton(onClick = {
                scope.launch {
                    val p = JSONObject().put("user_id", userId).put("client_id", clientId).put("secret", secret)
                    if (totpKey.isNotBlank()) p.put("totp_key", totpKey)
                    val r = Api.act(ctx, "shoonya_save", p)
                    msg = r.s("error") ?: "Saved"
                    secret = ""; totpKey = ""
                }
            }) { Text("Save") }
            Button(onClick = {
                scope.launch {
                    val r = Api.act(ctx, "shoonya_signin")
                    val url = r.s("url")
                    if (url != null) {
                        msg = "Sign in on Shoonya's page; you'll come back here automatically."
                        ctx.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url)).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
                    } else msg = r.s("error")
                }
            }) { Text("Sign in to Shoonya") }
        }
        st?.s("totp")?.let { code ->
            Text("TOTP for Shoonya's page: ${code.take(3)} ${code.drop(3)} · ${st?.optInt("totp_left")}s left",
                fontWeight = FontWeight.Bold, fontSize = 16.sp)
        }
        if (st?.b("waiting") == true) {
            Text("Waiting for Shoonya to send you back… If the browser shows an error page after signing in, copy " +
                "its whole address and paste it here:", color = Pal.muted, fontSize = 12.sp)
            OutlinedTextField(paste, { paste = it }, Modifier.fillMaxWidth(), singleLine = true,
                label = { Text("http://127.0.0.1:8765/shoonya/callback?code=…") })
            OutlinedButton(onClick = {
                scope.launch { msg = Api.act(ctx, "shoonya_code", JSONObject().put("text", paste)).s("error") ?: "Connecting…" }
            }) { Text("Use pasted address") }
        }
        Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
            OutlinedButton(onClick = { scope.launch { Api.act(ctx, "shoonya_disconnect"); msg = "Disconnected for today" } }) {
                Text("Disconnect")
            }
            OutlinedButton(onClick = onDone) { Text("Done") }
        }
        msg?.let { Text(it, fontSize = 13.sp) }

        Text("Status bar & lock screen", fontWeight = FontWeight.Bold, fontSize = 18.sp)
        Text("With Market monitor on, the live price is drawn into the status-bar icon, so it stays visible at " +
            "the top of the screen and on the lock screen. Pick what it shows:", color = Pal.muted, fontSize = 13.sp)
        var iconIndex by remember { mutableStateOf(Prefs.iconIndex(ctx)) }
        var iconMode by remember { mutableStateOf(Prefs.iconMode(ctx)) }
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            listOf("NIFTY BANK" to "BANKNIFTY", "^NSEI" to "NIFTY").forEach { (k, label) ->
                FilterChip(selected = iconIndex == k, onClick = { iconIndex = k; Prefs.setIcon(ctx, iconIndex, iconMode) },
                    label = { Text(label) })
            }
        }
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            listOf("both" to "57.8K / +0.62", "pct" to "% only", "points" to "Points").forEach { (k, label) ->
                FilterChip(selected = iconMode == k, onClick = { iconMode = k; Prefs.setIcon(ctx, iconIndex, iconMode) },
                    label = { Text(label) })
            }
        }
        Text("Keep Market monitor on so the engine keeps running in the background (Android stops apps that aren't " +
            "in use). Allow the app to run in the background in battery settings for alerts while the screen is off.",
            color = Pal.faint, fontSize = 11.sp)
        LockScreenCheck()
        AlertSelection()

        Text("Engine activity", fontWeight = FontWeight.Bold, fontSize = 18.sp)
        events.take(12).forEach { e ->
            Text("${e.s("at")?.substring(11, 19)}  ${e.s("text")}", color = Pal.muted, fontSize = 11.sp)
        }
        Text("Read-only: this app never places orders. Analytics, not investment advice.", color = Pal.faint, fontSize = 11.sp)
    }
}

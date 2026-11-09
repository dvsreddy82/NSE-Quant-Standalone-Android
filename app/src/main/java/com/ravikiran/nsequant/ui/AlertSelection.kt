package com.ravikiran.nsequant.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.ravikiran.nsequant.data.Api
import com.ravikiran.nsequant.data.Prefs
import com.ravikiran.nsequant.data.arr
import com.ravikiran.nsequant.data.objects
import com.ravikiran.nsequant.data.s
import org.json.JSONObject

/**
 * Which alert categories notify on this phone.  The list comes from /api/alerts ("categories"), the same one
 * the Mac's Alert settings page uses; this phone keeps its own choice.  Switched-off alerts still show on
 * Home and Signals, they just don't notify.
 */
@Composable
fun AlertSelection() {
    val ctx = LocalContext.current
    var cats by remember { mutableStateOf<List<JSONObject>>(emptyList()) }
    var off by remember { mutableStateOf(Prefs.alertsOff(ctx)) }
    var err by remember { mutableStateOf<String?>(null) }
    LaunchedEffect(Unit) {
        try {
            cats = Api.get(ctx, "/api/alerts").arr("categories").objects()
            if (cats.isEmpty()) err = "Update the app's engine / Mac app to choose alert types."
        } catch (e: Exception) {
            err = "Couldn't load the alert list: ${e.message}"
        }
    }
    Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
        Text("Alert notifications", fontWeight = FontWeight.Bold, fontSize = 18.sp)
        Text("Choose which alerts notify this phone. Switched-off alerts still appear on Home and Signals.",
            color = Pal.muted, fontSize = 12.sp)
        err?.let { Text(it, color = Pal.warn, fontSize = 12.sp) }
        if (cats.isNotEmpty()) {
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                TextButton(onClick = { Prefs.setAlertsOff(ctx, emptySet()); off = emptySet() }) { Text("All on") }
                TextButton(onClick = {
                    val all = cats.mapNotNull { it.s("key") }.toSet(); Prefs.setAlertsOff(ctx, all); off = all
                }) { Text("All off") }
                Spacer1()
                Text("${cats.size - off.size} of ${cats.size} on", color = Pal.faint, fontSize = 11.sp,
                    modifier = Modifier.align(Alignment.CenterVertically))
            }
        }
        var group: String? = null
        cats.forEach { c ->
            val key = c.s("key") ?: return@forEach
            if (c.s("group") != group) {
                group = c.s("group")
                Text(group ?: "", color = Pal.accent, fontWeight = FontWeight.Bold, fontSize = 13.sp,
                    modifier = Modifier.padding(top = 8.dp))
            }
            Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                Column(Modifier.weight(1f)) {
                    Text(c.s("label") ?: key, fontSize = 13.sp)
                    Text(c.s("help") ?: "", color = Pal.muted, fontSize = 11.sp)
                }
                Switch(checked = key !in off, onCheckedChange = { on ->
                    Prefs.setAlertOn(ctx, key, on); off = Prefs.alertsOff(ctx)
                })
            }
        }
    }
}

@Composable
private fun Spacer1() = androidx.compose.foundation.layout.Spacer(Modifier.padding(horizontal = 4.dp))

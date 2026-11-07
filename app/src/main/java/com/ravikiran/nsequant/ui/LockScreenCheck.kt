package com.ravikiran.nsequant.ui

import android.annotation.SuppressLint
import android.app.ActivityManager
import android.app.Notification
import android.app.NotificationManager
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.PowerManager
import android.provider.Settings
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.app.NotificationManagerCompat
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.compose.LocalLifecycleOwner
import com.ravikiran.nsequant.data.Prefs
import com.ravikiran.nsequant.monitor.MonitorService

/** One lock-screen requirement: did it pass, what it means, and how to fix it. */
private data class Check(val ok: Boolean?, val title: String, val detail: String, val fix: (() -> Unit)? = null,
                         val fixLabel: String = "Fix")

/**
 * Why the price may not show on the lock screen, checked on this phone: notifications allowed, the
 * Live ticker channel (blocked / silent / hidden on the lock screen), the phone-wide lock-screen
 * setting, battery optimisation and background restriction (which stop the monitor once the
 * phone is locked), and whether the monitor is running.  Re-checked when you come back from a
 * settings page.  Below that, the steps for this phone's brand, since Tecno / Infinix, Xiaomi,
 * Vivo, Oppo and Samsung each hide lock-screen notifications in their own menus.
 */
@Composable
fun LockScreenCheck() {
    val ctx = LocalContext.current
    var tick by remember { mutableIntStateOf(0) }
    val owner = LocalLifecycleOwner.current
    DisposableEffect(owner) {
        val obs = LifecycleEventObserver { _, e -> if (e == Lifecycle.Event.ON_RESUME) tick++ }
        owner.lifecycle.addObserver(obs)
        onDispose { owner.lifecycle.removeObserver(obs) }
    }
    val checks = remember(tick) { checks(ctx) }
    Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Text("Price on the lock screen — check", fontWeight = FontWeight.Bold, fontSize = 18.sp)
        val bad = checks.count { it.ok == false }
        Text(if (bad == 0) "✅ Everything this app can check is set up. If the lock screen still shows only the time, " +
            "follow the steps for your phone below." else "❌ $bad setting(s) below hide the price or stop the monitor " +
            "when the phone locks. Tap Fix, change the setting, then come back.",
            color = if (bad == 0) Pal.up else Pal.down, fontSize = 13.sp)
        checks.forEach { c ->
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Text(when (c.ok) { true -> "✅"; false -> "❌"; null -> "•" }, fontSize = 14.sp)
                Column(Modifier.weight(1f)) {
                    Text(c.title, fontWeight = FontWeight.Bold, fontSize = 13.sp)
                    Text(c.detail, color = Pal.muted, fontSize = 12.sp)
                }
                if (c.fix != null && c.ok != true) {
                    OutlinedButton(onClick = c.fix) { Text(c.fixLabel, fontSize = 12.sp) }
                }
            }
        }
        Text("Steps for your phone (${Build.MANUFACTURER} ${Build.MODEL})", fontWeight = FontWeight.Bold,
            fontSize = 14.sp, modifier = Modifier.padding(top = 4.dp))
        oemSteps().forEach { Text("• $it", color = Pal.muted, fontSize = 12.sp) }
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            OutlinedButton(onClick = { open(ctx, appNotifications(ctx)) }) { Text("App notifications", fontSize = 12.sp) }
            OutlinedButton(onClick = { open(ctx, appInfo(ctx)) }) { Text("App info", fontSize = 12.sp) }
        }
    }
}

private fun checks(ctx: Context): List<Check> {
    val nm = ctx.getSystemService(NotificationManager::class.java)
    val out = mutableListOf<Check>()
    val enabled = NotificationManagerCompat.from(ctx).areNotificationsEnabled()
    out += Check(enabled, "Notifications allowed",
        if (enabled) "The app may post notifications." else "Notifications are off for this app — nothing can show.",
        { open(ctx, appNotifications(ctx)) })
    val ch = nm.getNotificationChannel(MonitorService.TICKER_CHANNEL)
    if (ch == null) {
        out += Check(null, "Live ticker channel", "Turn Market monitor on (Home) to create it.")
    } else {
        val imp = ch.importance
        out += Check(imp >= NotificationManager.IMPORTANCE_DEFAULT, "Live ticker importance",
            when {
                imp == NotificationManager.IMPORTANCE_NONE -> "The Live ticker category is turned off."
                imp < NotificationManager.IMPORTANCE_DEFAULT -> "Set to silent / minimised — most phones hide silent " +
                    "notifications on the lock screen. Set it to Default (it stays silent: the channel has no sound)."
                else -> "Default — shown on the lock screen and in the status bar."
            }, { open(ctx, channelSettings(ctx, MonitorService.TICKER_CHANNEL)) })
        val vis = ch.lockscreenVisibility
        out += Check(vis != Notification.VISIBILITY_SECRET, "Live ticker on the lock screen",
            if (vis == Notification.VISIBILITY_SECRET) "This category is set to \"Don't show on lock screen\"."
            else "Allowed for this category.", { open(ctx, channelSettings(ctx, MonitorService.TICKER_CHANNEL)) })
    }
    val lockAll = secureInt(ctx, "lock_screen_show_notifications")
    out += Check(lockAll?.let { it != 0 }, "Phone-wide lock-screen notifications",
        when (lockAll) {
            null -> "This phone doesn't let apps read it — check it with the steps below."
            0 -> "Notifications on the lock screen are turned off for the whole phone."
            else -> "On."
        }, { open(ctx, Intent(Settings.ACTION_SETTINGS)) }, "Settings")
    val pm = ctx.getSystemService(PowerManager::class.java)
    val unrestricted = pm.isIgnoringBatteryOptimizations(ctx.packageName)
    out += Check(unrestricted, "Battery: not optimised",
        if (unrestricted) "The phone won't put the monitor to sleep when locked."
        else "Battery optimisation stops the monitor a few minutes after the phone locks — the ticker then " +
            "disappears from the lock screen.", { requestBattery(ctx) }, "Allow")
    if (Build.VERSION.SDK_INT >= 28) {
        val restricted = ctx.getSystemService(ActivityManager::class.java).isBackgroundRestricted
        out += Check(!restricted, "Background activity allowed",
            if (restricted) "Battery usage is set to \"Restricted\" — the app can't run in the background."
            else "Not restricted.", { open(ctx, appInfo(ctx)) }, "App info")
    }
    val on = Prefs.monitorOn(ctx)
    out += Check(on && MonitorService.isRunning, "Market monitor running",
        when {
            !on -> "Market monitor is off — turn it on from Home."
            !MonitorService.isRunning -> "Turned on, but the phone stopped it. Fix the battery items above, then " +
                "toggle it off and on."
            else -> "Running — the ticker updates every 5 s."
        })
    return out
}

private fun oemSteps(): List<String> {
    val m = Build.MANUFACTURER.lowercase()
    return when {
        m.contains("tecno") || m.contains("infinix") || m.contains("itel") || m.contains("transsion") -> listOf(
            "Settings → Notifications & status bar → Lock screen notifications → Show all notifications (not \"Hide\" or \"Icons only\").",
            "Settings → Notifications & status bar → App notifications → this app → turn on \"Lock screen\" and allow \"Live ticker\".",
            "Phone Master → Toolbox → Auto-start management → allow this app; Battery → App power saving → this app → No restriction.",
            "Open Recent apps, pull this app's card down (or tap the lock icon) so cleaning doesn't close it.")
        m.contains("xiaomi") || m.contains("redmi") || m.contains("poco") -> listOf(
            "Settings → Notifications & status bar → App notifications → this app → turn on \"Lock screen notifications\" (off by default for new apps).",
            "Settings → Apps → Manage apps → this app → Autostart ON; Battery saver → No restrictions.",
            "Recent apps → long-press this app → Lock.")
        m.contains("vivo") || m.contains("iqoo") -> listOf(
            "Settings → Notifications & status bar → this app → turn on \"Lock screen\" (and \"Show on lock screen\" for Live ticker).",
            "Settings → Battery → Background power consumption management → this app → Allow high background power use.",
            "i Manager → App manager → Auto-start → allow this app.")
        m.contains("oppo") || m.contains("realme") || m.contains("oneplus") -> listOf(
            "Settings → Notifications & status bar → Manage notifications → this app → Lock screen → Show.",
            "Settings → Battery → App battery management (or Apps → this app → Battery usage) → Allow background activity, Allow auto launch.",
            "Recent apps → tap ⋮ on this app's card → Lock.")
        m.contains("samsung") -> listOf(
            "Settings → Lock screen and AOD → Notifications → On, and View style → Details (\"Icons only\" hides the price).",
            "Settings → Battery → Background usage limits → Never auto sleeping apps → add this app.",
            "On the Always On Display only icons show — the price icon is the status-bar icon.")
        else -> listOf(
            "Settings → Notifications → Lock screen (or Notifications on lock screen) → Show all notification content.",
            "Settings → Apps → this app → Battery → Unrestricted.")
    }
}

@SuppressLint("BatteryLife")
private fun requestBattery(ctx: Context) {
    val i = Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:${ctx.packageName}"))
    if (!open(ctx, i)) open(ctx, Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS))
}

private fun secureInt(ctx: Context, key: String): Int? = try {
    Settings.Secure.getInt(ctx.contentResolver, key)
} catch (_: Exception) {
    null
}

private fun appNotifications(ctx: Context) = Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS)
    .putExtra(Settings.EXTRA_APP_PACKAGE, ctx.packageName)

private fun channelSettings(ctx: Context, id: String) = Intent(Settings.ACTION_CHANNEL_NOTIFICATION_SETTINGS)
    .putExtra(Settings.EXTRA_APP_PACKAGE, ctx.packageName).putExtra(Settings.EXTRA_CHANNEL_ID, id)

private fun appInfo(ctx: Context) = Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:${ctx.packageName}"))

private fun open(ctx: Context, i: Intent): Boolean = try {
    ctx.startActivity(i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
    true
} catch (_: Exception) {
    false
}

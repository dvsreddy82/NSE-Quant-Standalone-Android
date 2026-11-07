package com.ravikiran.nsequant

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Home
import androidx.compose.material.icons.filled.Info
import androidx.compose.material.icons.filled.Place
import androidx.compose.material.icons.automirrored.filled.List
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material.icons.filled.Star
import androidx.compose.material.icons.filled.Warning
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.NavigationBarItemDefaults
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.TopAppBarDefaults
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.MutableState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.sp
import androidx.core.content.ContextCompat
import com.ravikiran.nsequant.data.Prefs
import com.ravikiran.nsequant.monitor.MonitorService
import com.ravikiran.nsequant.ui.AppTheme
import com.ravikiran.nsequant.ui.BankNiftyScreen
import com.ravikiran.nsequant.ui.ChainScreen
import com.ravikiran.nsequant.ui.FiiScreen
import com.ravikiran.nsequant.ui.GlobalScreen
import com.ravikiran.nsequant.ui.HomeScreen
import com.ravikiran.nsequant.ui.Pal
import com.ravikiran.nsequant.ui.RiskScreen
import com.ravikiran.nsequant.ui.SettingsScreen

class MainActivity : ComponentActivity() {
    private val startTab = mutableStateOf("home")

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        intent?.getStringExtra("tab")?.let { startTab.value = it }
        if (Prefs.monitorOn(this)) MonitorService.start(this)
        setContent { AppTheme { App(startTab) } }
    }

    override fun onNewIntent(intent: Intent) {        // tapped the widget while the app was open
        super.onNewIntent(intent)
        intent.getStringExtra("tab")?.let { startTab.value = it }
    }
}

private data class Tab(val key: String, val label: String, val icon: ImageVector)

private val TABS = listOf(
    Tab("home", "Home", Icons.Filled.Home),
    Tab("banks", "BNF", Icons.Filled.Star),
    Tab("chain", "Chain", Icons.AutoMirrored.Filled.List),
    Tab("risk", "Writer risk", Icons.Filled.Warning),
    Tab("fii", "FII/DII", Icons.Filled.Info),
    Tab("global", "Global", Icons.Filled.Place),
)

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun App(startTab: MutableState<String>) {
    val ctx = LocalContext.current
    var tab by rememberSaveable { mutableStateOf(startTab.value) }
    LaunchedEffect(startTab.value) { tab = startTab.value }
    var settings by rememberSaveable { mutableStateOf(false) }
    var monitor by rememberSaveable { mutableStateOf(Prefs.monitorOn(ctx)) }
    val askNotify = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        if (granted) { MonitorService.start(ctx); Prefs.setMonitor(ctx, true); monitor = true }
    }
    fun setMonitor(on: Boolean) {
        if (on) {
            val needs = Build.VERSION.SDK_INT >= 33 &&
                ContextCompat.checkSelfPermission(ctx, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
            if (needs) { askNotify.launch(Manifest.permission.POST_NOTIFICATIONS); return }
            MonitorService.start(ctx)
        } else {
            MonitorService.stop(ctx)
        }
        Prefs.setMonitor(ctx, on)
        monitor = on
    }
    Scaffold(
        containerColor = Pal.bg,
        topBar = {
            TopAppBar(
                title = { Text(if (settings) "Settings" else "NSE Quant Pro", fontWeight = FontWeight.Bold, fontSize = 18.sp,
                    color = Pal.accent) },
                actions = { IconButton(onClick = { settings = !settings }) { Icon(Icons.Filled.Settings, "Settings") } },
                colors = TopAppBarDefaults.topAppBarColors(containerColor = Pal.panel2),
            )
        },
        bottomBar = {
            if (!settings) NavigationBar(containerColor = Pal.panel2) {
                TABS.forEach { t ->
                    NavigationBarItem(selected = tab == t.key, onClick = { tab = t.key },
                        icon = { Icon(t.icon, t.label) }, label = { Text(t.label, fontSize = 10.sp, maxLines = 1, softWrap = false) },
                        colors = NavigationBarItemDefaults.colors(indicatorColor = Pal.border))
                }
            }
        },
    ) { pad ->
        Box(Modifier.fillMaxSize().padding(pad)) {
            if (settings) {
                SettingsScreen(onDone = { settings = false })
            } else when (tab) {
                "home" -> HomeScreen(monitor, ::setMonitor)
                "banks" -> BankNiftyScreen()
                "chain" -> ChainScreen()
                "risk" -> RiskScreen()
                "fii" -> FiiScreen()
                "global" -> GlobalScreen()
            }
        }
    }
}

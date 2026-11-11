package com.ravikiran.nsequant.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.IntrinsicSize
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.lifecycle.repeatOnLifecycle
import com.ravikiran.nsequant.data.Api
import kotlinx.coroutines.delay
import org.json.JSONObject

data class ApiState(val data: JSONObject? = null, val error: String? = null, val at: Long = 0L)

/** Polls `path` every `everyMs` while the screen is visible (pauses in the background). */
@Composable
fun rememberApi(path: String, everyMs: Long, refreshKey: Any? = null): ApiState {
    val ctx = LocalContext.current
    val owner = LocalLifecycleOwner.current
    var state by remember(path) { mutableStateOf(ApiState()) }
    LaunchedEffect(path, refreshKey) {
        owner.lifecycle.repeatOnLifecycle(Lifecycle.State.STARTED) {
            while (true) {
                state = try {
                    ApiState(Api.get(ctx, path), null, System.currentTimeMillis())
                } catch (e: Exception) {
                    state.copy(error = e.message ?: "error")
                }
                delay(everyMs)
            }
        }
    }
    return state
}

@Composable
fun Section(title: String? = null, modifier: Modifier = Modifier, content: @Composable ColumnScope.() -> Unit) {
    Column(
        modifier
            .fillMaxWidth()
            .padding(horizontal = 12.dp, vertical = 5.dp)
            .background(Pal.panel, RoundedCornerShape(10.dp))
            .border(1.dp, Pal.border, RoundedCornerShape(10.dp))
            .padding(12.dp),
        verticalArrangement = Arrangement.spacedBy(6.dp),
    ) {
        if (title != null) {
            Text(title.uppercase(), color = Pal.muted, fontSize = 13.sp, fontWeight = FontWeight.Bold,
                letterSpacing = 1.sp)
        }
        content()
    }
}

@Composable
fun Badge(text: String, fg: Color, bg: Color) {
    Text(text, color = fg, fontSize = 13.sp, fontWeight = FontWeight.Bold,
        modifier = Modifier.background(bg, RoundedCornerShape(6.dp)).padding(horizontal = 8.dp, vertical = 3.dp))
}

@Composable
fun AlertCard(level: String, title: String, lines: List<String>, action: String? = null) {
    val (bg, fg) = when (level.uppercase()) {
        "RED" -> Pal.redBg to Color(0xFFFECACA)
        "AMBER" -> Pal.amberBg to Color(0xFFFDE68A)
        "GREEN" -> Pal.greenBg to Color(0xFFBBF7D0)
        else -> Pal.panel2 to Pal.text
    }
    val edge = when (level.uppercase()) {
        "RED" -> Pal.down; "AMBER" -> Pal.warn; "GREEN" -> Pal.up; else -> Pal.accent
    }
    Row(Modifier.fillMaxWidth().height(IntrinsicSize.Min).background(bg, RoundedCornerShape(8.dp))) {
        Box(Modifier.width(4.dp).fillMaxHeight().background(edge))
        Column(Modifier.padding(10.dp), verticalArrangement = Arrangement.spacedBy(3.dp)) {
            Text("[$level] $title", color = edge, fontWeight = FontWeight.Bold, fontSize = 15.sp)
            lines.forEach { Text("• $it", color = fg, fontSize = 14.sp) }
            if (!action.isNullOrBlank()) Text("→ $action", color = Color.White, fontWeight = FontWeight.Bold, fontSize = 14.sp)
        }
    }
}

@Composable
fun Loading(state: ApiState, content: @Composable (JSONObject) -> Unit) {
    val d = state.data
    when {
        d != null -> {
            if (state.error != null) {
                Text("⚠ ${state.error} (showing last data)", color = Pal.warn, fontSize = 13.sp,
                    modifier = Modifier.padding(horizontal = 14.dp, vertical = 4.dp))
            }
            content(d)
        }
        state.error != null -> Text(state.error, color = Pal.warn, modifier = Modifier.padding(16.dp))
        else -> Box(Modifier.fillMaxWidth().padding(32.dp), contentAlignment = Alignment.Center) {
            CircularProgressIndicator()
        }
    }
}

@Composable
fun KV(k: String, v: String, color: Color = Pal.text) {
    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
        Text(k, color = Pal.muted, fontSize = 14.sp)
        Text(v, color = color, fontSize = 14.sp, fontWeight = FontWeight.SemiBold)
    }
}

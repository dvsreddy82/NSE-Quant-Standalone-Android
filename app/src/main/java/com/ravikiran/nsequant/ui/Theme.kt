package com.ravikiran.nsequant.ui

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color

/** Same palette as the desktop app. */
object Pal {
    val bg = Color(0xFF0B0F17)
    val panel = Color(0xFF111827)
    val panel2 = Color(0xFF0F172A)
    val border = Color(0xFF1F2937)
    val text = Color(0xFFE5E7EB)
    val muted = Color(0xFFCBD5E1)      // brighter for readability
    val faint = Color(0xFF9CA3AF)
    val accent = Color(0xFF38BDF8)
    val up = Color(0xFF22C55E)
    val down = Color(0xFFEF4444)
    val warn = Color(0xFFF59E0B)
    val redBg = Color(0xFF450A0A)
    val amberBg = Color(0xFF3B2306)
    val greenBg = Color(0xFF052E16)

    fun chg(v: Double?): Color = when {
        v == null -> text
        v > 0 -> up
        v < 0 -> down
        else -> text
    }
}

@Composable
fun AppTheme(content: @Composable () -> Unit) {
    MaterialTheme(
        colorScheme = darkColorScheme(
            primary = Pal.accent, onPrimary = Color.Black, background = Pal.bg, onBackground = Pal.text,
            surface = Pal.panel, onSurface = Pal.text, surfaceVariant = Pal.panel2, onSurfaceVariant = Pal.muted,
            outline = Pal.border, error = Pal.down,
        ),
        content = content,
    )
}

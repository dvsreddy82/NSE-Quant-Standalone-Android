package com.ravikiran.nsequant.ui

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.net.Uri
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Checkbox
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
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.ravikiran.nsequant.data.Api
import com.ravikiran.nsequant.data.arr
import com.ravikiran.nsequant.data.s

private const val GEMINI_PKG = "com.google.android.apps.bard"

/**
 * "Export for Gemini": a text snapshot of everything the app has calculated, with instructions for
 * the model at the top, handed to the Gemini app (your Google One AI Pro plan) or copied to paste
 * anywhere.  Positions and P&L are left out unless ticked.  Nothing leaves the phone until you send it.
 */
@Composable
fun GeminiExportDialog(onDismiss: () -> Unit) {
    val ctx = LocalContext.current
    var withPositions by remember { mutableStateOf(false) }
    var text by remember { mutableStateOf<String?>(null) }
    var note by remember { mutableStateOf("Building the snapshot…") }
    LaunchedEffect(withPositions) {
        text = null
        note = "Building the snapshot…"
        try {
            val r = Api.get(ctx, "/api/brief" + if (withPositions) "?positions=1" else "")
            val t = r.s("text")
            if (t == null) {
                note = r.s("error") ?: "This app's engine doesn't have the Gemini export yet — update it."
            } else {
                text = t
                val missing = r.arr("missing").let { a -> (0 until a.length()).map { a.getString(it) } }
                note = "${t.length} characters" + if (missing.isNotEmpty()) " · not loaded yet: ${missing.joinToString()}" else ""
            }
        } catch (e: Exception) {
            note = "Couldn't build it: ${e.message}"
        }
    }
    AlertDialog(
        onDismissRequest = onDismiss,
        containerColor = Pal.panel,
        title = { Text("Export for Gemini") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Text("A snapshot of the app's numbers with instructions for the model. Open it in the Gemini app " +
                    "and ask your questions there. Gemini's answer is analysis, not investment advice.",
                    color = Pal.muted, fontSize = 12.sp)
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Checkbox(withPositions, { withPositions = it })
                    Text("Include my positions and P&L", fontSize = 13.sp)
                }
                Text(note, color = Pal.faint, fontSize = 11.sp)
                text?.let {
                    Column(Modifier.heightIn(max = 260.dp).verticalScroll(rememberScrollState())) {
                        Text(it, fontFamily = FontFamily.Monospace, fontSize = 10.sp, color = Pal.muted)
                    }
                }
            }
        },
        confirmButton = {
            TextButton(enabled = text != null, onClick = { text?.let { openGemini(ctx, it) }; onDismiss() }) {
                Text("Open in Gemini")
            }
        },
        dismissButton = {
            Row {
                TextButton(enabled = text != null, onClick = { text?.let { copy(ctx, it) } }) { Text("Copy") }
                TextButton(onClick = onDismiss) { Text("Close") }
            }
        },
    )
}

private fun copy(ctx: Context, text: String) {
    ctx.getSystemService(ClipboardManager::class.java).setPrimaryClip(ClipData.newPlainText("NSE snapshot", text))
}

/** Share straight into the Gemini app; if it isn't installed, the share sheet; last, gemini.google.com. */
private fun openGemini(ctx: Context, text: String) {
    copy(ctx, text)                                   // also on the clipboard, in case Gemini drops long shares
    val send = Intent(Intent.ACTION_SEND).setType("text/plain").putExtra(Intent.EXTRA_TEXT, text)
    val tries = listOf(
        Intent(send).setPackage(GEMINI_PKG),
        Intent.createChooser(send, "Send snapshot to"),
        Intent(Intent.ACTION_VIEW, Uri.parse("https://gemini.google.com/app")),
    )
    for (i in tries) {
        try {
            ctx.startActivity(i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
            return
        } catch (_: Exception) {
        }
    }
}

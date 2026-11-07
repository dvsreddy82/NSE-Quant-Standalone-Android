package com.ravikiran.nsequant.data

import android.content.Context

/** Monitor state, status-bar icon choice and alerts already notified (private app storage). */
object Prefs {
    private const val FILE = "nsequant"

    private fun sp(ctx: Context) = ctx.getSharedPreferences(FILE, Context.MODE_PRIVATE)

    fun monitorOn(ctx: Context) = sp(ctx).getBoolean("monitor", false)
    fun setMonitor(ctx: Context, on: Boolean) = sp(ctx).edit().putBoolean("monitor", on).apply()

    /** Status-bar icon: which index ("NIFTY BANK" / "^NSEI") and what it shows ("both" / "pct" / "points"). */
    fun iconIndex(ctx: Context): String = sp(ctx).getString("icon_index", "NIFTY BANK") ?: "NIFTY BANK"
    fun iconMode(ctx: Context): String = sp(ctx).getString("icon_mode", "both") ?: "both"
    fun setIcon(ctx: Context, index: String, mode: String) =
        sp(ctx).edit().putString("icon_index", index).putString("icon_mode", mode).apply()

    /**
     * Alert ids already notified, so each alert pings the phone once.  Kept in order (oldest first)
     * so trimming drops the oldest ids, never one that is still on the alert list.
     */
    fun seen(ctx: Context): LinkedHashSet<String> {
        val list = sp(ctx).getString("seen_list", null)
        if (list != null) return LinkedHashSet(list.split('\n').filter { it.isNotEmpty() })
        return LinkedHashSet(sp(ctx).getStringSet("seen", emptySet()) ?: emptySet())   // older versions
    }

    /** Saves [ids]; those still listed ([current]) move to the end so they are the last to be trimmed. */
    fun setSeen(ctx: Context, ids: Set<String>, current: Collection<String> = emptyList()) {
        val cur = current.filter { it in ids }
        val ordered = (ids.filter { it !in cur.toSet() } + cur).takeLast(1000)
        sp(ctx).edit().putString("seen_list", ordered.joinToString("\n")).remove("seen").apply()
    }
}

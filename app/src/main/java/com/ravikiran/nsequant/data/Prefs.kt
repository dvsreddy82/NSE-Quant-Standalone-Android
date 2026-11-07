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

    /** Alert ids already notified, so each alert pings the phone once. */
    fun seen(ctx: Context): MutableSet<String> = (sp(ctx).getStringSet("seen", emptySet()) ?: emptySet()).toMutableSet()
    fun setSeen(ctx: Context, ids: Set<String>) =
        sp(ctx).edit().putStringSet("seen", ids.toList().takeLast(300).toSet()).apply()
}

package com.ravikiran.nsequant.data

import android.icu.text.DecimalFormat
import kotlin.math.abs

/** Indian digit grouping (1,23,456.78) and short forms for contracts / rupees. */
object Fmt {
    private val p2 = DecimalFormat("#,##,##0.00")
    private val p1 = DecimalFormat("#,##,##0.0")
    private val p0 = DecimalFormat("#,##,##0")

    fun px(v: Double?): String = v?.let { p2.format(it) } ?: "—"
    fun px1(v: Double?): String = v?.let { p1.format(it) } ?: "—"
    fun int(v: Double?): String = v?.let { p0.format(it) } ?: "—"
    fun signed(v: Double?, dp: Int = 2): String = v?.let {
        (if (it >= 0) "+" else "−") + (if (dp == 0) p0 else if (dp == 1) p1 else p2).format(abs(it))
    } ?: "—"
    fun pct(v: Double?): String = v?.let { (if (it >= 0) "+" else "−") + String.format("%.2f%%", abs(it)) } ?: "—"
    fun rupees(v: Double?): String = v?.let { (if (it >= 0) "+₹" else "−₹") + p0.format(abs(it)) } ?: "—"
    fun short(v: Double?): String {
        if (v == null) return "—"
        val a = abs(v)
        val s = when {
            a >= 1e7 -> String.format("%.2fCr", a / 1e7)
            a >= 1e5 -> String.format("%.2fL", a / 1e5)
            a >= 1e3 -> String.format("%.1fK", a / 1e3)
            else -> p0.format(a)
        }
        return (if (v < 0) "−" else "") + s
    }
    fun shortSigned(v: Double?): String = v?.let { (if (it >= 0) "+" else "") + short(it) } ?: "—"
}

package com.ravikiran.nsequant.data

import android.content.Context
import com.chaquo.python.PyObject
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject

/**
 * The analysis engine runs inside this app (Python via Chaquopy: the desktop app's own modules).
 * Nothing talks to a Mac: the engine fetches from Shoonya (market hours), NSE, Yahoo and RSS itself
 * and stores everything in a local SQLite database.
 */
object Engine {
    @Volatile private var api: PyObject? = null
    private val lock = Any()

    fun startAsync(ctx: Context) {
        Thread({ ensure(ctx) }, "engine-start").start()
    }

    fun ensure(ctx: Context): PyObject = api ?: synchronized(lock) {
        api ?: run {
            val app = ctx.applicationContext
            if (!Python.isStarted()) Python.start(AndroidPlatform(app))
            val m = Python.getInstance().getModule("phone_api")
            m.callAttr("start", app.filesDir.absolutePath)
            api = m
            m
        }
    }

    /** A screen's data: the same JSON the companion app fetched from the desktop app (`/api/...`). */
    fun get(ctx: Context, path: String): JSONObject = JSONObject(ensure(ctx).callAttr("get", path).toString())

    /** An action: Shoonya settings / sign-in / disconnect, refresh, direction call now. */
    fun act(ctx: Context, name: String, payload: JSONObject = JSONObject()): JSONObject =
        JSONObject(ensure(ctx).callAttr("act", name, payload.toString()).toString())
}

/** Kept with the companion app's name so every screen works unchanged. */
object Api {
    class ApiError(message: String) : Exception(message)

    suspend fun get(ctx: Context, path: String): JSONObject = withContext(Dispatchers.IO) {
        val j = Engine.get(ctx, path)
        if (j.length() == 1 && j.has("error")) throw ApiError(j.getString("error"))
        j
    }

    suspend fun act(ctx: Context, name: String, payload: JSONObject = JSONObject()): JSONObject =
        withContext(Dispatchers.IO) { Engine.act(ctx, name, payload) }
}

// ---------------------------------------------------------------- JSON helpers
fun JSONObject.d(k: String): Double? = if (!has(k) || isNull(k)) null else optDouble(k).takeIf { !it.isNaN() }
fun JSONObject.s(k: String): String? = if (!has(k) || isNull(k)) null else optString(k)
fun JSONObject.b(k: String): Boolean = optBoolean(k, false)
fun JSONObject.obj(k: String): JSONObject? = if (!has(k) || isNull(k)) null else optJSONObject(k)
fun JSONObject.arr(k: String): JSONArray = optJSONArray(k) ?: JSONArray()
fun JSONArray.objects(): List<JSONObject> = (0 until length()).mapNotNull { optJSONObject(it) }
fun JSONArray.strings(): List<String> = (0 until length()).map { optString(it) }
fun JSONArray.doubles(): List<Double> = (0 until length()).mapNotNull { optDouble(it).takeIf { v -> !v.isNaN() } }

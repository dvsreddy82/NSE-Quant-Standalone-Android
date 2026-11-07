package com.ravikiran.nsequant

import android.app.Application
import com.ravikiran.nsequant.data.Engine

/** Starts the Python engine once per process, in the background, as soon as the app (or its service,
 *  widget or alarm) is created. */
class NseApp : Application() {
    override fun onCreate() {
        super.onCreate()
        Engine.startAsync(this)
    }
}

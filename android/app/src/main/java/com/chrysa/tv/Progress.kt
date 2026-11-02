package com.chrysa.tv

import android.content.Context

data class Entry(val pos: Long, val dur: Long, val last: Long) {
    val fraction get() = if (dur > 0) pos.toFloat() / dur else 0f
    /** Started, but not finished. */
    val resumable get() = pos > 10_000 && fraction < 0.95f
}

/** Playback positions, kept as one small preferences file: "pos|dur|lastPlayed" per video id. */
object Progress {
    private fun prefs(context: Context) = context.getSharedPreferences("watch", Context.MODE_PRIVATE)

    fun all(context: Context): Map<Long, Entry> {
        val out = HashMap<Long, Entry>()
        for ((key, value) in prefs(context).all) {
            val id = key.toLongOrNull() ?: continue
            val p = (value as? String)?.split('|') ?: continue
            if (p.size == 3) out[id] = Entry(p[0].toLongOrNull() ?: 0, p[1].toLongOrNull() ?: 0, p[2].toLongOrNull() ?: 0)
        }
        return out
    }

    fun get(context: Context, id: Long): Entry? = all(context)[id]

    fun save(context: Context, id: Long, pos: Long, dur: Long) {
        prefs(context).edit().putString(id.toString(), "$pos|$dur|${System.currentTimeMillis()}").apply()
    }
}

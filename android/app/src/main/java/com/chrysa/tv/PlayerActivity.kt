package com.chrysa.tv

import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.KeyEvent
import androidx.annotation.OptIn
import androidx.activity.ComponentActivity
import androidx.media3.common.MediaItem
import androidx.media3.common.C
import androidx.media3.common.util.UnstableApi
import androidx.media3.exoplayer.DefaultLoadControl
import androidx.media3.exoplayer.DefaultRenderersFactory
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.ui.PlayerView

@OptIn(UnstableApi::class)
class PlayerActivity : ComponentActivity() {
    private var player: ExoPlayer? = null
    private lateinit var view: PlayerView
    private var mediaId = 0L
    private val handler = Handler(Looper.getMainLooper())
    private val saver = object : Runnable {
        override fun run() {
            save()
            handler.postDelayed(this, 5_000)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_player)
        view = findViewById(R.id.player)
        mediaId = intent.getLongExtra("id", 0)
    }

    override fun onStart() {
        super.onStart()
        val uri = intent.data ?: return finish()
        // Small buffers: local files need little read-ahead, and RAM is scarce.
        val loadControl = DefaultLoadControl.Builder()
            .setBufferDurationsMs(5_000, 15_000, 1_500, 3_000)
            .setTargetBufferBytes(8 * 1024 * 1024)
            .build()
        val exo = ExoPlayer.Builder(this, DefaultRenderersFactory(this).setEnableDecoderFallback(true))
            .setLoadControl(loadControl)
            .build()
        val saved = Progress.get(this, mediaId)
        val start = if (saved != null && saved.resumable) saved.pos else 0L
        exo.setMediaItem(MediaItem.fromUri(uri), start)
        exo.prepare()
        exo.playWhenReady = true
        view.player = exo
        player = exo
        handler.postDelayed(saver, 5_000)
    }

    override fun onStop() {
        super.onStop()
        handler.removeCallbacks(saver)
        save()
        view.player = null
        player?.release()
        player = null
    }

    private fun save() {
        val p = player ?: return
        if (p.duration == C.TIME_UNSET || p.duration <= 0) return
        Progress.save(this, mediaId, p.currentPosition, p.duration)
    }

    /** With the controls hidden, left/right on the D-pad seek 10 seconds. */
    override fun dispatchKeyEvent(event: KeyEvent): Boolean {
        val p = player
        if (p != null && event.action == KeyEvent.ACTION_DOWN && !view.isControllerFullyVisible) {
            when (event.keyCode) {
                KeyEvent.KEYCODE_DPAD_LEFT -> { p.seekTo(maxOf(0, p.currentPosition - 10_000)); return true }
                KeyEvent.KEYCODE_DPAD_RIGHT -> { p.seekTo(p.currentPosition + 10_000); return true }
            }
        }
        return super.dispatchKeyEvent(event)
    }
}

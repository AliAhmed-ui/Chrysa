package com.chrysa.tv

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Environment
import android.provider.Settings
import android.view.KeyEvent
import android.os.Bundle
import android.view.View
import android.widget.TextView
import androidx.activity.ComponentActivity
import androidx.activity.OnBackPressedCallback
import androidx.activity.result.contract.ActivityResultContracts
import androidx.core.content.ContextCompat
import androidx.recyclerview.widget.LinearLayoutManager
import androidx.recyclerview.widget.RecyclerView

class MainActivity : ComponentActivity() {
    private var videos: List<Video> = emptyList()
    private var openShow: String? = null

    private lateinit var pageTitle: TextView
    private lateinit var list: RecyclerView
    private lateinit var empty: TextView
    private val rows = RowAdapter(::onCard)

    private var scanning = false
    private var lastScan = 0L

    private val permission = registerForActivityResult(ActivityResultContracts.RequestPermission()) { scan() }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        pageTitle = findViewById(R.id.page_title)
        empty = findViewById(R.id.empty)
        list = findViewById(R.id.list)
        list.layoutManager = LinearLayoutManager(this)
        list.adapter = rows

        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                if (openShow != null) {
                    openShow = null
                    render()
                } else {
                    isEnabled = false
                    onBackPressedDispatcher.onBackPressed()
                }
            }
        })

        videos = Library.load(this)
        if (videos.isNotEmpty()) render()
        if (Library.canReadFiles(this)) scan() else askAccess()
    }

    override fun onResume() {
        super.onResume()
        if (videos.isNotEmpty()) render()
        // Re-check for a newly plugged drive, at most every five minutes (Menu rescans on demand).
        if (Library.canReadFiles(this) && System.currentTimeMillis() - lastScan > 5 * 60_000) scan()
    }

    override fun onKeyDown(keyCode: Int, event: KeyEvent): Boolean {
        if (keyCode == KeyEvent.KEYCODE_MENU) {
            scan()
            return true
        }
        return super.onKeyDown(keyCode, event)
    }

    /** USB drives need "all files access" on Android 11+; some TVs lack that screen and fall back to the media index. */
    private fun askAccess() {
        if (Build.VERSION.SDK_INT >= 30) {
            try {
                startActivity(Intent(Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION, Uri.parse("package:$packageName")))
                return
            } catch (e: Exception) {
                // fall through
            }
        }
        permission.launch(
            if (Build.VERSION.SDK_INT >= 33) Manifest.permission.READ_MEDIA_VIDEO
            else Manifest.permission.READ_EXTERNAL_STORAGE,
        )
    }

    private fun scan() {
        if (scanning) return
        scanning = true
        pageTitle.text = getString(R.string.scanning)
        Thread {
            val found = Library.scan(this)
            if (found != null) Library.save(this, found)
            runOnUiThread {
                scanning = false
                lastScan = System.currentTimeMillis()
                if (found != null) videos = found
                pageTitle.text = openShow ?: ""
                render()
            }
        }.start()
    }

    private fun onCard(card: Card) {
        val video = card.play
        if (video != null) {
            startActivity(Intent(this, PlayerActivity::class.java).setData(video.uri).putExtra("id", video.id))
        } else if (card.show != null) {
            openShow = card.show
            render()
        }
    }

    private fun showEmpty(message: String) {
        empty.text = message
        empty.visibility = View.VISIBLE
    }

    private fun render() {
        val progress = Progress.all(this)
        fun cardOf(v: Video) = Card(
            title = v.show ?: v.title,
            subtitle = if (v.isEpisode) v.code else v.year?.toString().orEmpty(),
            thumb = v,
            play = v,
            fraction = progress[v.id]?.takeIf { it.resumable }?.fraction ?: 0f,
        )

        val out = ArrayList<Row>()
        val show = openShow
        if (show != null) {
            pageTitle.text = show
            videos.filter { it.show.equals(show, true) }
                .groupBy { it.season }
                .toSortedMap()
                .forEach { (season, eps) ->
                    out += Row(
                        if (season == 0) "Specials" else "Season $season",
                        eps.sortedBy { it.episode }.map {
                            cardOf(it).copy(title = it.code, subtitle = "")
                        },
                    )
                }
        } else {
            if (!scanning) pageTitle.text = ""
            val resuming = videos
                .filter { progress[it.id]?.resumable == true }
                .sortedByDescending { progress[it.id]!!.last }
                .take(12)
            if (resuming.isNotEmpty()) out += Row(getString(R.string.continue_watching), resuming.map(::cardOf))

            val shows = videos.filter { it.isEpisode }
                .groupBy { it.show!!.lowercase() }
                .values
                .sortedBy { it[0].show!!.lowercase() }
                .map { eps ->
                    val first = eps.minWith(compareBy({ it.season }, { it.episode }))
                    Card(first.show!!, "${eps.size} episodes", first, show = first.show)
                }
            if (shows.isNotEmpty()) out += Row(getString(R.string.tab_tv), shows)

            val movies = videos.filter { !it.isEpisode }.sortedBy { it.title.lowercase() }.map(::cardOf)
            if (movies.isNotEmpty()) out += Row(getString(R.string.tab_movies), movies)
        }

        rows.submit(out)
        if (out.isEmpty()) showEmpty(getString(R.string.empty)) else empty.visibility = View.GONE
    }
}

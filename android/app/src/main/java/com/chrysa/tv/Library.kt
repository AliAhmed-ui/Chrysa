package com.chrysa.tv

import android.Manifest
import android.content.ContentUris
import android.content.Context
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Environment
import android.os.storage.StorageManager
import android.provider.MediaStore
import androidx.core.content.ContextCompat
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.util.zip.CRC32

data class Video(
    val id: Long,
    val uri: Uri,
    val title: String,
    val show: String?,
    val season: Int,
    val episode: Int,
    val year: Int?,
    val added: Long,
) {
    val isEpisode get() = show != null
    val code get() = "S%02dE%02d".format(season, episode)
}

/**
 * Finds videos on USB drives and external disks, remembers them in library.json, and turns messy
 * file names into titles, seasons and episodes. Nothing on the TV's own storage is listed.
 */
object Library {
    private val EPISODE = Regex("""(?i)^(.+?)[\s._\-\[(]*(?:s(\d{1,2})[\s._-]*e(\d{1,3})|(\d{1,2})x(\d{2,3})(?!\d))""")
    private val YEAR = Regex("""(?i)^(.+?)[\s._(\[-]+((?:19|20)\d{2})(?!\d)""")
    private val TAGS = Regex("""(?i)[\s._\-\[(]+(?:2160p|1080p|720p|480p|bluray|brrip|web-?dl|webrip|hdtv|dvdrip|x26[45]|hevc).*$""")
    private val SAMPLE = Regex("""(?i)(?:^|[^a-z0-9])sample(?:[^a-z0-9]|$)""")
    private val EXTENSIONS = setOf("mp4", "mkv", "avi", "mov", "m4v", "ts", "webm", "wmv", "mpg", "mpeg")
    private val SKIP_DIRS = setOf("android", "lost.dir", "\$recycle.bin", "system volume information")
    private const val MIN_BYTES = 30L * 1024 * 1024
    private const val MAX_DEPTH = 8

    fun canReadFiles(context: Context) =
        if (Build.VERSION.SDK_INT >= 30) Environment.isExternalStorageManager()
        else ContextCompat.checkSelfPermission(context, Manifest.permission.READ_EXTERNAL_STORAGE) == PackageManager.PERMISSION_GRANTED

    /** Mounted USB / external drives. */
    private fun roots(context: Context): List<File> {
        val manager = context.getSystemService(StorageManager::class.java)
        val volumes = if (Build.VERSION.SDK_INT >= 30) {
            manager.storageVolumes
                .filter { it.isRemovable && it.state == Environment.MEDIA_MOUNTED }
                .mapNotNull { it.directory }
        } else emptyList()
        if (volumes.isNotEmpty()) return volumes
        return listOf("/storage", "/mnt/media_rw")
            .flatMap { File(it).listFiles()?.toList().orEmpty() }
            .filter { it.isDirectory && it.name != "emulated" && it.name != "self" && it.canRead() }
    }

    /** Every video on the connected drives, or null when no drive is connected. */
    fun scan(context: Context): List<Video>? {
        val roots = roots(context)
        val found = LinkedHashMap<String, Video>()
        if (roots.isNotEmpty() && canReadFiles(context)) roots.forEach { walk(it, 0, found) }
        if (found.isEmpty()) mediaIndex(context, found)
        return if (found.isEmpty() && roots.isEmpty()) null else found.values.toList()
    }

    private fun walk(dir: File, depth: Int, out: MutableMap<String, Video>) {
        val children = dir.listFiles() ?: return
        for (f in children) {
            if (f.name.startsWith(".")) continue
            if (f.isDirectory) {
                if (depth < MAX_DEPTH && f.name.lowercase() !in SKIP_DIRS) walk(f, depth + 1, out)
            } else if (f.extension.lowercase() in EXTENSIONS && f.length() >= MIN_BYTES && !SAMPLE.containsMatchIn(f.name)) {
                val uri = Uri.fromFile(f)
                out[uri.toString()] = parse(uri, f.name, f.lastModified() / 1000)
            }
        }
    }

    /** Fallback for TVs that index USB drives themselves but deny direct file access. */
    @Suppress("DEPRECATION")
    private fun mediaIndex(context: Context, out: MutableMap<String, Video>) {
        val projection = arrayOf(
            MediaStore.Video.Media._ID, MediaStore.Video.Media.DISPLAY_NAME, MediaStore.Video.Media.DATE_ADDED,
            MediaStore.Video.Media.SIZE, MediaStore.Video.Media.DATA,
        )
        try {
            context.contentResolver.query(MediaStore.Video.Media.EXTERNAL_CONTENT_URI, projection, null, null, null)?.use { c ->
                while (c.moveToNext()) {
                    val path = c.getString(4) ?: continue
                    if (!path.startsWith("/storage/") || path.startsWith("/storage/emulated")) continue
                    val name = c.getString(1) ?: continue
                    if (c.getLong(3) < MIN_BYTES || SAMPLE.containsMatchIn(name)) continue
                    val uri = ContentUris.withAppendedId(MediaStore.Video.Media.EXTERNAL_CONTENT_URI, c.getLong(0))
                    out[uri.toString()] = parse(uri, name, c.getLong(2))
                }
            }
        } catch (e: SecurityException) {
            // no media permission: leave the list empty
        }
    }

    // ------------------------------------------------------------- saved library

    private fun store(context: Context) = File(context.filesDir, "library.json")

    fun save(context: Context, videos: List<Video>) {
        val array = JSONArray()
        for (v in videos) {
            array.put(
                JSONObject().put("u", v.uri.toString()).put("t", v.title).put("s", v.show ?: "")
                    .put("se", v.season).put("ep", v.episode).put("y", v.year ?: 0).put("a", v.added),
            )
        }
        store(context).writeText(array.toString())
    }

    /** The saved library, limited to files that are reachable right now (the drive may be unplugged). */
    fun load(context: Context): List<Video> {
        val file = store(context)
        if (!file.exists()) return emptyList()
        val out = ArrayList<Video>()
        try {
            val array = JSONArray(file.readText())
            for (i in 0 until array.length()) {
                val o = array.getJSONObject(i)
                val uri = Uri.parse(o.getString("u"))
                if (uri.scheme == "file" && !File(uri.path!!).exists()) continue
                out += Video(
                    idOf(uri), uri, o.getString("t"), o.getString("s").ifEmpty { null },
                    o.getInt("se"), o.getInt("ep"), o.getInt("y").takeIf { it > 0 }, o.getLong("a"),
                )
            }
        } catch (e: Exception) {
            return emptyList()
        }
        return out
    }

    // ------------------------------------------------------------- name parsing

    private fun idOf(uri: Uri) = CRC32().apply { update(uri.toString().toByteArray()) }.value

    private fun clean(s: String) = s.replace('.', ' ').replace('_', ' ').replace(Regex("\\s+"), " ").trim(' ', '-')

    private fun parse(uri: Uri, fileName: String, added: Long): Video {
        val id = idOf(uri)
        val base = fileName.substringBeforeLast('.')
        EPISODE.find(base)?.let { m ->
            val show = clean(m.groupValues[1])
            if (show.isNotEmpty()) {
                val season = (m.groupValues[2].ifEmpty { m.groupValues[4] }).toInt()
                val episode = (m.groupValues[3].ifEmpty { m.groupValues[5] }).toInt()
                return Video(id, uri, show, show, season, episode, null, added)
            }
        }
        YEAR.find(base)?.let { m ->
            val title = clean(m.groupValues[1])
            if (title.isNotEmpty()) return Video(id, uri, title, null, 0, 0, m.groupValues[2].toInt(), added)
        }
        val title = clean(TAGS.replace(base, "")).ifEmpty { clean(base) }
        return Video(id, uri, title, null, 0, 0, null, added)
    }
}

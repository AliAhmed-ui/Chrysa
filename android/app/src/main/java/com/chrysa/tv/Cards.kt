package com.chrysa.tv

import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.media.ThumbnailUtils
import android.os.Build
import android.provider.MediaStore
import android.util.LruCache
import android.util.Size
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.ImageView
import android.widget.ProgressBar
import android.widget.TextView
import androidx.recyclerview.widget.RecyclerView
import java.io.File
import java.io.FileOutputStream
import java.util.concurrent.Executors

/** One tile. [play] is set for a playable video; otherwise [show] names a series to open. */
data class Card(
    val title: String,
    val subtitle: String,
    val thumb: Video,
    val play: Video? = null,
    val show: String? = null,
    val fraction: Float = 0f,
)

/** Small bounded thumbnail cache backed by a disk cache, so a sleeping USB drive is not woken for every redraw. */
object Thumbs {
    private val cache = object : LruCache<Long, Bitmap>(6 * 1024 * 1024) {
        override fun sizeOf(key: Long, value: Bitmap) = value.byteCount
    }
    private val pool = Executors.newFixedThreadPool(2)

    @Suppress("DEPRECATION")
    private fun render(context: Context, video: Video): Bitmap? = try {
        if (video.uri.scheme == "file") {
            val file = File(video.uri.path!!)
            if (Build.VERSION.SDK_INT >= 29) ThumbnailUtils.createVideoThumbnail(file, Size(320, 180), null)
            else ThumbnailUtils.createVideoThumbnail(file.path, MediaStore.Video.Thumbnails.MINI_KIND)
        } else if (Build.VERSION.SDK_INT >= 29) {
            context.contentResolver.loadThumbnail(video.uri, Size(320, 180), null)
        } else null
    } catch (e: Exception) {
        null
    }

    fun load(view: ImageView, video: Video) {
        view.tag = video.id
        val hit = cache.get(video.id)
        if (hit != null) {
            view.setImageBitmap(hit)
            return
        }
        view.setImageDrawable(null)
        val context = view.context.applicationContext
        pool.execute {
            val dir = File(context.cacheDir, "thumbs").apply { mkdirs() }
            val saved = File(dir, "${video.id}.jpg")
            var bitmap = if (saved.exists()) BitmapFactory.decodeFile(saved.path) else null
            if (bitmap == null) {
                bitmap = render(context, video) ?: return@execute
                try {
                    FileOutputStream(saved).use { bitmap.compress(Bitmap.CompressFormat.JPEG, 80, it) }
                } catch (e: Exception) {
                    // the cache is optional
                }
            }
            cache.put(video.id, bitmap)
            view.post { if (view.tag == video.id) view.setImageBitmap(bitmap) }
        }
    }
}

class CardAdapter(private val widthDp: Int, private val onClick: (Card) -> Unit) :
    RecyclerView.Adapter<CardAdapter.Holder>() {

    private var items: List<Card> = emptyList()

    fun submit(list: List<Card>) {
        items = list
        notifyDataSetChanged()
    }

    class Holder(view: View) : RecyclerView.ViewHolder(view) {
        val thumb: ImageView = view.findViewById(R.id.thumb)
        val progress: ProgressBar = view.findViewById(R.id.progress)
        val title: TextView = view.findViewById(R.id.title)
        val sub: TextView = view.findViewById(R.id.sub)
    }

    override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): Holder {
        val view = LayoutInflater.from(parent.context).inflate(R.layout.item_card, parent, false)
        if (widthDp > 0) {
            view.layoutParams = RecyclerView.LayoutParams(
                (widthDp * parent.resources.displayMetrics.density).toInt(),
                ViewGroup.LayoutParams.WRAP_CONTENT,
            )
        }
        return Holder(view)
    }

    override fun onBindViewHolder(holder: Holder, position: Int) {
        val card = items[position]
        holder.title.text = card.title
        holder.sub.text = card.subtitle
        holder.progress.visibility = if (card.fraction > 0f) View.VISIBLE else View.GONE
        holder.progress.progress = (card.fraction * 1000).toInt()
        Thumbs.load(holder.thumb, card.thumb)
        holder.itemView.setOnClickListener { onClick(card) }
    }

    override fun getItemCount() = items.size
}

data class Row(val title: String, val cards: List<Card>)

/** A vertical list of titled, horizontally scrolling rows: the whole page scrolls with the D-pad. */
class RowAdapter(private val onClick: (Card) -> Unit) : RecyclerView.Adapter<RowAdapter.Holder>() {
    private var items: List<Row> = emptyList()
    private val pool = RecyclerView.RecycledViewPool()

    fun submit(list: List<Row>) {
        items = list
        notifyDataSetChanged()
    }

    class Holder(view: View, val cards: CardAdapter) : RecyclerView.ViewHolder(view) {
        val title: TextView = view.findViewById(R.id.row_title)
    }

    override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): Holder {
        val view = LayoutInflater.from(parent.context).inflate(R.layout.item_row, parent, false)
        val cards = CardAdapter(190, onClick)
        view.findViewById<RecyclerView>(R.id.row).apply {
            layoutManager = androidx.recyclerview.widget.LinearLayoutManager(context, RecyclerView.HORIZONTAL, false)
            setRecycledViewPool(pool)
            adapter = cards
        }
        return Holder(view, cards)
    }

    override fun onBindViewHolder(holder: Holder, position: Int) {
        holder.title.text = items[position].title
        holder.cards.submit(items[position].cards)
    }

    override fun getItemCount() = items.size
}

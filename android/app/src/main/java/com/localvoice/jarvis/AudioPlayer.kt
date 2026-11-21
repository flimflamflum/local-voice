package com.localvoice.jarvis

import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioTrack
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit
import kotlin.concurrent.thread
import kotlin.math.max
import kotlin.math.min
import kotlin.math.sqrt

/**
 * Gapless player for the PC's streamed voice. Chunks go into one long AudioTrack stream,
 * and each chunk's callback fires when *that* audio actually starts playing, which is what
 * keeps the on-screen text in step with the voice. Callbacks and [onLevel] run on the main thread.
 */
class AudioPlayer(private val onLevel: (Float) -> Unit) {
    private sealed interface Item
    private class Pcm(val data: ByteArray, val rate: Int, val onStart: (() -> Unit)?) : Item
    private class Mark(val endOfReply: Boolean, val onReach: () -> Unit) : Item

    /** A point in the stream: fire [onReach] at [frame]; [env] gives the loudness for the orb. */
    private class Cue(
        val frame: Long, val endFrame: Long, val epoch: Int, val rate: Int,
        val env: FloatArray?, var onReach: (() -> Unit)?,
    )

    private val queue = LinkedBlockingQueue<Item>()
    private val cues = ArrayList<Cue>()
    private val main = Handler(Looper.getMainLooper())

    @Volatile private var epoch = 0
    @Volatile private var running = true
    @Volatile private var track: AudioTrack? = null
    private var trackRate = 0
    private var written = 0L // frames written into the current track (writer thread only)
    private var lastWrite = 0L

    init {
        thread(name = "player-writer", isDaemon = true) { writerLoop() }
        thread(name = "player-clock", isDaemon = true) { clockLoop() }
    }

    fun play(pcm: ByteArray, rate: Int, onStart: (() -> Unit)?) {
        queue.put(Pcm(pcm, rate, onStart))
    }

    /** Run [onReach] once everything queued before it has been heard. */
    fun mark(endOfReply: Boolean, onReach: () -> Unit) {
        queue.put(Mark(endOfReply, onReach))
    }

    /** Cut the audio right now (within ~50 ms) and drop everything queued. Main thread. */
    fun stop() {
        epoch++
        queue.clear()
        synchronized(cues) { cues.clear() }
        onLevel(0f)
    }

    fun release() {
        stop()
        running = false
    }

    // ------------------------------------------------------------------ writer
    private fun writerLoop() {
        var myEpoch = epoch
        while (running) {
            val item = queue.poll(250, TimeUnit.MILLISECONDS)
            if (epoch != myEpoch) {
                releaseTrack()
                myEpoch = epoch
            }
            when (item) {
                null -> releaseIfIdle()
                is Pcm -> {
                    val t = ensureTrack(item.rate) ?: continue
                    val frames = item.data.size / 2
                    addCue(Cue(written, written + frames, myEpoch, item.rate, envelope(item.data, item.rate), item.onStart))
                    written += writeAll(t, item.data, myEpoch) / 2
                }
                is Mark -> {
                    val t = track
                    if (t == null) {
                        main.post { if (epoch == myEpoch) item.onReach() }
                    } else {
                        addCue(Cue(written, written, myEpoch, trackRate, null, item.onReach))
                        if (item.endOfReply) {
                            // Push the tail of the reply out of the buffer with a little silence.
                            written += writeAll(t, ByteArray(t.bufferSizeInFrames * 2), myEpoch) / 2
                        }
                    }
                }
            }
        }
        releaseTrack()
    }

    private fun writeAll(t: AudioTrack, data: ByteArray, myEpoch: Int): Int {
        val piece = trackRate / 20 * 2 // 50 ms, so stop() is noticed quickly
        var off = 0
        while (off < data.size && epoch == myEpoch) {
            val n = t.write(data, off, min(piece, data.size - off))
            if (n <= 0) break
            off += n
        }
        lastWrite = SystemClock.elapsedRealtime()
        return off
    }

    private fun ensureTrack(rate: Int): AudioTrack? {
        track?.let { if (trackRate == rate) return it else releaseTrack() }
        return try {
            val min = AudioTrack.getMinBufferSize(rate, AudioFormat.CHANNEL_OUT_MONO, AudioFormat.ENCODING_PCM_16BIT)
            val t = AudioTrack.Builder()
                .setAudioAttributes(
                    AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_MEDIA)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                        .build()
                )
                .setAudioFormat(
                    AudioFormat.Builder()
                        .setSampleRate(rate)
                        .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                        .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                        .build()
                )
                .setTransferMode(AudioTrack.MODE_STREAM)
                .setBufferSizeInBytes(max(min * 2, rate / 5 * 2)) // at least 200 ms
                .build()
            t.play()
            trackRate = rate
            written = 0
            track = t
            t
        } catch (e: Exception) {
            null
        }
    }

    private fun releaseIfIdle() {
        val idle = SystemClock.elapsedRealtime() - lastWrite > 20_000
        if (track != null && idle && synchronized(cues) { cues.isEmpty() }) releaseTrack()
    }

    private fun releaseTrack() {
        val t = track ?: return
        track = null
        try {
            t.pause()
            t.flush()
        } catch (_: Exception) {
        }
        t.release()
        written = 0
    }

    private fun addCue(c: Cue) = synchronized(cues) { cues.add(c) }

    // ------------------------------------------------------------------ clock
    private fun clockLoop() {
        var lastLevel = 0f
        while (running) {
            SystemClock.sleep(25)
            val t = track
            val pos = if (t == null) -1L else try {
                t.playbackHeadPosition.toLong() and 0xffffffffL
            } catch (e: Exception) {
                -1L
            }
            val due = ArrayList<() -> Unit>()
            var level = 0f
            synchronized(cues) {
                val iter = cues.iterator()
                while (iter.hasNext()) {
                    val c = iter.next()
                    if (c.epoch != epoch) {
                        iter.remove()
                        continue
                    }
                    if (pos < c.frame) continue
                    c.onReach?.let { cb -> due += cb }
                    c.onReach = null
                    if (pos >= c.endFrame) {
                        iter.remove()
                    } else if (c.env != null) {
                        val i = ((pos - c.frame) * 20 / c.rate).toInt()
                        if (i in c.env.indices) level = c.env[i]
                    }
                }
            }
            val e = epoch
            for (cb in due) main.post { if (epoch == e) cb() }
            if (level != lastLevel) {
                lastLevel = level
                main.post { onLevel(level) }
            }
        }
    }

    private fun envelope(pcm: ByteArray, rate: Int): FloatArray {
        val block = max(1, rate / 20) // 50 ms
        val samples = pcm.size / 2
        val out = FloatArray((samples + block - 1) / block)
        for (b in out.indices) {
            var sum = 0.0
            val start = b * block
            val end = min(samples, start + block)
            for (i in start until end) {
                val v = ((pcm[2 * i + 1].toInt() shl 8) or (pcm[2 * i].toInt() and 0xff)).toShort().toInt()
                sum += (v * v).toDouble()
            }
            val rms = sqrt(sum / max(1, end - start)) / 32768.0
            out[b] = min(1.0, rms * 5).toFloat()
        }
        return out
    }
}

package com.localvoice.jarvis

import android.content.Context
import android.os.Handler
import android.os.Looper
import android.speech.tts.TextToSpeech
import android.speech.tts.UtteranceProgressListener
import java.util.concurrent.ConcurrentHashMap

/**
 * The phone's own text-to-speech, for "phone voice" mode: the PC only sends text, so there's
 * no audio streaming at all (lighter on data, but a more robotic voice).
 */
class PhoneVoice(context: Context) : TextToSpeech.OnInitListener {
    private val main = Handler(Looper.getMainLooper())
    private val callbacks = ConcurrentHashMap<String, () -> Unit>()
    private val beforeInit = ArrayList<() -> Unit>()
    private var initDone = false
    private var ok = false
    private var n = 0
    private val tts = TextToSpeech(context.applicationContext, this)

    /** 1.0 = normal speed. */
    var rate = 1f

    override fun onInit(status: Int) {
        main.post {
            initDone = true
            ok = status == TextToSpeech.SUCCESS
            if (ok) {
                tts.setOnUtteranceProgressListener(object : UtteranceProgressListener() {
                    override fun onStart(utteranceId: String) = fire(utteranceId)
                    override fun onDone(utteranceId: String) = fire(utteranceId)

                    @Deprecated("Deprecated in Java")
                    override fun onError(utteranceId: String) = fire(utteranceId)
                })
            }
            beforeInit.forEach { it() }
            beforeInit.clear()
        }
    }

    private fun fire(id: String) {
        callbacks.remove(id)?.let { main.post(it) }
    }

    /** Speak [text]; [onStart] runs when the phone starts saying it. */
    fun speak(text: String, onStart: () -> Unit) {
        if (!initDone) {
            beforeInit += { speak(text, onStart) }
            return
        }
        val id = "u${n++}"
        callbacks[id] = onStart
        if (!ok) return fire(id)
        tts.setSpeechRate(rate)
        if (tts.speak(text, TextToSpeech.QUEUE_ADD, null, id) != TextToSpeech.SUCCESS) fire(id)
    }

    /** Run [onReach] once everything queued before it has been spoken. */
    fun mark(onReach: () -> Unit) {
        if (!initDone) {
            beforeInit += { mark(onReach) }
            return
        }
        val id = "m${n++}"
        callbacks[id] = onReach
        if (!ok || tts.playSilentUtterance(30, TextToSpeech.QUEUE_ADD, id) != TextToSpeech.SUCCESS) fire(id)
    }

    fun stop() {
        beforeInit.clear()
        callbacks.clear()
        if (ok) tts.stop()
    }

    fun shutdown() {
        stop()
        tts.shutdown()
    }

    companion object {
        /** Strip markdown so the phone voice doesn't read symbols out loud. */
        fun clean(raw: String): String {
            var s = raw.replace(Regex("```[\\s\\S]*?(```|$)"), " I've put the code on screen. ")
            // Table separator rows and dividers ("|---|---|", "---"): nothing to say.
            s = s.replace(Regex("(?m)^\\s*(?:\\|?[\\s:|-]*-{3,}[\\s:|-]*\\|?|[*_]{3,})\\s*$"), "")
            // "| Flour | 2 cups |" -> "Flour, 2 cups." so table rows are read with pauses.
            s = s.replace(Regex("(?m)^\\s*\\|(.*)\\|\\s*$")) { m ->
                val cells = m.groupValues[1].split("|").map { it.trim() }.filter { it.isNotEmpty() }
                if (cells.isEmpty()) "" else cells.joinToString(", ") + "."
            }
            s = s.replace(Regex("`([^`]*)`"), "$1")
            s = s.replace(Regex("\\[([^\\]]+)]\\([^)]*\\)"), "$1")
            s = s.replace(Regex("[*_#>~|]+"), " ")
            return s.replace(Regex("\\s+"), " ").trim()
        }
    }
}

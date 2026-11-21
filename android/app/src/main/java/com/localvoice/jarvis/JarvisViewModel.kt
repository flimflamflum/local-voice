package com.localvoice.jarvis

import android.Manifest
import android.app.Application
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Handler
import android.os.Looper
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.core.content.ContextCompat
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import org.json.JSONArray
import org.json.JSONObject

enum class Phase { Offline, Connecting, Idle, Listening, Transcribing, Waiting, Thinking, Speaking }

enum class Role { User, Jarvis, Info }

class ChatMsg(val id: Long, val role: Role, text: String, live: Boolean = false) {
    var text by mutableStateOf(text)
    var live by mutableStateOf(live)

    // Reasoning from a thinking model, shown under a collapsed "See thinking" tab.
    var thoughts by mutableStateOf("")
    var thinking by mutableStateOf(false)            // still reasoning (live timer)
    var thinkSecs by mutableStateOf<Double?>(null)   // how long it thought, once done
    var thinkStart = 0L

    fun endThinking(secs: Double? = null) {
        if (!thinking) return
        thinking = false
        thinkSecs = secs ?: ((System.currentTimeMillis() - thinkStart) / 1000.0)
    }
}

class JarvisViewModel(app: Application) : AndroidViewModel(app) {
    private val store = SettingsStore(app)
    private val main = Handler(Looper.getMainLooper())

    // ---------------------------------------------------------------- UI state
    var prefs by mutableStateOf(Prefs())
        private set
    var phase by mutableStateOf(Phase.Offline)
        private set
    var status by mutableStateOf("Not connected")
        private set
    var assistantName by mutableStateOf("Jarvis")
        private set
    var models by mutableStateOf(listOf<String>())
        private set
    var voices by mutableStateOf(listOf<Pair<String, String>>())
        private set
    var styles by mutableStateOf(listOf<Pair<String, String>>())
        private set
    var showSettings by mutableStateOf(false)
    var requestMic by mutableStateOf(false)
        private set
    val level = mutableFloatStateOf(0f)
    val messages = mutableStateListOf<ChatMsg>()

    @Volatile private var current = Prefs() // read by the client thread when (re)connecting
    private var nextId = 0L

    // ---------------------------------------------------------------- engines
    private val client = JarvisClient(::onClientEvent)
    private val mic = MicRecorder()
    private val player = AudioPlayer { level.floatValue = it }
    private val phoneVoice = PhoneVoice(app)

    // ---------------------------------------------------------------- reply state
    private var turnId = -1
    private var turnUsesPcVoice = true
    private var bubble: ChatMsg? = null
    private var shown = ""   // what the bubble shows right now
    private var target = ""  // everything that's been "spoken" so far
    private var revealJob: Job? = null

    private var loaded = false
    private var pendingLink: String? = null

    init {
        viewModelScope.launch {
            setPrefs(store.load(), save = false)
            loaded = true
            val link = pendingLink
            pendingLink = null
            if (link != null && applyLink(link)) return@launch
            if (prefs.configured) connect() else {
                status = "Add your PC's address and token"
                showSettings = true
            }
        }
    }

    // ================================================================ actions
    /** Big orb: talk / send / interrupt depending on what Jarvis is doing. */
    fun onOrbTap() {
        when (phase) {
            Phase.Offline -> if (prefs.configured) connect() else showSettings = true
            Phase.Connecting -> Unit
            Phase.Listening -> mic.stop()
            Phase.Idle -> startListening()
            else -> {
                interrupt()
                startListening()
            }
        }
    }

    fun onMicPermission(granted: Boolean) {
        requestMic = false
        if (granted) startListening() else info("Jarvis needs the microphone to hear you - or type below.")
    }

    fun sendText(text: String) {
        val t = text.trim()
        if (t.isEmpty()) return
        if (!client.connected) return info("Not connected to your PC.")
        if (mic.active) mic.cancel()
        stopOutput()
        add(Role.User, t)
        client.send(JSONObject().put("type", "text").put("text", t))
        phase = Phase.Thinking
    }

    fun newChat() {
        if (mic.active) mic.cancel()
        stopOutput()
        client.send(JSONObject().put("type", "new_chat"))
        messages.clear()
        if (client.connected) phase = Phase.Idle
    }

    fun saveConnection(server: String, token: String) {
        setPrefs(prefs.copy(server = server.trim(), token = token.trim()))
        if (prefs.configured) connect()
    }

    /** The desktop app's QR code: jarvis://link?host=1.2.3.4&port=8765&token=... */
    fun applyLink(raw: String): Boolean {
        if (!loaded) {
            pendingLink = raw
            return true
        }
        val parsed = try {
            val uri = Uri.parse(raw.trim())
            if (uri.scheme == "jarvis") {
                val host = uri.getQueryParameter("host")
                val token = uri.getQueryParameter("token")
                val port = uri.getQueryParameter("port")?.toIntOrNull() ?: 8765
                if (!host.isNullOrBlank() && !token.isNullOrBlank()) "$host:$port" to token else null
            } else null
        } catch (e: Exception) {
            null
        }
        if (parsed == null) {
            status = "That QR code isn't a Jarvis link"
            return false
        }
        showSettings = false
        saveConnection(parsed.first, parsed.second)
        return true
    }

    fun scanFailed(message: String) {
        status = message
    }

    fun updatePrefs(change: (Prefs) -> Prefs) {
        setPrefs(change(prefs))
        client.send(JSONObject().put("type", "settings").put("settings", settingsJson(prefs)))
    }

    fun onBackground() {
        if (mic.active) mic.cancel()
    }

    // ================================================================ internals
    private fun setPrefs(p: Prefs, save: Boolean = true) {
        prefs = p
        current = p
        phoneVoice.rate = (1f + p.rate * 0.1f).coerceIn(0.5f, 2f)
        if (save) viewModelScope.launch { store.save(p) }
    }

    private fun connect() {
        client.connect(prefs.server) {
            val p = current
            JSONObject()
                .put("type", "hello")
                .put("token", p.token)
                .put("session", p.session)
                .put("settings", settingsJson(p))
        }
    }

    private fun settingsJson(p: Prefs) = JSONObject().apply {
        if (p.model.isNotEmpty()) put("model", p.model)
        if (p.style.isNotEmpty()) put("style", p.style)
        if (p.voice.isNotEmpty()) put("voice", p.voice)
        put("rate", p.rate)
        put("server_voice", p.serverVoice)
        put("think", p.think)
    }

    private fun startListening() {
        if (!client.connected || mic.active) return
        val granted = ContextCompat.checkSelfPermission(getApplication(), Manifest.permission.RECORD_AUDIO) ==
            PackageManager.PERMISSION_GRANTED
        if (!granted) {
            requestMic = true
            return
        }
        client.send(JSONObject().put("type", "audio_start"))
        phase = Phase.Listening
        mic.start(micListener)
    }

    private val micListener = object : MicRecorder.Listener {
        override fun onFrame(data: ByteArray, length: Int) = client.sendAudio(data, length)

        override fun onLevel(level: Float) {
            main.post { if (phase == Phase.Listening) this@JarvisViewModel.level.floatValue = level }
        }

        override fun onEnd(result: MicRecorder.Result) {
            main.post {
                if (result == MicRecorder.Result.Spoke) {
                    client.send(JSONObject().put("type", "audio_end"))
                    if (phase == Phase.Listening) phase = Phase.Transcribing
                } else {
                    client.send(JSONObject().put("type", "audio_cancel"))
                    if (phase == Phase.Listening) phase = if (client.connected) Phase.Idle else Phase.Offline
                    if (result == MicRecorder.Result.Failed) info("Couldn't use the microphone.")
                }
            }
        }
    }

    /** Stop Jarvis talking (locally and on the PC). */
    private fun interrupt() {
        client.send(JSONObject().put("type", "interrupt"))
        stopOutput()
    }

    private fun stopOutput() {
        player.stop()
        phoneVoice.stop()
        cutBubble()
    }

    private fun onClientEvent(e: ClientEvent) {
        when (e) {
            is ClientEvent.Connecting -> {
                phase = Phase.Connecting
                status = "Connecting…"
            }
            is ClientEvent.Welcome -> onWelcome(e.msg)
            is ClientEvent.Disconnected -> {
                if (mic.active) mic.cancel()
                stopOutput()
                phase = Phase.Offline
                status = e.reason
                if (e.fatal) showSettings = true
            }
            is ClientEvent.Message -> onMessage(e.msg)
            is ClientEvent.Chunk -> onChunk(e)
        }
    }

    private fun onWelcome(m: JSONObject) {
        models = m.optJSONArray("models").strings()
        voices = m.optJSONArray("voices").pairs()
        styles = m.optJSONArray("styles").pairs()
        assistantName = m.optString("name", "Jarvis").ifBlank { "Jarvis" }
        // Fill in anything not chosen on the phone with what the PC is using (not saved).
        val s = m.optJSONObject("settings") ?: JSONObject()
        prefs = prefs.copy(
            model = prefs.model.ifEmpty { s.optString("model") },
            style = prefs.style.ifEmpty { s.optString("style") },
            voice = prefs.voice.ifEmpty { s.optString("voice") },
        )
        current = prefs
        status = "Connected"
        phase = Phase.Idle
    }

    private fun onMessage(m: JSONObject) {
        when (m.optString("type")) {
            "state" -> when (m.optString("state")) {
                "transcribing" -> if (phase != Phase.Listening) phase = Phase.Transcribing
                "waiting" -> phase = Phase.Waiting
                "thinking" -> phase = Phase.Thinking
                "idle" -> if (phase in setOf(Phase.Transcribing, Phase.Waiting, Phase.Thinking)) phase = Phase.Idle
            }
            "transcript" -> {
                val text = m.optString("text")
                if (text.isBlank()) info("Didn't catch that.") else add(Role.User, text)
            }
            "turn" -> {
                cutBubble()
                turnId = m.optInt("id")
                turnUsesPcVoice = prefs.serverVoice
                shown = ""
                target = ""
                bubble = add(Role.Jarvis, "", live = true)
            }
            "thought" -> {
                if (m.optInt("id") != turnId) return
                val b = bubble ?: return
                if (!b.thinking && b.thinkSecs == null) {
                    b.thinking = true
                    b.thinkStart = System.currentTimeMillis()
                }
                b.thoughts += m.optString("text")
                if (!m.isNull("secs")) b.endThinking(m.optDouble("secs"))
            }
            "notice" -> info(m.optString("message"))
            "done" -> {
                val id = m.optInt("id")
                if (id != turnId) return
                if (turnUsesPcVoice) player.mark(endOfReply = true) { finishTurn(id) }
                else phoneVoice.mark { finishTurn(id) }
            }
            "error" -> {
                info(m.optString("message", "Something went wrong on the PC."))
                if (phase in setOf(Phase.Transcribing, Phase.Waiting, Phase.Thinking)) phase = Phase.Idle
            }
        }
    }

    private fun onChunk(c: ClientEvent.Chunk) {
        if (c.id != turnId) return
        val id = c.id
        when {
            c.pcm != null -> player.play(c.pcm, c.rate) { onSpoken(id, c.text, c.duration) }
            !turnUsesPcVoice -> {
                val clean = PhoneVoice.clean(c.text)
                if (clean.isEmpty()) phoneVoice.mark { onSpoken(id, c.text, 0.0) }
                else phoneVoice.speak(clean) {
                    onSpoken(id, c.text, clean.length * 0.065 / phoneVoice.rate)
                }
            }
            else -> player.mark(endOfReply = false) { onSpoken(id, c.text, 0.0) }
        }
    }

    /** A piece of the reply just started playing: type it out over as long as it's spoken. */
    private fun onSpoken(id: Int, text: String, duration: Double) {
        if (id != turnId) return
        val b = bubble ?: return
        phase = Phase.Speaking
        revealJob?.cancel()
        shown = target
        target += text
        val words = Regex("\\s*\\S+\\s*").findAll(text).map { it.value }.toList()
        if (duration <= 0 || words.size < 2) {
            shown = target
            b.text = shown.trimStart()
            return
        }
        val step = (duration * 1000 / words.size).toLong()
        revealJob = viewModelScope.launch {
            for (w in words) {
                shown += w
                b.text = shown.trimStart()
                delay(step)
            }
        }
    }

    private fun finishTurn(id: Int) {
        if (id != turnId) return
        revealJob?.cancel()
        bubble?.let {
            it.text = target.trim()
            it.live = false
            it.endThinking()
            if (it.text.isEmpty()) {
                if (it.thoughts.isBlank()) messages.remove(it) else it.text = NO_ANSWER
            }
        }
        bubble = null
        turnId = -1
        level.floatValue = 0f
        phase = if (client.connected) Phase.Idle else Phase.Offline
        if (prefs.handsFree && client.connected) startListening()
    }

    /** Interrupted: keep only what was actually said. */
    private fun cutBubble() {
        revealJob?.cancel()
        bubble?.let {
            it.live = false
            it.endThinking()
            val said = shown.trim()
            when {
                said.isNotEmpty() -> it.text = "$said —"
                it.thoughts.isNotBlank() -> it.text = NO_ANSWER
                else -> messages.remove(it)
            }
        }
        bubble = null
        turnId = -1
        level.floatValue = 0f
        if (phase == Phase.Speaking || phase == Phase.Thinking) phase = if (client.connected) Phase.Idle else Phase.Offline
    }

    private fun add(role: Role, text: String, live: Boolean = false): ChatMsg {
        val msg = ChatMsg(nextId++, role, text, live)
        messages.add(msg)
        return msg
    }

    private fun info(text: String) {
        add(Role.Info, text)
    }

    override fun onCleared() {
        client.disconnect()
        mic.cancel()
        player.release()
        phoneVoice.shutdown()
    }
}

const val NO_ANSWER = "Stopped before it could answer."

private fun JSONArray?.strings(): List<String> =
    if (this == null) emptyList() else List(length()) { optString(it) }

/** [[id, label], ...] -> pairs */
private fun JSONArray?.pairs(): List<Pair<String, String>> {
    if (this == null) return emptyList()
    return List(length()) { i ->
        val a = optJSONArray(i)
        if (a != null) a.optString(0) to a.optString(1, a.optString(0)) else optString(i) to optString(i)
    }
}

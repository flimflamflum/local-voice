package com.localvoice.jarvis

import android.os.Handler
import android.os.Looper
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString
import okio.ByteString.Companion.toByteString
import org.json.JSONObject
import java.net.ConnectException
import java.net.NoRouteToHostException
import java.net.SocketTimeoutException
import java.net.UnknownHostException
import java.util.concurrent.TimeUnit

sealed interface ClientEvent {
    data class Connecting(val url: String) : ClientEvent
    data class Welcome(val msg: JSONObject) : ClientEvent
    data class Disconnected(val reason: String, val fatal: Boolean) : ClientEvent
    data class Message(val msg: JSONObject) : ClientEvent

    /** One spoken piece of a reply. [pcm] is 16-bit mono audio, or null when the PC sent text only. */
    class Chunk(val id: Int, val text: String, val duration: Double, val rate: Int, val pcm: ByteArray?) : ClientEvent
}

/**
 * WebSocket link to server.py on the PC. Reconnects by itself; every event is delivered on
 * the main thread.
 */
class JarvisClient(private val onEvent: (ClientEvent) -> Unit) {
    private val http = OkHttpClient.Builder()
        .connectTimeout(8, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.MILLISECONDS)
        .pingInterval(20, TimeUnit.SECONDS)
        .build()
    private val main = Handler(Looper.getMainLooper())

    @Volatile private var ws: WebSocket? = null
    @Volatile var connected = false
        private set

    private var url: String? = null
    private var hello: (() -> JSONObject)? = null
    private var retryDelay = 1000L
    private val retry = Runnable { open() }

    /** [makeHello] builds the first message (token + settings); it's called on every (re)connect. */
    fun connect(server: String, makeHello: () -> JSONObject) {
        main.removeCallbacks(retry)
        val target = wsUrl(server)
        if (target == null) {
            url = null
            onEvent(ClientEvent.Disconnected("Add your PC's address in settings", true))
            return
        }
        url = target
        hello = makeHello
        retryDelay = 1000
        open()
    }

    fun disconnect() {
        url = null
        main.removeCallbacks(retry)
        ws?.close(1000, null)
        ws = null
        connected = false
    }

    fun send(msg: JSONObject): Boolean = connected && ws?.send(msg.toString()) == true

    fun sendAudio(data: ByteArray, length: Int) {
        if (connected) ws?.send(data.toByteString(0, length))
    }

    private fun open() {
        val target = url ?: return
        ws?.cancel()
        connected = false
        onEvent(ClientEvent.Connecting(target))
        val request = try {
            Request.Builder().url(target).build()
        } catch (e: IllegalArgumentException) {
            url = null
            onEvent(ClientEvent.Disconnected("That address doesn't look right", true))
            return
        }
        ws = http.newWebSocket(request, Listener())
    }

    private fun emit(event: ClientEvent) {
        main.post { onEvent(event) }
    }

    private inner class Listener : WebSocketListener() {
        private var pendingChunk: JSONObject? = null  // chunk header waiting for its audio frame

        override fun onOpen(webSocket: WebSocket, response: Response) {
            val makeHello = hello ?: return
            main.post {
                if (webSocket === ws) webSocket.send(makeHello().toString())
            }
        }

        override fun onMessage(webSocket: WebSocket, text: String) {
            if (webSocket !== ws) return
            val msg = try {
                JSONObject(text)
            } catch (e: Exception) {
                return
            }
            when (msg.optString("type")) {
                "welcome" -> main.post {
                    if (webSocket === ws) {
                        connected = true
                        retryDelay = 1000
                        onEvent(ClientEvent.Welcome(msg))
                    }
                }
                "chunk" -> if (msg.optInt("bytes") > 0) pendingChunk = msg else emit(chunk(msg, null))
                else -> emit(ClientEvent.Message(msg))
            }
        }

        override fun onMessage(webSocket: WebSocket, bytes: ByteString) {
            if (webSocket !== ws) return
            val meta = pendingChunk ?: return
            pendingChunk = null
            emit(chunk(meta, bytes.toByteArray()))
        }

        override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
            webSocket.close(1000, null)
            lost(webSocket, code, null)
        }

        override fun onClosed(webSocket: WebSocket, code: Int, reason: String) = lost(webSocket, code, null)

        override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) = lost(webSocket, 0, t)
    }

    private fun chunk(m: JSONObject, pcm: ByteArray?) = ClientEvent.Chunk(
        id = m.optInt("id"),
        text = m.optString("text"),
        duration = m.optDouble("duration", 0.0),
        rate = m.optInt("rate", 24000),
        pcm = pcm,
    )

    private fun lost(webSocket: WebSocket, code: Int, error: Throwable?) {
        main.post {
            if (webSocket !== ws) return@post
            ws = null
            connected = false
            if (code == 4001) {
                url = null  // don't hammer the PC with a wrong token
                onEvent(ClientEvent.Disconnected("Wrong token - check settings", true))
                return@post
            }
            val reason = when (error) {
                null -> "Disconnected"
                is ConnectException, is NoRouteToHostException, is SocketTimeoutException ->
                    "Can't reach your PC - is the server running?"
                is UnknownHostException -> "Unknown address"
                else -> error.message ?: "Connection lost"
            }
            onEvent(ClientEvent.Disconnected(reason, false))
            if (url != null) {
                main.postDelayed(retry, retryDelay)
                retryDelay = (retryDelay * 2).coerceAtMost(10_000)
            }
        }
    }

    companion object {
        /** "100.64.1.2" -> "ws://100.64.1.2:8765/ws"; also accepts http(s)://, ws(s)://, custom ports and paths. */
        fun wsUrl(input: String): String? {
            var s = input.trim().trimEnd('/')
            if (s.isEmpty()) return null
            var scheme = "ws"
            Regex("^([a-zA-Z]+)://").find(s)?.let { m ->
                scheme = if (m.groupValues[1].lowercase() in setOf("https", "wss")) "wss" else "ws"
                s = s.substring(m.value.length)
            }
            val slash = s.indexOf('/')
            var host = if (slash >= 0) s.substring(0, slash) else s
            var path = if (slash >= 0) s.substring(slash) else ""
            if (host.isEmpty()) return null
            if (':' !in host && scheme == "ws") host += ":8765"
            if (path.isEmpty() || path == "/") path = "/ws"
            return "$scheme://$host$path"
        }
    }
}

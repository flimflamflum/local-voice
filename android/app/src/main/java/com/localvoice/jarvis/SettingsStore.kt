package com.localvoice.jarvis

import android.content.Context
import androidx.datastore.preferences.core.booleanPreferencesKey
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.intPreferencesKey
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import kotlinx.coroutines.flow.first
import java.util.UUID

private val Context.dataStore by preferencesDataStore(name = "jarvis")

/** Everything the app remembers. Empty model/style/voice = use whatever the PC has set. */
data class Prefs(
    val server: String = "",
    val token: String = "",
    val model: String = "",
    val style: String = "",
    val voice: String = "",
    val rate: Int = 1,
    val voiceMode: Boolean = true,   // off = plain text chat: no orb, no mic, no audio
    val serverVoice: Boolean = true,
    val handsFree: Boolean = false,
    val think: Boolean = false,
    val session: String = "",
) {
    val configured get() = server.isNotBlank() && token.isNotBlank()
}

class SettingsStore(private val context: Context) {
    private object K {
        val server = stringPreferencesKey("server")
        val token = stringPreferencesKey("token")
        val model = stringPreferencesKey("model")
        val style = stringPreferencesKey("style")
        val voice = stringPreferencesKey("voice")
        val rate = intPreferencesKey("rate")
        val voiceMode = booleanPreferencesKey("voice_mode")
        val serverVoice = booleanPreferencesKey("server_voice")
        val handsFree = booleanPreferencesKey("hands_free")
        val think = booleanPreferencesKey("think")
        val session = stringPreferencesKey("session")
    }

    suspend fun load(): Prefs {
        val p = context.dataStore.data.first()
        var prefs = Prefs(
            server = p[K.server] ?: "",
            token = p[K.token] ?: "",
            model = p[K.model] ?: "",
            style = p[K.style] ?: "",
            voice = p[K.voice] ?: "",
            rate = p[K.rate] ?: 1,
            voiceMode = p[K.voiceMode] ?: true,
            serverVoice = p[K.serverVoice] ?: true,
            handsFree = p[K.handsFree] ?: false,
            think = p[K.think] ?: false,
            session = p[K.session] ?: "",
        )
        if (prefs.session.isEmpty()) {
            // Lets the PC keep this phone's chat history across reconnects.
            prefs = prefs.copy(session = UUID.randomUUID().toString())
            save(prefs)
        }
        return prefs
    }

    suspend fun save(prefs: Prefs) {
        context.dataStore.edit { e ->
            e[K.server] = prefs.server
            e[K.token] = prefs.token
            e[K.model] = prefs.model
            e[K.style] = prefs.style
            e[K.voice] = prefs.voice
            e[K.rate] = prefs.rate
            e[K.voiceMode] = prefs.voiceMode
            e[K.serverVoice] = prefs.serverVoice
            e[K.handsFree] = prefs.handsFree
            e[K.think] = prefs.think
            e[K.session] = prefs.session
        }
    }
}

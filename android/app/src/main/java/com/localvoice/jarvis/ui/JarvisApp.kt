package com.localvoice.jarvis.ui

import android.Manifest
import androidx.activity.compose.BackHandler
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.slideInVertically
import androidx.compose.animation.slideOutVertically
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.systemBarsPadding
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.OutlinedTextFieldDefaults
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.localvoice.jarvis.ChatMsg
import com.localvoice.jarvis.JarvisViewModel
import com.localvoice.jarvis.Phase
import com.localvoice.jarvis.Role

@Composable
fun JarvisApp(vm: JarvisViewModel) {
    val micLauncher = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) {
        vm.onMicPermission(it)
    }
    LaunchedEffect(vm.requestMic) {
        if (vm.requestMic) micLauncher.launch(Manifest.permission.RECORD_AUDIO)
    }
    BackHandler(enabled = vm.showSettings) { vm.showSettings = false }

    Box(
        Modifier
            .fillMaxSize()
            .background(Brush.verticalGradient(listOf(Color(0xFF0B0D18), Bg, Bg)))
    ) {
        Column(
            Modifier
                .fillMaxSize()
                .systemBarsPadding()
                .imePadding()
        ) {
            TopBar(vm)
            Chat(vm, Modifier.weight(1f))
            val level by vm.level
            Orb(
                phase = vm.phase,
                level = level,
                onTap = vm::onOrbTap,
                modifier = Modifier
                    .size(if (vm.messages.isEmpty()) 260.dp else 150.dp)
                    .align(Alignment.CenterHorizontally),
            )
            Text(
                phaseLabel(vm.phase),
                color = Muted,
                fontSize = 13.sp,
                modifier = Modifier
                    .align(Alignment.CenterHorizontally)
                    .padding(bottom = 10.dp),
            )
            Composer(onSend = vm::sendText)
        }

        AnimatedVisibility(
            visible = vm.showSettings,
            enter = slideInVertically { it / 6 } + fadeIn(),
            exit = slideOutVertically { it / 6 } + fadeOut(),
        ) {
            SettingsScreen(vm)
        }
    }
}

private fun phaseLabel(p: Phase) = when (p) {
    Phase.Offline -> "Offline · tap to reconnect"
    Phase.Connecting -> "Connecting…"
    Phase.Idle -> "Tap the orb to talk"
    Phase.Listening -> "Listening… tap to send"
    Phase.Transcribing -> "Transcribing…"
    Phase.Waiting -> "Waiting for your PC…"
    Phase.Thinking -> "Thinking…"
    Phase.Speaking -> "Speaking · tap to interrupt"
}

@Composable
private fun TopBar(vm: JarvisViewModel) {
    Row(
        Modifier
            .fillMaxWidth()
            .padding(start = 20.dp, end = 8.dp, top = 8.dp, bottom = 4.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Column(Modifier.weight(1f)) {
            Text(vm.assistantName, color = TextColor, fontSize = 22.sp, fontWeight = FontWeight.SemiBold)
            Row(verticalAlignment = Alignment.CenterVertically) {
                val dot = when (vm.phase) {
                    Phase.Offline -> Bad
                    Phase.Connecting -> Warn
                    else -> Good
                }
                Box(
                    Modifier
                        .size(7.dp)
                        .clip(CircleShape)
                        .background(dot)
                )
                Spacer(Modifier.size(6.dp))
                Text(vm.status, color = Muted, fontSize = 12.sp, maxLines = 1)
            }
        }
        IconButton(onClick = vm::newChat) {
            Icon(JarvisIcons.Add, contentDescription = "New chat", tint = Muted)
        }
        IconButton(onClick = { vm.showSettings = true }) {
            Icon(JarvisIcons.Settings, contentDescription = "Settings", tint = Muted)
        }
    }
}

@Composable
private fun Chat(vm: JarvisViewModel, modifier: Modifier) {
    val list = rememberLazyListState()
    val last = vm.messages.lastOrNull()
    LaunchedEffect(vm.messages.size, last?.text) {
        if (vm.messages.isNotEmpty()) list.scrollToItem(vm.messages.size - 1)
    }
    if (vm.messages.isEmpty()) {
        Box(modifier.fillMaxWidth(), contentAlignment = Alignment.BottomCenter) {
            Text(
                "Your PC does the thinking.\nTap the orb and say hi.",
                color = Faint, fontSize = 14.sp, textAlign = TextAlign.Center,
                modifier = Modifier.padding(bottom = 8.dp),
            )
        }
        return
    }
    LazyColumn(
        modifier.fillMaxWidth(),
        state = list,
        contentPadding = PaddingValues(horizontal = 16.dp, vertical = 12.dp),
        verticalArrangement = Arrangement.spacedBy(10.dp),
    ) {
        items(vm.messages, key = { it.id }) { Bubble(it) }
    }
}

@Composable
private fun Bubble(m: ChatMsg) {
    when (m.role) {
        Role.Info -> Text(
            m.text, color = Faint, fontSize = 12.sp, textAlign = TextAlign.Center,
            modifier = Modifier.fillMaxWidth(),
        )
        Role.User, Role.Jarvis -> {
            val user = m.role == Role.User
            Box(Modifier.fillMaxWidth(), contentAlignment = if (user) Alignment.CenterEnd else Alignment.CenterStart) {
                val shape = RoundedCornerShape(
                    topStart = 18.dp, topEnd = 18.dp,
                    bottomStart = if (user) 18.dp else 4.dp, bottomEnd = if (user) 4.dp else 18.dp,
                )
                Box(
                    Modifier
                        .widthIn(max = 310.dp)
                        .clip(shape)
                        .background(if (user) Accent.copy(alpha = 0.16f) else Panel)
                        .border(1.dp, if (user) Accent.copy(alpha = 0.25f) else Line, shape)
                        .padding(horizontal = 14.dp, vertical = 10.dp)
                ) {
                    val text = if (m.live && m.text.isEmpty()) "…" else m.text
                    Text(text, color = TextColor, fontSize = 15.sp, lineHeight = 21.sp)
                }
            }
        }
    }
}

@Composable
private fun Composer(onSend: (String) -> Unit) {
    var text by rememberSaveable { mutableStateOf("") }
    val send: () -> Unit = {
        if (text.isNotBlank()) {
            onSend(text)
            text = ""
        }
    }
    Row(
        Modifier
            .fillMaxWidth()
            .padding(start = 12.dp, end = 6.dp, bottom = 10.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        OutlinedTextField(
            value = text,
            onValueChange = { text = it },
            placeholder = { Text("Type instead…", color = Faint) },
            maxLines = 4,
            shape = RoundedCornerShape(22.dp),
            keyboardOptions = KeyboardOptions(imeAction = ImeAction.Send),
            keyboardActions = KeyboardActions(onSend = { send() }),
            colors = fieldColors(),
            modifier = Modifier.weight(1f),
        )
        IconButton(onClick = send, enabled = text.isNotBlank()) {
            Icon(JarvisIcons.Send, contentDescription = "Send", tint = if (text.isNotBlank()) Accent else Faint)
        }
    }
}

@Composable
fun fieldColors() = OutlinedTextFieldDefaults.colors(
    focusedTextColor = TextColor,
    unfocusedTextColor = TextColor,
    focusedContainerColor = Panel,
    unfocusedContainerColor = Panel,
    focusedBorderColor = Accent.copy(alpha = 0.6f),
    unfocusedBorderColor = LineStrong,
    cursorColor = Accent,
    focusedLabelColor = Accent,
    unfocusedLabelColor = Muted,
)

@Composable
fun SectionTitle(text: String) {
    Text(
        text.uppercase(), color = Faint, fontSize = 11.sp, letterSpacing = 1.4.sp,
        fontWeight = FontWeight.SemiBold, modifier = Modifier.padding(top = 22.dp, bottom = 8.dp),
    )
}

@Composable
fun Gap(h: Int = 12) = Spacer(Modifier.height(h.dp))

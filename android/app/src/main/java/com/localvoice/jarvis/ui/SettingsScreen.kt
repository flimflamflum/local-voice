package com.localvoice.jarvis.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.systemBarsPadding
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Slider
import androidx.compose.material3.SliderDefaults
import androidx.compose.material3.Switch
import androidx.compose.material3.SwitchDefaults
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.foundation.layout.height
import androidx.compose.ui.draw.clip
import androidx.compose.ui.platform.LocalContext
import com.google.mlkit.vision.barcode.common.Barcode
import com.google.mlkit.vision.codescanner.GmsBarcodeScannerOptions
import com.google.mlkit.vision.codescanner.GmsBarcodeScanning
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.localvoice.jarvis.JarvisViewModel
import com.localvoice.jarvis.Phase
import kotlin.math.roundToInt

@Composable
fun SettingsScreen(vm: JarvisViewModel) {
    val p = vm.prefs
    Box(
        Modifier
            .fillMaxSize()
            .background(Bg)
            .systemBarsPadding()
            .imePadding()
    ) {
        Column(
            Modifier
                .fillMaxSize()
                .verticalScroll(rememberScrollState())
                .padding(horizontal = 20.dp, vertical = 8.dp)
        ) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text("Settings", color = TextColor, fontSize = 22.sp, fontWeight = FontWeight.SemiBold, modifier = Modifier.weight(1f))
                IconButton(onClick = { vm.showSettings = false }) {
                    Icon(JarvisIcons.Close, contentDescription = "Close", tint = Muted)
                }
            }

            // ------------------------------------------------------------ connection
            SectionTitle("Your PC")
            val context = LocalContext.current
            Button(
                onClick = {
                    val options = GmsBarcodeScannerOptions.Builder()
                        .setBarcodeFormats(Barcode.FORMAT_QR_CODE)
                        .enableAutoZoom()
                        .build()
                    GmsBarcodeScanning.getClient(context, options).startScan()
                        .addOnSuccessListener { code -> vm.applyLink(code.rawValue ?: "") }
                        .addOnFailureListener { e -> vm.scanFailed("Scanner unavailable: ${e.message ?: "unknown error"}") }
                },
                colors = ButtonDefaults.buttonColors(containerColor = Accent, contentColor = Bg),
                shape = RoundedCornerShape(14.dp),
                modifier = Modifier
                    .fillMaxWidth()
                    .height(52.dp),
            ) { Text("Scan QR code", fontSize = 16.sp, fontWeight = FontWeight.SemiBold) }
            Gap(6)
            Text(
                "On your PC: Jarvis desktop app → Settings → Phone. Or type it in:",
                color = Faint, fontSize = 12.sp, lineHeight = 17.sp,
            )
            Gap(10)
            var server by remember(p.server) { mutableStateOf(p.server) }
            var token by remember(p.token) { mutableStateOf(p.token) }
            OutlinedTextField(
                value = server, onValueChange = { server = it },
                label = { Text("Address") }, placeholder = { Text("100.x.y.z:8765", color = Faint) },
                singleLine = true, colors = fieldColors(),
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri),
                modifier = Modifier.fillMaxWidth(),
            )
            Gap(8)
            OutlinedTextField(
                value = token, onValueChange = { token = it },
                label = { Text("Token") }, singleLine = true, colors = fieldColors(),
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password),
                modifier = Modifier.fillMaxWidth(),
            )
            Gap(10)
            Row(verticalAlignment = Alignment.CenterVertically) {
                OutlinedButton(
                    onClick = { vm.saveConnection(server, token) },
                    enabled = server.isNotBlank() && token.isNotBlank(),
                    colors = ButtonDefaults.outlinedButtonColors(contentColor = Accent),
                ) { Text("Connect") }
                val color = when (vm.phase) {
                    Phase.Offline -> Bad
                    Phase.Connecting -> Warn
                    else -> Good
                }
                Text(vm.status, color = color, fontSize = 13.sp, modifier = Modifier.padding(start = 14.dp))
            }
            Gap(8)
            Text(
                "The phone server has to be running on your PC: start it from the desktop app's " +
                    "Phone settings, or run run-server.bat. Pick the Tailscale address (starts with 100.) " +
                    "to talk to Jarvis from anywhere.",
                color = Faint, fontSize = 12.sp, lineHeight = 17.sp,
            )

            // ------------------------------------------------------------ brain
            SectionTitle("Jarvis")
            Picker("Model", vm.models.map { it to it }, p.model) { id -> vm.updatePrefs { it.copy(model = id) } }
            Gap()
            Picker("Personality", vm.styles, p.style) { id -> vm.updatePrefs { it.copy(style = id) } }

            // ------------------------------------------------------------ voice
            SectionTitle("Voice")
            Toggle(
                "PC voice (Kokoro)",
                if (p.serverVoice) "Your PC speaks and streams the audio to this phone."
                else "Using this phone's own voice: text only, less data.",
                p.serverVoice,
            ) { on -> vm.updatePrefs { it.copy(serverVoice = on) } }
            Gap()
            if (p.serverVoice) {
                Picker("Voice", vm.voices, p.voice) { id -> vm.updatePrefs { it.copy(voice = id) } }
                Gap()
            }
            var rate by remember(p.rate) { mutableFloatStateOf(p.rate.toFloat()) }
            val r = rate.roundToInt()
            Text("Speed  ${if (r > 0) "+$r" else "$r"}", color = Muted, fontSize = 13.sp)
            Slider(
                value = rate, onValueChange = { rate = it },
                onValueChangeFinished = { vm.updatePrefs { it.copy(rate = rate.roundToInt()) } },
                valueRange = -5f..6f, steps = 10,
                colors = SliderDefaults.colors(thumbColor = Accent, activeTrackColor = Accent, inactiveTrackColor = LineStrong),
            )
            Toggle(
                "Hands-free",
                "Start listening again after every reply.",
                p.handsFree,
            ) { on -> vm.updatePrefs { it.copy(handsFree = on) } }
            Gap(32)
        }
    }
}

@Composable
private fun Toggle(title: String, subtitle: String, checked: Boolean, onChange: (Boolean) -> Unit) {
    Row(
        Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(14.dp))
            .clickable { onChange(!checked) }
            .padding(vertical = 6.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Column(Modifier.weight(1f)) {
            Text(title, color = TextColor, fontSize = 15.sp)
            Text(subtitle, color = Faint, fontSize = 12.sp, lineHeight = 16.sp)
        }
        Switch(
            checked = checked, onCheckedChange = onChange,
            colors = SwitchDefaults.colors(checkedThumbColor = Bg, checkedTrackColor = Accent),
        )
    }
}

/** A simple dropdown: [options] are (id, label) pairs. */
@Composable
private fun Picker(label: String, options: List<Pair<String, String>>, selected: String, onSelect: (String) -> Unit) {
    var open by remember { mutableStateOf(false) }
    val current = options.firstOrNull { it.first == selected }?.second ?: selected.ifEmpty { "—" }
    val shape = RoundedCornerShape(12.dp)
    Column {
        Text(label, color = Muted, fontSize = 13.sp, modifier = Modifier.padding(bottom = 6.dp))
        Box {
            Row(
                Modifier
                    .fillMaxWidth()
                    .clip(shape)
                    .background(Panel)
                    .border(1.dp, LineStrong, shape)
                    .clickable(enabled = options.isNotEmpty()) { open = true }
                    .padding(horizontal = 14.dp, vertical = 13.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Text(current, color = TextColor, fontSize = 15.sp, maxLines = 1, overflow = TextOverflow.Ellipsis, modifier = Modifier.weight(1f))
                Text("▾", color = Muted)
            }
            DropdownMenu(expanded = open, onDismissRequest = { open = false }) {
                options.forEach { (id, name) ->
                    DropdownMenuItem(
                        text = { Text(name, color = if (id == selected) Accent else TextColor) },
                        onClick = {
                            open = false
                            onSelect(id)
                        },
                    )
                }
            }
        }
    }
}

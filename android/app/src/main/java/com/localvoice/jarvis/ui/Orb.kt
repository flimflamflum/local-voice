package com.localvoice.jarvis.ui

import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.clickable
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.graphics.drawscope.rotate
import androidx.compose.ui.semantics.Role
import com.localvoice.jarvis.Phase
import kotlin.math.PI
import kotlin.math.sin

private fun tintFor(phase: Phase) = when (phase) {
    Phase.Offline, Phase.Connecting -> Color(0xFF4A5068)
    Phase.Idle -> Color(0xFF3FA9D6)
    Phase.Listening -> Accent
    Phase.Speaking -> Color(0xFF6FE3FF)
    Phase.Transcribing, Phase.Waiting, Phase.Thinking -> Accent2
}

/** The glowing orb. It breathes when idle, swells with your voice and with the assistant's. */
@Composable
fun Orb(phase: Phase, level: Float, onTap: () -> Unit, modifier: Modifier = Modifier) {
    val transition = rememberInfiniteTransition(label = "orb")
    val t by transition.animateFloat(
        0f, (2 * PI).toFloat(), infiniteRepeatable(tween(4000, easing = LinearEasing)), label = "t",
    )
    val spin by transition.animateFloat(
        0f, 360f, infiniteRepeatable(tween(1300, easing = LinearEasing)), label = "spin",
    )
    val smooth by animateFloatAsState(level, tween(90), label = "level")
    val tint by animateColorAsState(tintFor(phase), tween(450), label = "tint")
    val busy = phase in setOf(Phase.Connecting, Phase.Transcribing, Phase.Waiting, Phase.Thinking)

    Canvas(
        modifier.clickable(
            interactionSource = remember { MutableInteractionSource() },
            indication = null,
            role = Role.Button,
            onClickLabel = "Talk",
            onClick = onTap,
        )
    ) {
        val c = center
        val base = size.minDimension * 0.27f
        val r = base * (1f + 0.025f * sin(t) + 0.22f * smooth)

        // glow
        val glow = r * 1.9f
        drawCircle(
            Brush.radialGradient(
                listOf(tint.copy(alpha = 0.42f + 0.35f * smooth), tint.copy(alpha = 0.10f), Color.Transparent),
                center = c, radius = glow,
            ),
            radius = glow, center = c,
        )

        // listening ripples
        if (phase == Phase.Listening) {
            for (i in 0 until 3) {
                val p = ((t / (2 * PI).toFloat()) * 2f + i / 3f) % 1f
                drawCircle(
                    tint.copy(alpha = (1f - p) * 0.45f),
                    radius = r * (1.05f + p * 0.6f), center = c,
                    style = Stroke(width = 2f * density),
                )
            }
        }

        // core
        drawCircle(
            Brush.radialGradient(
                listOf(Color(0xFFE6FBFF), tint, Accent2),
                center = c + Offset(-r * 0.3f, -r * 0.35f), radius = r * 1.55f,
            ),
            radius = r, center = c,
        )
        drawCircle(Color.White.copy(alpha = 0.16f), radius = r, center = c, style = Stroke(width = 1.2f * density))

        // thinking spinner
        if (busy) {
            val ring = r * 1.28f
            rotate(spin, c) {
                drawArc(
                    Brush.sweepGradient(listOf(Color.Transparent, tint, Color.Transparent), center = c),
                    startAngle = 0f, sweepAngle = 300f, useCenter = false,
                    topLeft = c - Offset(ring, ring), size = Size(ring * 2, ring * 2),
                    style = Stroke(width = 3f * density, cap = StrokeCap.Round),
                )
            }
        }
    }
}

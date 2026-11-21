package com.localvoice.jarvis.ui

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.graphics.vector.addPathNodes
import androidx.compose.ui.unit.dp

// Same palette as the desktop app (ui/style.css).
val Bg = Color(0xFF06070C)
val Panel = Color(0xFF12141F)
val PanelSolid = Color(0xFF10121C)
val Line = Color(0x12FFFFFF)
val LineStrong = Color(0x24FFFFFF)
val TextColor = Color(0xFFE9ECF5)
val Muted = Color(0xFF8A90A6)
val Faint = Color(0xFF5A6078)
val Accent = Color(0xFF4FD1FF)
val Accent2 = Color(0xFF7C5CFF)
val Good = Color(0xFF3DDC97)
val Warn = Color(0xFFFFB547)
val Bad = Color(0xFFFF5C7A)

@Composable
fun JarvisTheme(content: @Composable () -> Unit) {
    MaterialTheme(
        colorScheme = darkColorScheme(
            primary = Accent,
            onPrimary = Bg,
            secondary = Accent2,
            background = Bg,
            onBackground = TextColor,
            surface = PanelSolid,
            onSurface = TextColor,
            surfaceVariant = Panel,
            onSurfaceVariant = Muted,
            surfaceContainer = PanelSolid,
            surfaceContainerHigh = PanelSolid,
            outline = LineStrong,
        ),
        content = content,
    )
}

/** Tiny hand-picked icon set (Material paths) so we don't pull in the whole icons library. */
object JarvisIcons {
    private fun icon(name: String, path: String) =
        ImageVector.Builder(name, 24.dp, 24.dp, 24f, 24f)
            .addPath(pathData = addPathNodes(path), fill = SolidColor(Color.White))
            .build()

    val Send = icon("send", "M2.01 21L23 12 2.01 3 2 10l15 2-15 2z")
    val Settings = icon(
        "tune",
        "M3 17v2h6v-2H3zM3 5v2h10V5H3zm10 16v-2h8v-2h-8v-2h-2v6h2zM7 9v2H3v2h4v2h2V9H7zm14 4v-2H11v2h10zm-6-4h2V7h4V5h-4V3h-2v6z",
    )
    val Add = icon("add", "M19 13h-6v6h-2v-6H5v-2h6V5h2v6h6v2z")
    val Close = icon(
        "close",
        "M19 6.41L17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z",
    )
}

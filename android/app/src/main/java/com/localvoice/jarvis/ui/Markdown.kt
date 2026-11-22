package com.localvoice.jarvis.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.drawBehind
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.drawscope.DrawScope
import androidx.compose.ui.layout.Layout
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextDecoration
import androidx.compose.ui.text.withStyle
import androidx.compose.ui.unit.Constraints
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

/*
 * Replies can contain Markdown: headings, tables, lists, dividers and code. This is a small
 * hand-written renderer (no library, to keep the APK small), matching the desktop app.
 */

sealed interface MdBlock {
    data class Heading(val level: Int, val text: String) : MdBlock
    data class Paragraph(val lines: List<String>) : MdBlock
    data class ListBlock(val ordered: Boolean, val start: Int, val items: List<String>) : MdBlock
    data class Table(val header: List<String>, val aligns: List<TextAlign>, val rows: List<List<String>>) : MdBlock
    data class Code(val text: String) : MdBlock
    data object Rule : MdBlock
}

private val HEADING = Regex("^\\s{0,3}(#{1,6})\\s+(.*?)\\s*#*\\s*$")
private val RULE = Regex("^\\s{0,3}([-*_])(\\s*\\1){2,}\\s*$")
private val TABLE_RULE = Regex("^\\s*\\|?\\s*:?-{2,}:?\\s*(\\|\\s*:?-{2,}:?\\s*)*\\|?\\s*$")
private val BULLET = Regex("^\\s*[-*•+]\\s+(.*)$")
private val NUMBERED = Regex("^\\s*(\\d+)[.)]\\s+(.*)$")
private val CONTINUATION = Regex("^\\s{2,}\\S.*")

private fun isTableRow(l: String) = l.contains('|') && l.trim().length > 1
private fun splitRow(l: String) = l.trim().removePrefix("|").removeSuffix("|").split('|').map { it.trim() }

fun parseMarkdown(text: String): List<MdBlock> {
    val out = ArrayList<MdBlock>()
    // ``` code fences first; everything outside them is parsed line by line.
    text.split("```").forEachIndexed { i, part ->
        if (i % 2 == 1) out += MdBlock.Code(part.replaceFirst(Regex("^[\\w+-]*\\n"), "").trimEnd())
        else parseBlocks(part, out)
    }
    return out
}

private fun parseBlocks(src: String, out: MutableList<MdBlock>) {
    val lines = src.split('\n')
    val para = ArrayList<String>()
    var listOrdered = false
    var listStart = 1
    var items: MutableList<String>? = null

    fun flushPara() {
        if (para.isNotEmpty()) out += MdBlock.Paragraph(para.toList())
        para.clear()
    }
    fun flushList() {
        items?.let { out += MdBlock.ListBlock(listOrdered, listStart, it.toList()) }
        items = null
    }
    fun addItem(ordered: Boolean, start: Int, text: String) {
        flushPara()
        if (items == null || listOrdered != ordered) {
            flushList()
            listOrdered = ordered
            listStart = start
            items = ArrayList()
        }
        items!!.add(text)
    }

    var i = 0
    while (i < lines.size) {
        val line = lines[i]
        val next = lines.getOrNull(i + 1) ?: ""
        var m: MatchResult? = null
        when {
            line.isBlank() -> { flushPara(); flushList() }
            HEADING.matchEntire(line)?.also { m = it } != null -> {
                flushPara(); flushList()
                out += MdBlock.Heading(m!!.groupValues[1].length, m!!.groupValues[2])
            }
            RULE.matches(line) -> { flushPara(); flushList(); out += MdBlock.Rule }
            isTableRow(line) && next.contains('-') && TABLE_RULE.matches(next) -> {
                flushPara(); flushList()
                val header = splitRow(line)
                val aligns = splitRow(next).map {
                    when {
                        it.startsWith(":") && it.endsWith(":") -> TextAlign.Center
                        it.endsWith(":") -> TextAlign.End
                        else -> TextAlign.Start
                    }
                }
                val rows = ArrayList<List<String>>()
                var j = i + 2
                while (j < lines.size && isTableRow(lines[j])) rows += splitRow(lines[j++])
                out += MdBlock.Table(header, aligns, rows)
                i = j - 1
            }
            BULLET.matchEntire(line)?.also { m = it } != null -> addItem(false, 1, m!!.groupValues[1])
            NUMBERED.matchEntire(line)?.also { m = it } != null ->
                addItem(true, m!!.groupValues[1].toIntOrNull() ?: 1, m!!.groupValues[2])
            items != null && CONTINUATION.matches(line) -> {
                val list = items!!
                list[list.size - 1] = list.last() + " " + line.trim() // wrapped list item
            }
            else -> { flushList(); para += line }
        }
        i++
    }
    flushPara()
    flushList()
}

private val INLINE = Regex(
    "\\*\\*([^*\\n]+)\\*\\*|__([^_\\n]+)__|`([^`\\n]+)`|~~([^~\\n]+)~~|\\[([^\\]\\n]+)]\\([^)\\s]*\\)|" +
        "(?<![*\\w])\\*([^*\\n]+)\\*|(?<![_\\w])_([^_\\n]+)_(?!\\w)"
)

/** **bold**, *italic*, `code`, ~~strike~~ and [links](...) as styled text. */
fun inlineMarkdown(s: String): AnnotatedString = buildAnnotatedString {
    var pos = 0
    for (m in INLINE.findAll(s)) {
        append(s.substring(pos, m.range.first))
        val g = m.groupValues
        when {
            g[1].isNotEmpty() -> withStyle(SpanStyle(fontWeight = FontWeight.SemiBold)) { append(g[1]) }
            g[2].isNotEmpty() -> withStyle(SpanStyle(fontWeight = FontWeight.SemiBold)) { append(g[2]) }
            g[3].isNotEmpty() -> withStyle(
                SpanStyle(fontFamily = FontFamily.Monospace, background = Color(0x12FFFFFF), fontSize = 13.sp)
            ) { append(g[3]) }
            g[4].isNotEmpty() -> withStyle(SpanStyle(textDecoration = TextDecoration.LineThrough)) { append(g[4]) }
            g[5].isNotEmpty() -> withStyle(SpanStyle(color = Accent, textDecoration = TextDecoration.Underline)) { append(g[5]) }
            g[6].isNotEmpty() -> withStyle(SpanStyle(fontStyle = FontStyle.Italic)) { append(g[6]) }
            g[7].isNotEmpty() -> withStyle(SpanStyle(fontStyle = FontStyle.Italic)) { append(g[7]) }
        }
        pos = m.range.last + 1
    }
    append(s.substring(pos))
}

@Composable
fun Markdown(text: String, modifier: Modifier = Modifier) {
    val blocks = remember(text) { parseMarkdown(text) }
    Column(modifier) {
        blocks.forEachIndexed { i, b ->
            val top = when {
                i == 0 -> 0.dp
                b is MdBlock.Heading -> 14.dp
                else -> 7.dp
            }
            Box(Modifier.padding(top = top)) { MdBlockView(b) }
        }
    }
}

@Composable
private fun MdBlockView(b: MdBlock) {
    when (b) {
        is MdBlock.Heading -> {
            val size = when (b.level) { 1 -> 20.sp; 2 -> 18.sp; 3 -> 16.5.sp; else -> 15.sp }
            Column {
                Text(
                    inlineMarkdown(b.text),
                    color = if (b.level >= 4) Muted else TextColor,
                    fontSize = size, lineHeight = size * 1.3f, fontWeight = FontWeight.SemiBold,
                )
                if (b.level <= 2) {
                    Box(
                        Modifier
                            .padding(top = 5.dp)
                            .fillMaxWidth()
                            .height(1.dp)
                            .background(if (b.level == 1) LineStrong else Line)
                    )
                }
            }
        }
        is MdBlock.Paragraph -> Text(
            inlineMarkdown(b.lines.joinToString("\n")),
            color = TextColor, fontSize = 15.sp, lineHeight = 21.sp,
        )
        is MdBlock.ListBlock -> Column {
            b.items.forEachIndexed { i, item ->
                Row(Modifier.padding(vertical = 2.dp)) {
                    Text(
                        if (b.ordered) "${b.start + i}." else "•",
                        color = Accent, fontSize = 15.sp, lineHeight = 21.sp,
                        modifier = Modifier.widthIn(min = if (b.ordered) 22.dp else 16.dp),
                    )
                    Text(inlineMarkdown(item), color = TextColor, fontSize = 15.sp, lineHeight = 21.sp)
                }
            }
        }
        is MdBlock.Table -> TableView(b)
        is MdBlock.Code -> Text(
            b.text,
            color = TextColor, fontSize = 12.5.sp, lineHeight = 17.sp, fontFamily = FontFamily.Monospace,
            modifier = Modifier
                .clip(RoundedCornerShape(10.dp))
                .background(Color(0x59000000))
                .border(1.dp, Line, RoundedCornerShape(10.dp))
                .horizontalScroll(rememberScrollState())
                .padding(horizontal = 12.dp, vertical = 10.dp),
        )
        MdBlock.Rule -> Box(
            Modifier
                .padding(vertical = 6.dp)
                .fillMaxWidth()
                .height(1.dp)
                .background(LineStrong)
        )
    }
}

/** Where the last layout put the column/row edges, for drawing the grid lines. */
private class Grid {
    var widths = IntArray(0)
    var heights = IntArray(0)

    fun draw(scope: DrawScope) = with(scope) {
        if (heights.isEmpty()) return@with
        val line = 1.dp.toPx()
        drawRect(Color(0x0FFFFFFF), size = Size(size.width, heights[0].toFloat())) // header row
        var y = 0f
        for (r in 0 until heights.size - 1) {
            y += heights[r]
            drawLine(if (r == 0) LineStrong else Line, Offset(0f, y), Offset(size.width, y), line)
        }
        var x = 0f
        for (c in 0 until widths.size - 1) {
            x += widths[c]
            drawLine(Line, Offset(x, 0f), Offset(x, size.height), line)
        }
    }
}

/** A real grid: every column as wide as its widest cell (up to a cap), scrolls sideways if needed. */
@Composable
private fun TableView(t: MdBlock.Table) {
    val cols = t.header.size.coerceAtLeast(1)
    val rows = listOf(t.header) + t.rows.map { r -> List(cols) { r.getOrElse(it) { "" } } }
    val grid = remember { Grid() }
    val shape = RoundedCornerShape(10.dp)
    Box(
        Modifier
            .clip(shape)
            .border(1.dp, LineStrong, shape)
            .horizontalScroll(rememberScrollState())
    ) {
        Layout(
            content = {
                rows.forEachIndexed { r, row ->
                    row.forEachIndexed { c, cell ->
                        Text(
                            inlineMarkdown(cell),
                            color = if (r == 0) TextColor else TextColor.copy(alpha = 0.92f),
                            fontSize = 13.sp, lineHeight = 18.sp,
                            fontWeight = if (r == 0) FontWeight.SemiBold else null,
                            textAlign = t.aligns.getOrElse(c) { TextAlign.Start },
                            modifier = Modifier.padding(horizontal = 10.dp, vertical = 7.dp),
                        )
                    }
                }
            },
            modifier = Modifier.drawBehind { grid.draw(this) },
        ) { measurables, _ ->
            val maxCol = 200.dp.roundToPx()
            val minCol = 44.dp.roundToPx()
            val widths = IntArray(cols) { minCol }
            measurables.forEachIndexed { i, m ->
                val c = i % cols
                widths[c] = maxOf(widths[c], minOf(m.maxIntrinsicWidth(Constraints.Infinity), maxCol))
            }
            val placeables = measurables.mapIndexed { i, m -> m.measure(Constraints.fixedWidth(widths[i % cols])) }
            val heights = IntArray(rows.size) { r -> (0 until cols).maxOf { placeables[r * cols + it].height } }
            grid.widths = widths
            grid.heights = heights
            layout(widths.sum(), heights.sum()) {
                var y = 0
                for (r in rows.indices) {
                    var x = 0
                    for (c in 0 until cols) {
                        placeables[r * cols + c].place(x, y)
                        x += widths[c]
                    }
                    y += heights[r]
                }
            }
        }
    }
}

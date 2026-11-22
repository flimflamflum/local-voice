"use strict";

const $ = (id) => document.getElementById(id);
const body = document.body;

const STATUS = {
  idle:         ["Ready",       'Press <kbd>Space</kbd> or tap the mic to talk'],
  listening:    ["Listening…",  "Go ahead. I'll stop when you pause, or hit <kbd>Space</kbd>"],
  transcribing: ["Got it…",     "Turning your voice into words"],
  thinking:     ["Thinking…",   ""],
  searching:    ["Searching…",  ""],
  working:      ["Working…",    ""],
  waiting:      ["Waiting for you", ""],
  speaking:    ["Speaking",    '<kbd>Esc</kbd> to interrupt &nbsp;·&nbsp; <kbd>Space</kbd> to cut in'],
};

// Orb colours per state: [core, edge] as RGB
const PALETTE = {
  idle:         [[79, 209, 255], [124, 92, 255]],
  listening:    [[62, 240, 180], [20, 184, 166]],
  transcribing: [[62, 240, 180], [79, 209, 255]],
  thinking:     [[177, 140, 255], [255, 92, 168]],
  searching:    [[79, 209, 255], [177, 140, 255]],
  speaking:    [[255, 181, 71], [255, 107, 92]],
};

const DEFAULT_NAME = "Vesper";

let api = null;
let state = "idle";
let voiceMode = true; // off = plain text chat: no orb, no mic, nothing spoken
let webSearch = false; // on = the model may search the web before answering
let searchQuery = ""; // what it's searching for right now (shown under the orb)
let appMode = "chat"; // "chat" = voice assistant, "code" = Vesper Code
let codeFolder = null; // {path, name} of the folder Vesper Code works in
let micLevel = 0;
let settings = {};
const bubbles = new Map(); // turn id -> {el, text}
const replyEls = new Map(); // turn id -> its message element, kept so stats can land after it closes

// ------------------------------------------------------------------ chat
function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// ------------------------------------------------------------ markdown
// Replies can contain Markdown: headings, tables, lists, dividers, code. Rendered by hand
// (no library) and safe: every bit of model text goes through escapeHtml first.
const CARET = ""; // marks where the typing cursor goes while a reply streams in

function renderInline(s) {
  return escapeHtml(s)
    .replace(/`([^`\n]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
    .replace(/__([^_\n]+)__/g, "<strong>$1</strong>")
    .replace(/(^|[^*\w])\*([^*\n]+)\*/g, "$1<em>$2</em>")
    .replace(/(^|[^_\w])_([^_\n]+)_(?!\w)/g, "$1<em>$2</em>")
    .replace(/~~([^~\n]+)~~/g, "<del>$1</del>")
    .replace(/\[([^\]\n]+)\]\(([^)\s]*)\)/g, '<span class="md-link">$1</span>');
}

const TABLE_RULE = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;
const isTableRow = (l) => l.includes("|") && l.trim().length > 1;
const splitRow = (l) =>
  l.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());

function renderTable(header, rule, rows) {
  const aligns = splitRow(rule).map((c) =>
    c.startsWith(":") && c.endsWith(":") ? "center" : c.endsWith(":") ? "right" : "left");
  const cell = (tag, text, i) =>
    `<${tag}${aligns[i] && aligns[i] !== "left" ? ` style="text-align:${aligns[i]}"` : ""}>${renderInline(text ?? "")}</${tag}>`;
  const head = header.map((h, i) => cell("th", h, i)).join("");
  const body = rows.map((r) => `<tr>${header.map((_, i) => cell("td", r[i], i)).join("")}</tr>`).join("");
  return `<div class="md-table"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}

function renderBlocks(src) {
  const lines = src.split("\n");
  let html = "";
  let para = [];
  let list = null; // {tag: "ul" | "ol", start, items: []}
  const flushPara = () => {
    if (para.length) html += `<p>${para.map(renderInline).join("<br>")}</p>`;
    para = [];
  };
  const flushList = () => {
    if (list) {
      const start = list.tag === "ol" && list.start !== 1 ? ` start="${list.start}"` : "";
      html += `<${list.tag}${start}>${list.items.map((i) => `<li>${renderInline(i)}</li>`).join("")}</${list.tag}>`;
    }
    list = null;
  };
  const flush = () => (flushPara(), flushList());
  const addItem = (tag, text, start = 1) => {
    flushPara();
    if (list?.tag !== tag) {
      flushList();
      list = { tag, start, items: [] };
    }
    list.items.push(text);
  };

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    let m;
    if (!line.trim()) {
      flush();
    } else if ((m = line.match(/^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/))) {
      flush();
      html += `<div class="md-h md-h${Math.min(m[1].length, 4)}">${renderInline(m[2])}</div>`;
    } else if (/^\s{0,3}([-*_])(\s*\1){2,}\s*$/.test(line)) {
      flush();
      html += "<hr>";
    } else if (isTableRow(line) && TABLE_RULE.test(lines[i + 1] || "") && lines[i + 1].includes("-")) {
      flush();
      const rows = [];
      let j = i + 2;
      for (; j < lines.length && isTableRow(lines[j]); j++) rows.push(splitRow(lines[j]));
      html += renderTable(splitRow(line), lines[i + 1], rows);
      i = j - 1;
    } else if ((m = line.match(/^\s*[-*•+]\s+(.*)$/))) {
      addItem("ul", m[1]);
    } else if ((m = line.match(/^\s*(\d+)[.)]\s+(.*)$/))) {
      addItem("ol", m[2], +m[1]);
    } else if (list && /^\s{2,}\S/.test(line)) {
      list.items[list.items.length - 1] += " " + line.trim(); // wrapped list item
    } else {
      flushList();
      para.push(line);
    }
  }
  flush();
  return html;
}

function renderMarkdown(text, caret = false) {
  if (caret) text = text.trimEnd() + CARET;
  const parts = text.split(/```/);
  const html = parts
    .map((part, i) => {
      if (i % 2 === 1) return `<pre>${escapeHtml(part.replace(/^[\w+-]*\n/, ""))}</pre>`;
      return renderBlocks(part);
    })
    .join("");
  return html.replaceAll(CARET, '<span class="caret"></span>');
}

function activeChat() {
  return appMode === "code" ? $("codeChat") : $("chat");
}

function addMessage(kind, text, label, container = $("chat")) {
  container.querySelector(".empty, .code-empty")?.remove();
  const wrap = document.createElement("div");
  wrap.className = `msg ${kind}`;
  wrap.innerHTML = `<span class="who"></span><div class="bubble"></div>`;
  wrap.querySelector(".who").textContent = label;
  const bubble = wrap.querySelector(".bubble");
  const markdown = kind.startsWith("bot");
  bubble.classList.toggle("md", markdown);
  if (text != null) bubble.innerHTML = markdown ? renderMarkdown(text) : escapeHtml(text);
  container.appendChild(wrap);
  scrollChat();
  return wrap;
}

function scrollChat() {
  const chat = activeChat();
  chat.scrollTop = chat.scrollHeight;
}

function assistantLabel() {
  return settings.assistant_name || DEFAULT_NAME;
}

// --------------------------------------------------------------- events
const handlers = {
  state(s) {
    state = s;
    body.dataset.state = s;
    const [label, hint] = STATUS[s] || STATUS.idle;
    $("statusLabel").textContent = label;
    $("statusHint").innerHTML =
      s === "thinking" ? escapeHtml(prettyModel(settings.model))
      : s === "searching" ? escapeHtml(searchQuery ? `“${searchQuery}”` : "the web")
      : hint;
    if (s !== "listening") micLevel = 0;
  },
  level(l) {
    micLevel = l;
  },
  user(text) {
    addMessage("user", text, "You");
  },
  assistant_start(id) {
    const el = addMessage("bot streaming", null, assistantLabel());
    el.querySelector(".bubble").innerHTML = '<span class="typing"><i></i><i></i><i></i></span>';
    bubbles.set(id, { el, text: "" });
    replyEls.set(id, el);
  },
  // Token count and time for a finished reply. While the reply is still being spoken,
  // hold on to it until the bubble closes.
  stats(st) {
    const b = bubbles.get(st.id);
    if (b) b.stats = st;
    else showStats(replyEls.get(st.id), st);
    replyEls.delete(st.id);
  },
  token({ id, t }) {
    const b = bubbles.get(id);
    if (!b) return;
    b.text += t;
    renderBubble(b);
  },
  // A sentence just started playing: type it out over exactly as long as it's spoken.
  reveal({ id, t, dur }) {
    const b = bubbles.get(id);
    if (!b) return;
    finishReveal(b);
    const words = t.match(/\s*\S+\s*/g) || [t];
    if (dur <= 0 || words.length < 2) {
      b.text += t;
      return renderBubble(b);
    }
    b.pending = words;
    const step = (dur * 1000) / words.length;
    const tick = () => {
      b.text += b.pending.shift();
      renderBubble(b);
      b.timer = b.pending.length ? setTimeout(tick, step) : null;
    };
    tick();
  },
  // Reasoning from a thinking model: goes into the collapsed "See thinking" panel.
  thought({ id, t, secs }) {
    const b = bubbles.get(id);
    if (!b) return;
    const th = b.thoughts || (b.thoughts = makeThoughts(b));
    if (t) {
      th.body.append(t);
      if (th.el.open) th.body.scrollTop = th.body.scrollHeight;
    }
    if (secs != null) endThoughts(th, secs);
  },
  cant_think(model) {
    if (warnedNoThink.has(model)) return;
    warnedNoThink.add(model);
    toast(`${prettyModel(model)} can't think, so it answered straight away`, 4500);
  },
  // The model is searching the web: a small note above the reply shows what for.
  search({ id, query }) {
    searchQuery = query;
    const b = bubbles.get(id);
    if (!b) return;
    const el = document.createElement("div");
    el.className = "search-note live";
    el.innerHTML =
      '<svg viewBox="0 0 24 24" class="ico"><path d="M10.5 3a7.5 7.5 0 0 1 5.9 12.1l4.3 4.3a1 1 0 0 1-1.4 1.4l-4.3-4.3A7.5 7.5 0 1 1 10.5 3Zm0 2a5.5 5.5 0 1 0 0 11 5.5 5.5 0 0 0 0-11Z"/></svg>' +
      '<span class="sn-label">Searching the web</span><span class="sn-query"></span>';
    el.querySelector(".sn-query").textContent = query;
    b.el.insertBefore(el, b.el.querySelector(".bubble"));
    (b.searches || (b.searches = [])).push(el);
    scrollChat();
  },
  search_done({ id }) {
    searchQuery = "";
    const el = bubbles.get(id)?.searches?.at(-1);
    if (!el) return;
    el.classList.remove("live");
    el.querySelector(".sn-label").textContent = "Searched the web for";
  },
  cant_search(model) {
    if (warnedNoSearch.has(model)) return;
    warnedNoSearch.add(model);
    toast(`${prettyModel(model)} can't use tools, so it can't search the web`, 4500);
  },
  assistant_done(id) {
    const b = bubbles.get(id);
    if (!b) return;
    finishReveal(b);
    closeBubble(id, b);
  },
  // Interrupted: keep only what was actually said.
  assistant_cut(id) {
    if (codeTurns.has(id)) return endCodeTurn(id, "Stopped.");
    const b = bubbles.get(id);
    if (!b) return;
    clearTimeout(b.timer);
    b.pending = [];
    if (b.text.trim()) {
      b.text = b.text.trimEnd() + " —";
      renderBubble(b);
    }
    closeBubble(id, b);
  },
  error(msg) {
    addMessage("bot error", msg, "System", activeChat());
  },

  // ---- Vesper Code: one turn = text blocks, tool cards and permission prompts, in order
  code_start(id) {
    const wrap = addMessage("bot code-turn", null, assistantLabel(), $("codeChat"));
    const steps = wrap.querySelector(".bubble");
    steps.className = "steps";
    steps.innerHTML = '<span class="typing"><i></i><i></i><i></i></span>';
    codeTurns.set(id, { el: wrap, steps, block: null, th: null, card: null });
    codeTurnEls.set(id, wrap);
  },
  // This request changed files: offer to undo them.
  code_checkpoint({ id, files }) {
    const wrap = codeTurnEls.get(id);
    if (!wrap || !files.length) return;
    const bar = document.createElement("div");
    bar.className = "turn-undo";
    bar.innerHTML =
      '<span class="tu-text"></span><button class="tu-btn" title="">' +
      '<svg viewBox="0 0 24 24" class="ico"><path d="M9 14 4 9l5-5M4 9h10.5a5.5 5.5 0 0 1 0 11H11" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>Undo</button>';
    bar.querySelector(".tu-text").textContent =
      files.length === 1 ? `Changed ${files[0]}` : `Changed ${files.length} files`;
    bar.querySelector(".tu-text").title = files.join("\n");
    bar.querySelector(".tu-btn").title =
      "Put these files back the way they were before this request (later requests' changes are undone too). " +
      "Changes made by commands can't be undone.";
    bar.querySelector(".tu-btn").addEventListener("click", () => undoTurn(id));
    wrap.appendChild(bar);
    scrollChat();
  },
  code_text({ id, t }) {
    const turn = codeStep(id);
    if (!turn) return;
    if (turn.th) endThoughts(turn.th);
    if (!turn.block) {
      if (!t.trim()) return; // don't open a bubble for a stray newline between tool calls
      const el = document.createElement("div");
      el.className = "bubble md";
      turn.steps.appendChild(el);
      turn.block = { el, text: "" };
    }
    turn.block.text += t;
    turn.block.el.innerHTML = renderMarkdown(turn.block.text.replace(/^\s+/, ""), true);
    scrollChat();
  },
  code_thought({ id, t }) {
    const turn = codeStep(id);
    if (!turn) return;
    if (!turn.th || turn.th.done) {
      closeBlock(turn);
      turn.th = makeThoughts(null, turn.steps);
    }
    turn.th.body.append(t);
    if (turn.th.el.open) turn.th.body.scrollTop = turn.th.body.scrollHeight;
  },
  code_tool({ id, tid, name, label }) {
    const turn = codeStep(id);
    if (!turn) return;
    closeBlock(turn);
    if (turn.th) endThoughts(turn.th);
    const el = document.createElement("details");
    el.className = "tool-card live";
    el.dataset.tool = name;
    el.innerHTML =
      `<summary>${TOOL_ICONS[name] || TOOL_ICONS.default}<span class="tc-label"></span><span class="tc-sum"></span>` +
      '<svg viewBox="0 0 24 24" class="tc-chev"><path d="m9 6 6 6-6 6" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>' +
      '</summary><div class="tc-body"></div>';
    el.querySelector(".tc-label").textContent = label;
    el.querySelector(".tc-label").title = label;
    turn.steps.appendChild(el);
    turn.card = el;
    toolCards.set(tid, el);
    scrollChat();
  },
  code_tool_done({ tid, ok, summary, output, diff }) {
    const el = toolCards.get(tid);
    if (!el) return;
    toolCards.delete(tid);
    el.classList.remove("live", "asking");
    if (ok && /^(exit code [^0]|exit code -|timed out)/.test(summary)) ok = false; // the command itself failed
    el.classList.add(ok ? "ok" : "err");
    const denied = !ok && summary.startsWith("The user said no");
    el.querySelector(".tc-sum").textContent = denied ? "Denied" : summary;
    const body = el.querySelector(".tc-body");
    if (diff) {
      body.innerHTML = renderDiff(diff);
      el.open = true; // show what changed, like a code review
    } else if (output) {
      body.innerHTML = `<pre>${escapeHtml(output)}</pre>`;
      el.open = false;
    } else {
      body.innerHTML = ok ? "" : `<pre>${escapeHtml(summary)}</pre>`;
      el.open = false;
    }
    scrollChat();
  },
  code_ask({ id, ask, kind, label, detail }) {
    const turn = codeTurns.get(id);
    const el = turn?.card;
    if (!el) return api?.code_answer(ask, "deny");
    el.classList.add("asking");
    el.open = true;
    const body = el.querySelector(".tc-body");
    const always = kind === "edit" ? "Allow all edits" : "Always allow commands";
    body.innerHTML =
      `<div class="ask-box"><div class="ask-title">${kind === "edit" ? "Make this change?" : "Run this command?"}</div>` +
      (kind === "edit" ? renderDiff(detail) : `<div class="ask-cmd"></div>`) +
      `<div class="ask-btns"><button class="allow" data-a="allow">${kind === "edit" ? "Allow" : "Run"}</button>` +
      `<button data-a="always">${always}</button><button class="deny" data-a="deny">Deny</button></div></div>`;
    if (kind !== "edit") body.querySelector(".ask-cmd").textContent = label;
    body.querySelectorAll("button").forEach((btn) =>
      btn.addEventListener("click", () => {
        const answer = btn.dataset.a;
        api?.code_answer(ask, answer);
        el.classList.remove("asking");
        body.innerHTML = "";
        if (answer === "always" && kind === "edit") {
          $("codeAutoEdits").checked = true;
          save({ code_auto_edits: true });
          toast("Auto-accept edits on: I'll stop asking before changing files");
        }
        $("textInput").focus();
      }),
    );
    body.querySelector(".allow").focus();
    scrollChat();
  },
  code_note({ id, t }) {
    const turn = codeStep(id);
    if (!turn) return;
    closeBlock(turn);
    const el = document.createElement("div");
    el.className = "code-note";
    el.textContent = t;
    turn.steps.appendChild(el);
    scrollChat();
  },
  code_done(id) {
    endCodeTurn(id);
  },
  toast(msg) {
    toast(msg);
  },
  whisper({ ready, error }) {
    $("whisperStatus").textContent = ready ? "ready" : error ? "failed" : "loading…";
    if (error) toast("Speech model failed to load: " + error, 6000);
  },
};

function renderBubble(b) {
  b.el.querySelector(".bubble").innerHTML = renderMarkdown(b.text, b.el.classList.contains("streaming"));
  scrollChat();
}

function finishReveal(b) {
  clearTimeout(b.timer);
  b.timer = null;
  if (b.pending?.length) {
    b.text += b.pending.join("");
    b.pending = [];
    renderBubble(b);
  }
}

function closeBubble(id, b) {
  b.el.classList.remove("streaming");
  if (b.text.trim()) renderBubble(b); // drop the typing cursor
  if (b.thoughts) endThoughts(b.thoughts);
  if (b.stats) showStats(b.el, b.stats);
  if (!b.text.trim()) {
    if (!b.thoughts?.body.textContent.trim()) b.el.remove();
    else b.el.querySelector(".bubble").innerHTML = '<span class="no-answer">Stopped before it could answer.</span>';
  }
  bubbles.delete(id);
}

function fmtSecs(s) {
  if (s < 10) return `${s.toFixed(1)}s`;
  if (s < 60) return `${Math.round(s)}s`;
  return `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`;
}

// "312 tokens · responded in 4.2s" under a reply; hover for speed and context use.
function showStats(el, { tokens, prompt, secs, ctx }) {
  if (!el?.isConnected || el.querySelector(".reply-stats")) return;
  const line = document.createElement("div");
  line.className = "reply-stats";
  const n = tokens.toLocaleString();
  line.textContent = `${n} token${tokens === 1 ? "" : "s"} · responded in ${fmtSecs(secs)}`;
  const tips = [];
  if (secs > 0 && tokens) tips.push(`${(tokens / secs).toFixed(1)} tokens/s overall`);
  if (prompt) tips.push(`${prompt.toLocaleString()} prompt tokens read (context: ${ctx.toLocaleString()})`);
  line.title = tips.join("\n");
  el.appendChild(line);
  scrollChat();
}

// ---------------------------------------------------------- Vesper Code
const codeTurns = new Map(); // turn id -> {el, steps, block, th, card}
const toolCards = new Map(); // tool call id -> its card
const codeTurnEls = new Map(); // turn id -> its message element (kept after the turn ends, for Undo)

async function undoTurn(id) {
  if (!api) return;
  const r = await api.code_undo(id);
  if (r.error) return toast(r.error, 4000);
  for (const t of r.undone) {
    const bar = codeTurnEls.get(t)?.querySelector(".turn-undo");
    if (!bar) continue;
    bar.classList.add("undone");
    bar.querySelector(".tu-btn").remove();
    bar.querySelector(".tu-text").textContent = "Undone: files put back";
  }
  const n = r.files.length;
  toast(n === 1 ? `Undid the changes to ${r.files[0]}` : `Undid the changes to ${n} files`);
}

const svgIco = (d) =>
  `<svg viewBox="0 0 24 24" class="tc-ico"><path d="${d}" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
const TOOL_ICONS = {
  list_files: svgIco("M3.5 6.5a2 2 0 0 1 2-2h4l2 2.2h7a2 2 0 0 1 2 2v8.8a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2v-11Z"),
  read_file: svgIco("M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8l-5-5Zm0 0v5h5M9 13h6M9 17h4"),
  search_files: svgIco("M10.5 4a6.5 6.5 0 1 0 0 13 6.5 6.5 0 0 0 0-13Zm9.5 16-4.8-4.8"),
  write_file: svgIco("M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8l-5-5Zm0 0v5h5M12 11v6M9 14h6"),
  edit_file: svgIco("M4 20h4L19 9l-4-4L4 16v4ZM13.5 6.5l4 4"),
  run_command: svgIco("m5 7 5 5-5 5M12 18h7"),
  web_search: svgIco("M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18Zm-9 9h18M12 3c2.5 2.5 3.5 5.5 3.5 9s-1 6.5-3.5 9c-2.5-2.5-3.5-5.5-3.5-9s1-6.5 3.5-9Z"),
  default: svgIco("M12 4v16M4 12h16"),
};

function codeStep(id) {
  const turn = codeTurns.get(id);
  turn?.steps.querySelector(":scope > .typing")?.remove();
  return turn;
}

function closeBlock(turn) {
  if (!turn.block) return;
  const text = turn.block.text.trim();
  if (text) turn.block.el.innerHTML = renderMarkdown(text);
  else turn.block.el.remove();
  turn.block = null;
}

function endCodeTurn(id, note) {
  const turn = codeTurns.get(id);
  if (!turn) return;
  codeTurns.delete(id);
  turn.steps.querySelector(":scope > .typing")?.remove();
  closeBlock(turn);
  if (turn.th) endThoughts(turn.th);
  turn.steps.querySelectorAll(".tool-card.live").forEach((el) => {
    el.classList.remove("live", "asking");
    el.classList.add("err");
    el.querySelector(".tc-sum").textContent = "Stopped";
    el.querySelector(".tc-body").innerHTML = "";
    el.open = false;
  });
  if (note && turn.steps.children.length) {
    const n = document.createElement("div");
    n.className = "code-note";
    n.textContent = note;
    turn.steps.appendChild(n);
  }
  if (!turn.steps.children.length && !turn.el.querySelector(".turn-undo")) turn.el.remove();
}

function renderDiff(diff) {
  const rows = (diff || "").split("\n").map((line) => {
    const cls = line.startsWith("+++") || line.startsWith("---") ? "d-file"
      : line.startsWith("@@") ? "d-hunk"
      : line.startsWith("+") ? "d-add"
      : line.startsWith("-") ? "d-del" : "d-ctx";
    return `<div class="${cls}">${escapeHtml(line) || " "}</div>`;
  });
  return `<div class="diff">${rows.join("")}</div>`;
}

function codeEmptyState() {
  const chat = $("codeChat");
  chat.innerHTML = "";
  const el = document.createElement("div");
  el.className = "code-empty";
  if (!codeFolder) {
    el.innerHTML =
      '<div class="empty-title">Vesper Code</div>' +
      '<div class="empty-sub">Pick a folder and your local model can read, write and run the code in it. It asks before changing anything.</div>' +
      '<button class="primary-btn" id="pickFolderBig">Choose a folder</button>';
    el.querySelector("button").addEventListener("click", chooseFolder);
  } else {
    el.innerHTML =
      '<div class="empty-title"></div>' +
      '<div class="empty-sub">Ask it to build, fix or explain something in this folder.</div>' +
      '<div class="ce-tips"></div>';
    el.querySelector(".empty-title").textContent = `Working in ${codeFolder.name}`;
    for (const tip of ["What's in this folder? Give me a quick overview.",
                       "Find and fix any obvious bugs.",
                       "Make a small Python script that prints today's date."]) {
      const t = document.createElement("div");
      t.className = "ce-tip";
      t.textContent = tip;
      t.addEventListener("click", () => {
        $("textInput").value = tip;
        $("textInput").focus();
      });
      el.querySelector(".ce-tips").appendChild(t);
    }
  }
  chat.appendChild(el);
}

function applyFolder(info) {
  codeFolder = info || null;
  $("folderName").textContent = codeFolder ? codeFolder.name : "Choose a folder";
  $("folderPath").textContent = codeFolder ? codeFolder.path : "";
  $("folderPath").title = codeFolder ? codeFolder.path : "";
  updateComposer();
}

async function chooseFolder() {
  if (!api) return;
  const before = codeFolder?.path;
  const info = await api.choose_folder();
  if (!info) return;
  applyFolder(info);
  if (info.path !== before) {
    codeEmptyState();
    toast(`Vesper Code is now working in ${info.name}`);
  }
  $("textInput").focus();
}

function applyMode(mode) {
  appMode = mode === "code" ? "code" : "chat";
  body.classList.toggle("code-mode", appMode === "code");
  for (const b of document.querySelectorAll("#modeSwitch button")) {
    b.setAttribute("aria-selected", String(b.dataset.mode === appMode));
  }
  updateComposer();
  scrollChat();
}

function setMode(mode) {
  if (mode === appMode) return;
  applyMode(mode);
  save({ app_mode: appMode });
  $("textInput").focus();
}

function updateComposer() {
  const input = $("textInput");
  if (appMode === "code") {
    input.disabled = !codeFolder;
    input.placeholder = codeFolder ? `Ask ${assistantLabel()} Code to build, fix or explain something…` : "Choose a folder first";
  } else {
    input.disabled = false;
    input.placeholder = voiceMode ? "…or type a message" : "Message " + assistantLabel();
  }
}

document.querySelectorAll("#modeSwitch button").forEach((b) => b.addEventListener("click", () => setMode(b.dataset.mode)));
$("folderBtn").addEventListener("click", chooseFolder);
$("codeAutoEdits").addEventListener("change", (e) => {
  save({ code_auto_edits: e.target.checked });
  toast(e.target.checked ? "Auto-accept edits on: file changes apply without asking" : "Auto-accept edits off: I'll ask before each change");
});

// ------------------------------------------------------------- thinking
const warnedNoThink = new Set();
const warnedNoSearch = new Set();

function makeThoughts(b, parent = null) {
  const el = document.createElement("details");
  el.className = "thoughts live";
  el.innerHTML =
    '<summary><span class="th-dot"></span><span class="th-label">Thinking…</span>' +
    '<span class="th-time"></span>' +
    '<svg viewBox="0 0 24 24" class="th-chev"><path d="m9 6 6 6-6 6" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>' +
    "</summary><div class=\"th-body\"></div>";
  if (parent) parent.appendChild(el);
  else b.el.insertBefore(el, b.el.querySelector(".bubble"));
  const th = { el, body: el.querySelector(".th-body"), label: el.querySelector(".th-label"),
               time: el.querySelector(".th-time"), start: performance.now(), done: false };
  const tickTime = () => (th.time.textContent = `${Math.floor((performance.now() - th.start) / 1000)}s`);
  tickTime();
  th.timer = setInterval(tickTime, 250);
  el.addEventListener("toggle", () => {
    if (th.done) th.label.textContent = el.open ? "Hide thinking" : "See thinking";
    if (el.open) th.body.scrollTop = th.done ? 0 : th.body.scrollHeight;
  });
  scrollChat();
  return th;
}

function endThoughts(th, secs) {
  if (th.done) return;
  th.done = true;
  clearInterval(th.timer);
  th.el.classList.remove("live");
  th.label.textContent = th.el.open ? "Hide thinking" : "See thinking";
  const s = secs ?? (performance.now() - th.start) / 1000;
  th.time.textContent = s < 10 ? `${s.toFixed(1)}s` : `${Math.round(s)}s`;
}

window.jarvis = {
  onEvent(kind, payload) {
    try {
      handlers[kind]?.(payload);
    } catch (e) {
      console.error(e);
    }
  },
};

let toastTimer;
function toast(msg, ms = 2600) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("show"), ms);
}

function prettyModel(name = "") {
  return name.replace(/:latest$/, "").split("/").pop();
}

// -------------------------------------------------------------- controls
function talk() {
  if (voiceMode && appMode === "chat") api?.toggle_listen();
}

// ------------------------------------------------------------ voice mode
function applyVoiceMode(on) {
  voiceMode = on;
  body.classList.toggle("text-mode", !on);
  $("voiceToggle").setAttribute("aria-checked", String(on));
  $("voiceToggleLabel").textContent = on ? "Voice on" : "Voice off";
  updateComposer();
}

function setVoiceMode(on) {
  if (on === voiceMode) return;
  applyVoiceMode(on);
  save({ voice_mode: on });
  if (!on) $("textInput").focus();
  scrollChat();
  toast(on ? "Voice on: talk or type" : "Voice off: text chat only");
}

$("voiceToggle").addEventListener("click", () => setVoiceMode(!voiceMode));

// ------------------------------------------------------------ web search
function applyWebSearch(on) {
  webSearch = on;
  $("searchToggle").setAttribute("aria-checked", String(on));
  $("searchToggleLabel").textContent = on ? "Web on" : "Web off";
}

function setWebSearch(on) {
  if (on === webSearch) return;
  applyWebSearch(on);
  save({ web_search: on });
  toast(on ? "Web search on: I'll look things up when I need to" : "Web search off: fully offline");
}

$("searchToggle").addEventListener("click", () => setWebSearch(!webSearch));
function stopAll() {
  api?.stop();
}

$("micBtn").addEventListener("click", talk);
$("stopBtn").addEventListener("click", stopAll);
$("newChatBtn").addEventListener("click", newChat);

function newChat() {
  if (appMode === "code") {
    api?.code_new();
    codeTurns.clear();
    toolCards.clear();
    codeTurnEls.clear();
    codeEmptyState();
    return;
  }
  api?.new_chat();
  for (const b of bubbles.values()) clearInterval(b.thoughts?.timer);
  bubbles.clear();
  replyEls.clear();
  $("chat").innerHTML =
    '<div class="empty" id="emptyState"><div class="empty-title">Fresh start.</div><div class="empty-sub">Memory wiped. What\'s on your mind?</div></div>';
}

$("textForm").addEventListener("submit", (e) => {
  e.preventDefault();
  const input = $("textInput");
  const text = input.value.trim();
  if (!text) {
    if (state !== "idle") stopAll(); // the send button turns into stop while a reply is coming
    return;
  }
  input.value = "";
  if (appMode === "code") {
    if (!codeFolder) return chooseFolder();
    addMessage("user", text, "You", $("codeChat"));
    api?.code_send(text);
  } else {
    api?.send_text(text);
  }
});

document.addEventListener("keydown", (e) => {
  const tag = document.activeElement?.tagName;
  const typing = tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA";
  if (e.key === "Escape") {
    if (body.classList.contains("drawer-open")) return closeDrawer();
    if (typing) document.activeElement.blur();
    stopAll();
  } else if (e.code === "Space" && !typing && !e.repeat) {
    e.preventDefault();
    talk();
  } else if (e.ctrlKey && e.shiftKey && e.key.toLowerCase() === "v") {
    e.preventDefault();
    setVoiceMode(!voiceMode);
  } else if (e.ctrlKey && e.shiftKey && e.key.toLowerCase() === "w") {
    e.preventDefault();
    setWebSearch(!webSearch);
  } else if (e.ctrlKey && e.key.toLowerCase() === "n") {
    e.preventDefault();
    newChat();
  }
});

// --------------------------------------------------------------- settings
function openDrawer() {
  body.classList.add("drawer-open");
  refreshPhone();
  clearInterval(phone.timer);
  phone.timer = setInterval(refreshPhone, 2000);
}
function closeDrawer() {
  body.classList.remove("drawer-open");
  clearInterval(phone.timer);
}

// --------------------------------------------------------------- phone link (QR)
const phone = { host: null, timer: null, qr: "", startedAt: 0 };

async function refreshPhone() {
  if (!api) return;
  const p = await api.phone_link(phone.host);
  phone.host = p.host;

  const sel = $("hostSelect");
  const key = p.addresses.map((a) => a[0]).join();
  if (sel.dataset.key !== key) {
    const labels = Object.fromEntries(p.addresses);
    fillSelect(sel, p.addresses.map((a) => a[0]), p.host, (ip) => `${ip}  ·  ${labels[ip]}`);
    sel.dataset.key = key;
  }
  sel.value = p.host;

  const qr = p.qr || '<span class="qr-empty">No network connection</span>';
  if (qr !== phone.qr) $("qrBox").innerHTML = phone.qr = qr;

  const starting = !p.running && Date.now() - phone.startedAt < 90000;
  $("serverStatus").classList.toggle("on", p.running);
  $("serverStatus").classList.toggle("starting", starting);
  $("serverStatusText").textContent = p.running
    ? `Phone server running on port ${p.port}`
    : starting
      ? "Starting… loading speech + voice models"
      : "Phone server isn't running, so the app can't connect yet";
  $("startServer").hidden = p.running || starting;
}

$("hostSelect").addEventListener("change", (e) => {
  phone.host = e.target.value;
  refreshPhone();
});
$("startServer").addEventListener("click", async () => {
  const err = await api?.start_phone_server();
  if (err) return toast(err, 6000);
  phone.startedAt = Date.now();
  toast("Starting the phone server in the background");
  refreshPhone();
});
$("settingsBtn").addEventListener("click", openDrawer);
$("closeDrawer").addEventListener("click", closeDrawer);
$("scrim").addEventListener("click", closeDrawer);

async function save(changes) {
  if (!api) return;
  settings = await api.set_settings(changes);
}

function fillSelect(sel, values, current, labelFn = (v) => v) {
  sel.innerHTML = "";
  for (const v of values) {
    const o = document.createElement("option");
    o.value = v;
    o.textContent = labelFn(v);
    sel.appendChild(o);
  }
  if (current) sel.value = current;
}

function bindSettings() {
  $("modelSelect").addEventListener("change", (e) => {
    save({ model: e.target.value });
    toast(`Switched to ${prettyModel(e.target.value)}`);
  });
  $("voiceSelect").addEventListener("change", (e) => save({ voice: e.target.value }));
  $("styleSelect").addEventListener("change", (e) => {
    save({ style: e.target.value });
    toast(`Personality: ${e.target.selectedOptions[0].textContent}`);
  });
  $("rate").addEventListener("input", (e) => {
    $("rateVal").textContent = fmtSigned(e.target.value);
  });
  $("rate").addEventListener("change", (e) => save({ rate: +e.target.value }));
  $("volume").addEventListener("input", (e) => ($("volumeVal").textContent = e.target.value + "%"));
  $("volume").addEventListener("change", (e) => save({ volume: +e.target.value }));
  $("speakReplies").addEventListener("change", (e) => save({ speak_replies: e.target.checked }));
  $("thinkMode").addEventListener("change", (e) => {
    save({ think: e.target.checked });
    toast(e.target.checked ? "Thinking on: smarter answers, slower to start" : "Thinking off: instant answers");
  });
  $("handsFree").addEventListener("change", (e) => {
    save({ hands_free: e.target.checked });
    toast(e.target.checked ? "Hands-free on: I'll keep listening after I reply" : "Hands-free off");
  });
  $("whisperSelect").addEventListener("change", (e) => save({ whisper_model: e.target.value }));
  $("userName").addEventListener("change", (e) => save({ user_name: e.target.value.trim() }));
  $("assistantName").addEventListener("change", (e) => {
    const name = e.target.value.trim() || DEFAULT_NAME;
    save({ assistant_name: name }).then(() => applyVoiceMode(voiceMode));
    $("brandName").textContent = name.toUpperCase();
  });
  $("testVoice").addEventListener("click", () => api?.test_voice());
}

function fmtSigned(v) {
  v = +v;
  return v > 0 ? `+${v}` : `${v}`;
}

function applySettings(s, models, voices, styles) {
  settings = s;
  const styleLabels = Object.fromEntries(styles);
  fillSelect($("styleSelect"), styles.map((x) => x[0]), s.style, (id) => styleLabels[id]);
  fillSelect($("modelSelect"), models.length ? models : [""], s.model, (m) => prettyModel(m) || "No models found");
  const voiceLabels = Object.fromEntries(voices);
  fillSelect($("voiceSelect"), voices.map((v) => v[0]), s.voice, (id) => voiceLabels[id]);
  $("rate").value = s.rate;
  $("rateVal").textContent = fmtSigned(s.rate);
  $("volume").value = s.volume;
  $("volumeVal").textContent = s.volume + "%";
  $("speakReplies").checked = s.speak_replies;
  $("handsFree").checked = s.hands_free;
  $("thinkMode").checked = !!s.think;
  $("whisperSelect").value = s.whisper_model;
  $("userName").value = s.user_name || "";
  $("assistantName").value = s.assistant_name || DEFAULT_NAME;
  $("brandName").textContent = (s.assistant_name || DEFAULT_NAME).toUpperCase();
  applyVoiceMode(s.voice_mode !== false);
  applyWebSearch(!!s.web_search);
  $("codeAutoEdits").checked = !!s.code_auto_edits;
  applyMode(s.app_mode);
}

async function boot() {
  api = window.pywebview.api;
  bindSettings();
  const info = await api.init();
  applyFolder(info.code_folder);
  codeEmptyState();
  applySettings(info.settings, info.models, info.voices, info.styles);
  $("whisperStatus").textContent = info.whisper_ready ? "ready" : "loading…";
  if (info.error) addMessage("bot error", info.error, "System");
  handlers.state("idle");
}

if (window.pywebview?.api) boot();
else window.addEventListener("pywebviewready", boot);

// =================================================================== ORB
const canvas = $("orb");
const ctx = canvas.getContext("2d");
let W = 0, H = 0, DPR = 1;

function resize() {
  DPR = window.devicePixelRatio || 1;
  const r = canvas.getBoundingClientRect();
  W = r.width;
  H = r.height;
  canvas.width = Math.round(W * DPR);
  canvas.height = Math.round(H * DPR);
  ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
}
new ResizeObserver(resize).observe(canvas);
resize();

const col = { core: [...PALETTE.idle[0]], edge: [...PALETTE.idle[1]] };
let energy = 0;      // smoothed "loudness" that drives the blob
let speakTarget = 0; // fake syllable envelope while TTS talks
let nextSyllable = 0;
let spin = 0;

const lerp = (a, b, t) => a + (b - a) * t;
const rgba = (c, a) => `rgba(${c[0] | 0},${c[1] | 0},${c[2] | 0},${a})`;

function blobPath(cx, cy, R, amp, t, seed) {
  const N = 96;
  ctx.beginPath();
  for (let i = 0; i <= N; i++) {
    const a = (i / N) * Math.PI * 2;
    const n =
      Math.sin(a * 3 + t * 1.3 + seed) * 0.5 +
      Math.sin(a * 5 - t * 1.9 + seed * 2.1) * 0.3 +
      Math.sin(a * 2 + t * 0.7 + seed * 3.7) * 0.2;
    const r = R * (1 + amp * n);
    const x = cx + Math.cos(a) * r;
    const y = cy + Math.sin(a) * r;
    i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
  }
  ctx.closePath();
}

function frame(now) {
  if (!voiceMode) return requestAnimationFrame(frame); // orb hidden: don't spend GPU on it
  const t = now / 1000;
  const [tc, te] = PALETTE[state] || PALETTE.idle;
  for (let i = 0; i < 3; i++) {
    col.core[i] = lerp(col.core[i], tc[i], 0.06);
    col.edge[i] = lerp(col.edge[i], te[i], 0.06);
  }

  // what the blob should be doing
  const busy = state === "thinking" || state === "searching" || state === "transcribing";
  let target;
  if (state === "listening") target = 0.08 + micLevel * 0.9;
  else if (state === "speaking") {
    if (now > nextSyllable) {
      speakTarget = Math.random() < 0.18 ? 0.05 : 0.25 + Math.random() * 0.55;
      nextSyllable = now + 70 + Math.random() * 110;
    }
    target = speakTarget;
  } else if (busy) target = 0.18 + Math.sin(t * 4) * 0.05;
  else target = 0.05 + Math.sin(t * 1.2) * 0.025; // idle breathing
  energy = lerp(energy, target, state === "speaking" ? 0.25 : 0.15);

  const speed = state === "thinking" || state === "searching" ? 3.2 : state === "idle" ? 0.7 : 1.6;
  spin += 0.016 * speed;

  ctx.clearRect(0, 0, W, H);
  const cx = W / 2, cy = H / 2;
  const R = Math.min(W, H) * 0.23 * (1 + energy * 0.18);

  // outer glow (kept inside the canvas so no hard edge shows)
  const glow = ctx.createRadialGradient(cx, cy, R * 0.4, cx, cy, Math.min(W, H) * 0.5);
  glow.addColorStop(0, rgba(col.core, 0.32 + energy * 0.25));
  glow.addColorStop(0.5, rgba(col.edge, 0.09));
  glow.addColorStop(1, rgba(col.edge, 0));
  ctx.fillStyle = glow;
  ctx.fillRect(0, 0, W, H);

  // HUD ring: tick marks that react to energy
  const ticks = 90;
  ctx.save();
  ctx.translate(cx, cy);
  ctx.rotate(spin * 0.15);
  for (let i = 0; i < ticks; i++) {
    const a = (i / ticks) * Math.PI * 2;
    const wob = 0.5 + 0.5 * Math.sin(a * 6 + t * 3);
    const len = 4 + energy * 26 * wob;
    const r0 = R * 1.55;
    ctx.strokeStyle = rgba(i % 5 ? col.edge : col.core, 0.22 + energy * 0.5 * wob);
    ctx.lineWidth = i % 5 ? 1 : 1.6;
    ctx.beginPath();
    ctx.moveTo(Math.cos(a) * r0, Math.sin(a) * r0);
    ctx.lineTo(Math.cos(a) * (r0 + len), Math.sin(a) * (r0 + len));
    ctx.stroke();
  }
  ctx.restore();

  // thinking: orbiting arcs
  const arcAlpha = busy ? 0.85 : 0.18;
  ctx.lineCap = "round";
  for (let k = 0; k < 3; k++) {
    const rr = R * (1.28 + k * 0.08);
    const start = spin * (k % 2 ? -1.2 : 1) * (1 + k * 0.3) + k * 2;
    ctx.strokeStyle = rgba(k === 1 ? col.edge : col.core, arcAlpha * (1 - k * 0.25));
    ctx.lineWidth = 2.2 - k * 0.5;
    ctx.beginPath();
    ctx.arc(cx, cy, rr, start, start + 0.9 + k * 0.4);
    ctx.stroke();
  }

  // layered liquid blobs
  ctx.globalCompositeOperation = "lighter";
  for (let i = 0; i < 4; i++) {
    const amp = 0.04 + energy * (0.22 + i * 0.05);
    blobPath(cx, cy, R * (1 - i * 0.06), amp, t * (1 + i * 0.35) * (0.6 + energy), i * 1.7);
    const g = ctx.createRadialGradient(cx - R * 0.3, cy - R * 0.35, R * 0.05, cx, cy, R * 1.25);
    g.addColorStop(0, rgba(col.core, 0.38));
    g.addColorStop(0.6, rgba(col.edge, 0.2));
    g.addColorStop(1, rgba(col.edge, 0.02));
    ctx.fillStyle = g;
    ctx.fill();
  }
  ctx.globalCompositeOperation = "source-over";

  // glossy core
  const core = ctx.createRadialGradient(cx - R * 0.25, cy - R * 0.3, 0, cx, cy, R * 0.85);
  core.addColorStop(0, "rgba(255,255,255,0.55)");
  core.addColorStop(0.25, rgba(col.core, 0.35));
  core.addColorStop(1, rgba(col.edge, 0));
  ctx.fillStyle = core;
  ctx.beginPath();
  ctx.arc(cx, cy, R * 0.85, 0, Math.PI * 2);
  ctx.fill();

  // mic ring on the button follows the voice
  $("micBtn").style.setProperty("--lvl-scale", (1 + micLevel * 0.35).toFixed(3));

  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);

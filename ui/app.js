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
let appMode = "chat"; // "chat" = voice assistant, "code" = Vesper Code, "sandbox" = Sandbox
let codeFolder = null; // {path, name} of the folder Vesper Code works in
let micLevel = 0;
let settings = {};
const ctxUsed = { chat: 0, code: 0, sandbox: 0 }; // tokens in the context after the last request, per mode
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
  return appMode === "code" ? $("codeChat") : appMode === "sandbox" ? $("sbChat") : $("chat");
}

function addMessage(kind, text, label, container = $("chat")) {
  container.querySelector(".empty, .code-empty, .sb-empty")?.remove();
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
  // How full the context window is, after each request the model finishes.
  ctx({ used, mode }) {
    ctxUsed[mode in ctxUsed ? mode : "chat"] = used;
    renderCtxMeter();
  },
  stats(st) {
    const b = bubbles.get(st.id);
    if (b) b.stats = st;
    else showStats(replyEls.get(st.id) || codeTurnEls.get(st.id), st);
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
    if (sb.turns.has(id)) return; // its sb_done follows, with "Stopped by you"
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

// "312 tokens · responded in 4.2s" under a reply; hover for speed and prompt size.
function showStats(el, { tokens, prompt, secs }) {
  if (!el?.isConnected || el.querySelector(".reply-stats")) return;
  const line = document.createElement("div");
  line.className = "reply-stats";
  const n = tokens.toLocaleString();
  line.textContent = `${n} token${tokens === 1 ? "" : "s"} · responded in ${fmtSecs(secs)}`;
  const tips = [];
  if (secs > 0 && tokens) tips.push(`${(tokens / secs).toFixed(1)} tokens/s overall`);
  // Summed over every request the reply made (a web search or thinking cut-off adds one).
  if (prompt) tips.push(`${prompt.toLocaleString()} prompt tokens read`);
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
  if (info?.path !== codeFolder?.path) {
    ctxUsed.code = 0; // a new folder starts a new Vesper Code conversation
    renderCtxMeter();
  }
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
  appMode = ["code", "sandbox"].includes(mode) ? mode : "chat";
  body.classList.toggle("code-mode", appMode === "code");
  body.classList.toggle("sandbox-mode", appMode === "sandbox");
  if (appMode === "sandbox") loadSbModel();
  for (const b of document.querySelectorAll("#modeSwitch button")) {
    b.setAttribute("aria-selected", String(b.dataset.mode === appMode));
  }
  updateComposer();
  scrollChat();
  renderCtxMeter();
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
  } else if (appMode === "sandbox") {
    input.disabled = false;
    input.placeholder = "Message the model with the settings on the left";
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
  ctxUsed[appMode] = 0;
  renderCtxMeter();
  if (appMode === "sandbox") {
    api?.sandbox_new();
    sbDone.clear();
    sbEmptyState();
    return;
  }
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
  } else if (appMode === "sandbox") {
    sbSend(text);
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
$("ctxMeter").addEventListener("click", () => {
  if (appMode === "sandbox") {
    const field = document.querySelector('.sb-field[data-key="num_ctx"]');
    field.closest("details").open = true;
    return field.scrollIntoView({ block: "center", behavior: "smooth" });
  }
  openDrawer();
  setTimeout(() => $("ctxField").scrollIntoView({ block: "center", behavior: "smooth" }), 250);
});
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
    save({ model: e.target.value }).then(() => appMode === "sandbox" && loadSbModel());
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
  $("effortSlider").addEventListener("input", (e) => showEffort(+e.target.value, $("thinkMode").checked));
  $("effortSlider").addEventListener("change", (e) => {
    const effort = EFFORTS[+e.target.value];
    save({ think_effort: effort.id });
    toast(`Thinking effort: ${effort.label}`);
  });
  $("ctxSlider").addEventListener("input", (e) => showCtx(CTX_STOPS[+e.target.value]));
  $("ctxSlider").addEventListener("change", (e) => {
    const tokens = CTX_STOPS[+e.target.value];
    save({ num_ctx: tokens }).then(renderCtxMeter);
    toast(`Context: ${tokens / 1024}k tokens. Reloading the model…`);
  });
  $("speakReplies").addEventListener("change", (e) => save({ speak_replies: e.target.checked }));
  $("thinkMode").addEventListener("change", (e) => {
    save({ think: e.target.checked });
    showEffort(+$("effortSlider").value, e.target.checked);
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

// Context window stops (tokens). Up to 12k fits a laptop RTX 3050's 4 GB of VRAM with the 4B
// model at full speed (measured: 43 tok/s at 4k, 8k and 12k); 16k no longer fits on the GPU.
const CTX_STOPS = [2048, 4096, 6144, 8192, 12288, 16384, 24576, 32768, 65536];
const CTX_RECOMMENDED = 12288;

function ctxIndex(tokens) {
  const i = CTX_STOPS.findIndex((t) => t >= tokens);
  return i < 0 ? CTX_STOPS.length - 1 : i;
}

function showCtx(tokens) {
  const over = tokens > CTX_RECOMMENDED;
  $("ctxField").classList.toggle("over", over);
  $("ctxVal").textContent = `${tokens / 1024}k tokens`;
  $("ctxNote").textContent = over
    ? `⚠ Over ${CTX_RECOMMENDED / 1024}k won't fit in your RTX 3050's 4 GB of VRAM. Part of the model moves to the CPU, so replies get 2–3× slower, and it may fail to load.`
    : "How much of the conversation the model can see at once. Up to 12k runs at full speed on your GPU.";
}

// Thinking effort = how long the model may think before it has to answer (src/llm.py THINK_EFFORT).
const EFFORTS = [
  { id: "low", label: "Low", note: "A quick look before answering. Up to ~600 tokens or 20 s." },
  { id: "medium", label: "Medium", note: "Balanced. Up to ~2,000 tokens or 45 s." },
  { id: "high", label: "High", note: "Thinks it through. Up to ~5,000 tokens or 2 min." },
  { id: "max", label: "Max", note: "As long as it needs, up to 5 min. Capped at 60% of the context window, so raise that too." },
];

function effortIndex(id) {
  const i = EFFORTS.findIndex((e) => e.id === id);
  return i < 0 ? 1 : i;
}

function showEffort(i, thinkOn) {
  $("effortVal").textContent = EFFORTS[i].label;
  $("effortNote").textContent = thinkOn ? EFFORTS[i].note : "Turn on \"Think before answering\" to use this.";
  $("effortField").classList.toggle("off", !thinkOn);
}

// Top bar meter: how much of the context window the current conversation fills.
function fmtTokens(n) {
  return n < 1000 ? `${n}` : `${+(n / 1024).toFixed(n < 10240 ? 1 : 0)}k`;
}

function renderCtxMeter() {
  const max = (appMode === "sandbox" ? sb.cfg.num_ctx : settings.num_ctx) || 8192;
  const used = Math.min(ctxUsed[appMode] || 0, max);
  const pct = Math.round((used / max) * 100);
  const level = pct >= 90 ? "full" : pct >= 70 ? "high" : "";
  $("ctxMeter").dataset.level = level;
  $("ctxMeterFill").style.width = `${Math.max(pct, used ? 2 : 0)}%`;
  $("ctxMeterText").textContent = `${fmtTokens(used)} / ${fmtTokens(max)}`;
  $("ctxMeter").title =
    `Context window: ${used.toLocaleString()} of ${max.toLocaleString()} tokens used (${pct}%).
` +
    (level === "full"
      ? "Nearly full: the oldest messages are being dropped. Start a new chat, or raise the context window in Settings."
      : "As it fills up, the oldest messages get dropped. Click to change the size.");
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
  $("ctxSlider").value = ctxIndex(s.num_ctx || 8192);
  showCtx(CTX_STOPS[ctxIndex(s.num_ctx || 8192)]);
  $("speakReplies").checked = s.speak_replies;
  $("handsFree").checked = s.hands_free;
  $("thinkMode").checked = !!s.think;
  $("effortSlider").value = effortIndex(s.think_effort);
  showEffort(effortIndex(s.think_effort), !!s.think);
  $("whisperSelect").value = s.whisper_model;
  $("userName").value = s.user_name || "";
  $("assistantName").value = s.assistant_name || DEFAULT_NAME;
  $("brandName").textContent = (s.assistant_name || DEFAULT_NAME).toUpperCase();
  applyVoiceMode(s.voice_mode !== false);
  applyWebSearch(!!s.web_search);
  $("codeAutoEdits").checked = !!s.code_auto_edits;
  applyMode(s.app_mode);
  renderCtxMeter();
}

// ================================================================ Sandbox
// A plain text chat with every knob exposed. Settings on the left go out with the next message;
// each reply shows what was sent, how long it took, why it stopped and how sure the model was.
const sb = { cfg: {}, defaults: {}, presets: [], info: null, turns: new Map(), saveTimer: null };

const r2 = (v) => Math.round(v * 100) / 100;
const SB_FIELDS = {
  think_budget: {
    label: "Thinking budget", stops: [64, 128, 256, 512, 1024, 2048, 4096, 8192, 16384, 0],
    fmt: (v) => (v ? `${fmtTokens(v)} tokens` : "No limit"),
    help: "The most tokens it may think for. A model can't limit its own thinking, so this app counts the tokens, cuts it off, and asks for an answer from its notes so far.",
  },
  temperature: {
    label: "Temperature", min: 0, max: 2, step: 0.05, fmt: (v) => v.toFixed(2),
    help: "At every step the model gives each possible next token a probability, then one is picked at random. Temperature reshapes those odds first.",
    explain: (v) => v === 0 ? "0 = always the single likeliest token: same question, same answer."
      : v < 0.5 ? "Low: sticks to safe, likely tokens. Focused, a bit repetitive."
      : v <= 1 ? "Balanced: mostly likely tokens, the odd surprise."
      : v <= 1.4 ? "High: takes chances. More creative, more mistakes."
      : "Very high: unlikely tokens win all the time. Expect gibberish.",
  },
  top_p: {
    label: "Top P", min: 0.05, max: 1, step: 0.01, fmt: (v) => v.toFixed(2),
    help: "Keeps the likeliest tokens until their probabilities add up to this, and throws away the long tail of unlikely ones.",
    explain: (v) => (v >= 1 ? "1 = off: every token stays in the running." : `Keeps the top ${Math.round(v * 100)}% of the probability, drops the rest.`),
  },
  top_k: {
    label: "Top K", min: 1, max: 100, step: 1, fmt: (v) => `${v}`,
    help: "Only the K likeliest next tokens are allowed.",
    explain: (v) => (v === 1 ? "1 = only the favourite, so temperature stops mattering." : `It picks among the ${v} likeliest tokens.`),
  },
  min_p: {
    label: "Min P", min: 0, max: 0.5, step: 0.01, fmt: (v) => v.toFixed(2),
    help: "Drops any token that's less likely than this fraction of the favourite.",
    explain: (v) => (v === 0 ? "0 = off." : `A token must be at least ${Math.round(v * 100)}% as likely as the favourite.`),
  },
  repeat_penalty: {
    label: "Repeat penalty", min: 0.8, max: 2, step: 0.05, fmt: (v) => v.toFixed(2),
    help: "Makes tokens it used in the last 64 less likely, so it doesn't go round in circles.",
    explain: (v) => v === 1 ? "1 = off."
      : v < 1 ? "Below 1 actually rewards repeating itself."
      : v > 1.5 ? "Strong: it starts dodging ordinary words like “the”."
      : "Gently discourages repeating.",
  },
  presence_penalty: {
    label: "Presence penalty", min: 0, max: 2, step: 0.1, fmt: (v) => v.toFixed(1),
    help: "A flat penalty on every token that has appeared at all, pushing it toward new words and topics.",
    explain: (v) => (v === 0 ? "0 = off." : "Higher = more new words, less sticking to the point."),
  },
  num_predict: {
    label: "Max reply length", stops: [8, 16, 32, 64, 128, 256, 512, 1024, 2048, 4096, -1],
    fmt: (v) => (v < 0 ? "No limit" : `${v.toLocaleString()} tokens`),
    help: "A hard cap on the reply. A token is a word or a piece of one (an English word is about 1.3 tokens). It stops mid-sentence when it hits this.",
  },
  num_ctx: {
    label: "Context window", stops: CTX_STOPS, fmt: (v) => `${v / 1024}k tokens`,
    help: "Everything the model can see at once: system prompt, chat history and its own reply. When it's full the oldest messages fall out. Changing it reloads the model.",
    explain: (v) => (v > CTX_RECOMMENDED ? "⚠ Over 12k won't fit in your GPU's 4 GB, so replies get 2–3× slower." : ""),
  },
  memory: {
    label: "Memory", stops: [0, 1, 2, 3, 4, 5, 6, 8, 10, 15, -1],
    fmt: (v) => (v < 0 ? "Everything" : v === 0 ? "None" : `Last ${v} exchange${v === 1 ? "" : "s"}`),
    help: "The model remembers nothing between messages. Every time, the app re-sends the conversation so far; this sets how much of it.",
    explain: (v) => (v === 0 ? "Each message is a fresh start: tell it your name, then ask for it." : ""),
  },
};

// Click an experiment and it sets things up for you.
const SB_EXPERIMENTS = [
  { title: "Same answer every time", how: "Temperature 0, then hit Regenerate a few times.",
    set: { temperature: 0 }, prompt: "Write a one-sentence story about a cat." },
  { title: "See it hesitate", how: "Token probabilities on. Hover the number it picks.",
    set: { logprobs: true, temperature: 0.8 }, prompt: "Pick a random number between 1 and 100. Reply with just the number." },
  { title: "It has no memory", how: "Memory: None. It can't know what you said a second ago.",
    set: { memory: 0 }, prompt: "My name is Sam. What's my name?" },
  { title: "Cut off mid-sentence", how: "Max reply length: 16 tokens.",
    set: { num_predict: 16 }, prompt: "Explain how rainbows form." },
  { title: "Turn the heat up", how: "Temperature 1.8 and Top P 1: unlikely tokens everywhere.",
    set: { temperature: 1.8, top_p: 1, top_k: 100 }, prompt: "Describe your perfect breakfast." },
  { title: "Rewrite its personality", how: "The “Always wrong” system prompt.",
    preset: "wrong", prompt: "What's the capital of France?" },
];

function sbField(key) {
  const f = SB_FIELDS[key];
  const range = f.stops ? `min="0" max="${f.stops.length - 1}" step="1"` : `min="${f.min}" max="${f.max}" step="${f.step}"`;
  return `<div class="sb-field" data-key="${key}">
    <label class="sb-label" for="sb_${key}"><span>${f.label}</span><span class="sb-val"></span></label>
    <input type="range" id="sb_${key}" ${range} />
    <div class="sb-help">${f.help}</div>
    ${f.explain ? '<div class="sb-explain"></div>' : ""}
  </div>`;
}

function sbToggle(key, label, help) {
  return `<label class="toggle sb-toggle"><input type="checkbox" id="sb_${key}" />
    <span class="track"><span class="thumb"></span></span>
    <span class="toggle-text">${label}<small>${help}</small></span></label>`;
}

function sbGroup(title, body, open = true) {
  return `<details class="sb-group"${open ? " open" : ""}><summary>${title}
    <svg viewBox="0 0 24 24" class="sb-chev"><path d="m9 6 6 6-6 6" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>
    </summary><div class="sb-body">${body}</div></details>`;
}

function buildSandboxPanel() {
  const side = $("sbSide");
  side.innerHTML =
    `<div class="sb-head"><div><div class="sb-title">Sandbox</div>
       <div class="sb-sub">Change anything here, then send a message (or hit Regenerate) to see what it does.</div></div>
       <button class="sb-mini" id="sbReset" title="Put every sandbox setting back to the start">Reset</button></div>` +
    sbGroup("Model", `<div class="sb-model" id="sbModel"><span class="sb-help">Loading…</span></div>`) +
    sbGroup("System prompt",
      `<select id="sbPreset" class="sb-select"></select>
       <textarea id="sbSystem" class="sb-text" rows="5" spellcheck="false" placeholder="No system prompt: the model only sees your messages."></textarea>
       <div class="sb-help">Instructions sent before the conversation. To the model it's just more text, placed first and labelled “system”, which it was trained to follow closely. <span id="sbSysCount"></span></div>`) +
    sbGroup("Thinking",
      sbToggle("think", "Think before answering", "The model writes out private reasoning first. Needs a thinking model, like Qwen 3.5.") +
      sbField("think_budget")) +
    sbGroup("Sampling: how the next token is picked",
      `<div class="sb-help sb-lead">A model never writes a sentence at once. It predicts one token at a time; these decide how adventurous each pick is.</div>` +
      sbField("temperature") + sbField("top_p") + sbField("top_k") + sbField("min_p") +
      `<button class="sb-mini wide" id="sbModelDefaults">Use this model's recommended values</button>`) +
    sbGroup("Repetition", sbField("repeat_penalty") + sbField("presence_penalty"), false) +
    sbGroup("Length & memory",
      sbField("num_predict") + sbField("num_ctx") + sbField("memory") +
      `<div class="sb-field"><label class="sb-label" for="sb_stop"><span>Stop sequences</span></label>
         <input id="sb_stop" class="sb-input" placeholder="e.g.  ., \\n" spellcheck="false" />
         <div class="sb-help">It stops the moment it writes one of these. Separate them with commas.</div></div>`, false) +
    sbGroup("Randomness seed",
      sbToggle("seed_on", "Fixed seed", "The “random” picks come from a number generator. Same seed + same settings + same chat = the exact same reply.") +
      `<div class="sb-seed"><input id="sb_seed" class="sb-input" type="number" min="0" step="1" />
         <button class="sb-mini" id="sbDice" title="New random seed">🎲</button></div>`, false) +
    sbGroup("Look inside",
      sbToggle("logprobs", "Token probabilities", "Shows the reply token by token, shaded by how unsure the model was. Hover one to see what else it nearly said.") +
      sbToggle("show_request", "Show the raw request", "Opens the exact JSON sent to Ollama under every reply."));

  for (const key of Object.keys(SB_FIELDS)) {
    const f = SB_FIELDS[key];
    const input = $(`sb_${key}`);
    const read = () => (f.stops ? f.stops[+input.value] : r2(+input.value));
    input.addEventListener("input", () => {
      sb.cfg[key] = read();
      showSbField(key);
      if (key === "num_ctx") renderCtxMeter();
      saveSb();
    });
    if (key === "num_ctx") input.addEventListener("change", () => toast(`Context: ${sb.cfg.num_ctx / 1024}k. The model reloads on your next message`));
  }
  for (const key of ["think", "seed_on", "logprobs", "show_request"]) {
    $(`sb_${key}`).addEventListener("change", (e) => {
      sb.cfg[key] = e.target.checked;
      showSbToggles();
      saveSb();
    });
  }
  $("sb_stop").addEventListener("input", (e) => ((sb.cfg.stop = e.target.value), saveSb()));
  $("sb_seed").addEventListener("input", (e) => ((sb.cfg.seed = Math.max(0, Math.floor(+e.target.value || 0))), saveSb()));
  $("sbDice").addEventListener("click", () => {
    sb.cfg.seed = Math.floor(Math.random() * 100000);
    sb.cfg.seed_on = true;
    applySbConfig();
    saveSb();
  });
  $("sbSystem").addEventListener("input", (e) => {
    sb.cfg.system = e.target.value;
    showSbSystem();
    saveSb();
  });
  $("sbPreset").addEventListener("change", (e) => {
    const p = sb.presets.find((x) => x.id === e.target.value);
    if (!p) return;
    sb.cfg.system = p.text;
    $("sbSystem").value = p.text;
    showSbSystem();
    saveSb();
  });
  $("sbReset").addEventListener("click", () => {
    sb.cfg = { ...sb.defaults };
    applySbConfig();
    saveSb();
    toast("Sandbox settings reset");
  });
  $("sbModelDefaults").addEventListener("click", () => {
    const d = sb.info?.defaults;
    if (!d) return toast("Couldn't read this model's settings");
    Object.assign(sb.cfg, d);
    applySbConfig();
    saveSb();
    const own = sb.info.own_defaults || [];
    toast(own.length ? `Using what ${prettyModel(sb.info.model)} ships with: ${own.map((k) => `${k} ${r2(d[k])}`).join(", ")}`
                     : `${prettyModel(sb.info.model)} doesn't set its own, so these are Ollama's defaults`, 5000);
  });
}

function showSbField(key) {
  const f = SB_FIELDS[key];
  const v = sb.cfg[key];
  const el = document.querySelector(`.sb-field[data-key="${key}"]`);
  el.querySelector(".sb-val").textContent = f.fmt(v);
  const ex = el.querySelector(".sb-explain");
  if (ex) {
    ex.textContent = f.explain(v);
    ex.hidden = !ex.textContent;
    ex.classList.toggle("warn", ex.textContent.startsWith("⚠"));
  }
}

function showSbToggles() {
  document.querySelector('.sb-field[data-key="think_budget"]').classList.toggle("off", !sb.cfg.think);
  $("sb_seed").disabled = !sb.cfg.seed_on;
  $("sbChat").classList.toggle("show-request", !!sb.cfg.show_request);
}

function showSbSystem() {
  const text = sb.cfg.system || "";
  const match = sb.presets.find((p) => p.text === text);
  $("sbPreset").value = match ? match.id : "custom";
  // A token is roughly four characters of English.
  $("sbSysCount").textContent = text ? `About ${Math.max(1, Math.round(text.length / 4)).toLocaleString()} tokens.` : "";
}

function applySbConfig() {
  const c = sb.cfg;
  for (const key of Object.keys(SB_FIELDS)) {
    const f = SB_FIELDS[key];
    if (f.stops) {
      const i = f.stops.indexOf(c[key]);
      $(`sb_${key}`).value = i >= 0 ? i : f.stops.findIndex((s) => s >= c[key]) >= 0 ? f.stops.findIndex((s) => s >= c[key]) : f.stops.length - 1;
    } else {
      $(`sb_${key}`).value = c[key];
    }
    showSbField(key);
  }
  for (const key of ["think", "seed_on", "logprobs", "show_request"]) $(`sb_${key}`).checked = !!c[key];
  $("sb_stop").value = c.stop || "";
  $("sb_seed").value = c.seed ?? 42;
  $("sbSystem").value = c.system || "";
  showSbSystem();
  showSbToggles();
  renderCtxMeter();
}

function saveSb() {
  clearTimeout(sb.saveTimer);
  sb.saveTimer = setTimeout(() => save({ sandbox: sb.cfg }), 400);
}

function fmtBytes(n) {
  return n >= 1e9 ? `${(n / 1e9).toFixed(1)} GB` : `${Math.round(n / 1e6)} MB`;
}

async function loadSbModel() {
  if (!api) return;
  const info = await api.sandbox_model_info(settings.model);
  sb.info = info.error ? null : info;
  const box = $("sbModel");
  if (info.error) {
    box.innerHTML = `<span class="sb-help">Couldn't ask Ollama about this model.</span>`;
    return;
  }
  const bits = /^Q(\d)/i.exec(info.quant || "")?.[1] || (/F16|BF16/i.test(info.quant) ? "16" : /F32/i.test(info.quant) ? "32" : "");
  const rows = [
    ["Model", prettyModel(info.model), "Pick a different one in the top bar."],
    ["Size", info.params ? `${info.params} parameters` : "?",
     "Parameters are the numbers the model learned in training. More usually means smarter, but bigger and slower."],
    ["Precision", info.quant || "?", bits
      ? `Each parameter is squeezed into about ${bits} bits (instead of 16) so the model fits in your GPU, losing a little accuracy.`
      : "How precisely each parameter is stored."],
    ["On disk", info.size ? fmtBytes(info.size) : "?", "This all has to fit in your GPU's memory to run fast."],
    ["Trained context", info.trained_ctx ? `${fmtTokens(info.trained_ctx)} tokens` : "?",
     "The longest text it was trained to handle. The context window below is how much you actually let it use, because memory is limited."],
  ];
  const caps = (info.capabilities || []).filter((c) => c !== "completion");
  box.innerHTML =
    rows.map(([k, v, tip]) => `<div class="sb-row" title="${escapeHtml(tip)}"><span>${k}</span><b>${escapeHtml(v)}</b></div>`).join("") +
    (caps.length ? `<div class="sb-caps">${caps.map((c) => `<span class="sb-cap">${escapeHtml(c)}</span>`).join("")}</div>` : "");
}

function sbEmptyState() {
  const chat = $("sbChat");
  chat.innerHTML = "";
  sb.turns.clear();
  const el = document.createElement("div");
  el.className = "sb-empty";
  el.innerHTML =
    '<div class="empty-title">Same model, nothing hidden.</div>' +
    '<div class="empty-sub">Change a setting on the left and see what it does. Every reply shows its tokens, speed, why it stopped and exactly what was sent. Try an experiment:</div>' +
    '<div class="sb-exps"></div>';
  for (const x of SB_EXPERIMENTS) {
    const card = document.createElement("button");
    card.className = "sb-exp";
    card.innerHTML = `<b></b><span></span>`;
    card.querySelector("b").textContent = x.title;
    card.querySelector("span").textContent = x.how;
    card.addEventListener("click", () => runExperiment(x));
    el.querySelector(".sb-exps").appendChild(card);
  }
  chat.appendChild(el);
}

function runExperiment(x) {
  if (x.set) Object.assign(sb.cfg, x.set);
  if (x.preset) sb.cfg.system = sb.presets.find((p) => p.id === x.preset)?.text || "";
  applySbConfig();
  saveSb();
  $("textInput").value = x.prompt;
  $("textInput").focus();
  toast("Settings changed on the left. Press Enter to send");
}

function sbSend(text) {
  addMessage("user", text, "You", $("sbChat"));
  api?.sandbox_send(text, sb.cfg);
}

// The settings a reply was made with, in one line, to compare replies with each other.
function sbUsedLine(c) {
  const parts = [`temp ${c.temperature}`, `top-p ${c.top_p}`, `top-k ${c.top_k}`];
  if (c.min_p) parts.push(`min-p ${c.min_p}`);
  if (c.repeat_penalty !== 1) parts.push(`repeat ${c.repeat_penalty}`);
  if (c.presence_penalty) parts.push(`presence ${c.presence_penalty}`);
  parts.push(c.seed_on ? `seed ${c.seed}` : "random seed");
  parts.push(c.think ? `thinking ≤ ${c.think_budget ? fmtTokens(c.think_budget) : "∞"}` : "no thinking");
  if (c.memory >= 0) parts.push(`memory ${c.memory}`);
  return parts.join(" · ");
}

const STOP_REASONS = {
  stop: ["Ended by itself", "The model produced its special end-of-reply token (or wrote one of your stop sequences)."],
  length: ["Hit max length", "It ran into the Max reply length cap and was cut off, wherever it was."],
  cancelled: ["Stopped by you", "You stopped it before it finished."],
};

function sbTokenHtml(tokens) {
  return tokens
    .map((tk, i) => {
      const u = tk.p == null ? 0 : 1 - tk.p;
      const text = escapeHtml(tk.t).replace(/\n/g, '<span class="tok-nl">↵</span>\n');
      return `<span class="tok" data-i="${i}" style="--u:${u.toFixed(3)}">${text}</span>`;
    })
    .join("");
}

function renderSbTurn(turn) {
  const bubble = turn.bubble;
  const tokensView = turn.view === "tokens" && turn.tokens.some((t) => t.p != null);
  bubble.classList.toggle("md", !tokensView);
  bubble.classList.toggle("tokens", tokensView);
  if (!turn.text) return;
  bubble.innerHTML = tokensView
    ? sbTokenHtml(turn.tokens) + (turn.live ? '<span class="caret"></span>' : "")
    : renderMarkdown(turn.text, turn.live);
  scrollChat();
}

function sbChip(text, tip, cls = "") {
  return `<span class="sb-chip ${cls}" title="${escapeHtml(tip)}">${escapeHtml(text)}</span>`;
}

function finishSbTurn(turn, st) {
  turn.live = false;
  turn.el.classList.remove("streaming");
  turn.bubble.querySelector(".typing")?.remove();
  if (turn.th) endThoughts(turn.th);
  if (!turn.text.trim()) {
    turn.bubble.innerHTML = `<span class="no-answer">${st?.done_reason === "cancelled" ? "Stopped before it answered." : "No reply."}</span>`;
  } else {
    renderSbTurn(turn);
  }
  if (!st) return;
  const chips = [];
  chips.push(sbChip(`${st.reply_tokens.toLocaleString()} reply token${st.reply_tokens === 1 ? "" : "s"}`,
    "How many tokens the reply is. Each one took a full pass through the whole model."));
  if (st.think_tokens) chips.push(sbChip(`${st.think_tokens.toLocaleString()} thinking`,
    "Tokens it spent reasoning before answering. You don't see them in the reply, but they cost the same time."));
  if (st.eval_secs > 0) chips.push(sbChip(`${(st.eval_tokens / st.eval_secs).toFixed(1)} tok/s`,
    "Generation speed: how many tokens per second your GPU produced."));
  if (st.prompt_tokens) chips.push(sbChip(`read ${st.prompt_tokens.toLocaleString()} tokens`,
    `Before writing anything, it read the whole request (system prompt + history + your message): ${st.prompt_tokens.toLocaleString()} tokens` +
    (st.prompt_secs ? ` in ${fmtSecs(st.prompt_secs)}. Ollama reuses what it read last time when the start is unchanged, which is why that can be quick.` : ".")));
  if (st.ttft != null) chips.push(sbChip(`first token ${fmtSecs(st.ttft)}`,
    "Time until the first word of the reply appeared (includes loading, reading the prompt and any thinking)."));
  if (st.load_secs > 0.5) chips.push(sbChip(`loaded model ${fmtSecs(st.load_secs)}`,
    "Ollama had to load the model into memory first, e.g. after a context window change.", "warn"));
  if (st.budget_hit) chips.push(sbChip(`thinking budget hit`,
    `It reached the ${st.budget_hit.toLocaleString()}-token thinking budget, so this app stopped it and made a second request asking for an answer from its notes.`, "warn"));
  const reason = STOP_REASONS[st.done_reason];
  if (reason) chips.push(sbChip(reason[0], reason[1], st.done_reason === "stop" ? "" : "warn"));

  const meta = document.createElement("div");
  meta.className = "sb-meta";
  const canTokens = turn.tokens.some((t) => t.p != null);
  meta.innerHTML =
    `<div class="sb-chips">${chips.join("")}</div>` +
    `<div class="sb-used"></div>` +
    `<div class="sb-actions">` +
    (canTokens ? `<button class="sb-act" data-a="view"></button>` : "") +
    `<button class="sb-act" data-a="req">Request</button>` +
    `<button class="sb-act sb-regen" data-a="regen" title="Ask the same question again with the settings on the left. This reply stays here to compare.">` +
    `<svg viewBox="0 0 24 24" class="ico"><path d="M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>Regenerate</button>` +
    `</div><pre class="sb-req"></pre>`;
  meta.querySelector(".sb-used").textContent = sbUsedLine(turn.cfg);
  meta.querySelector(".sb-req").textContent = turn.requests
    .map((b, i) => (turn.requests.length > 1 ? `// Request ${i + 1}${i ? ": answer from the notes" : ""}\n` : "") + JSON.stringify(b, null, 2))
    .join("\n\n");
  const viewBtn = meta.querySelector('[data-a="view"]');
  const label = () => viewBtn && (viewBtn.textContent = turn.view === "tokens" ? "Show text" : "Show tokens");
  label();
  meta.addEventListener("click", (e) => {
    const a = e.target.closest(".sb-act")?.dataset.a;
    if (a === "view") {
      turn.view = turn.view === "tokens" ? "text" : "tokens";
      label();
      renderSbTurn(turn);
    } else if (a === "req") {
      meta.classList.toggle("req-open");
    } else if (a === "regen") {
      regenerate(turn);
    }
  });
  turn.el.appendChild(meta);
  if (canTokens && turn.view === "tokens") {
    const legend = document.createElement("div");
    legend.className = "tok-legend";
    legend.innerHTML = '<span>sure</span><i></i><span>unsure</span><em>hover a token</em>';
    turn.el.insertBefore(legend, turn.bubble);
  }
  scrollChat();
}

async function regenerate(turn) {
  if (!api) return;
  const text = await api.sandbox_regenerate(sb.cfg);
  if (!text) return toast("Nothing to regenerate");
  // Keep the old reply on screen to compare, but it's no longer in the model's memory.
  turn.el.classList.add("superseded");
  turn.el.querySelector(".who").textContent = "Earlier try · not in memory";
}

Object.assign(handlers, {
  sb_start({ id }) {
    const el = addMessage("bot streaming sb-turn", null, assistantLabel(), $("sbChat"));
    const bubble = el.querySelector(".bubble");
    bubble.innerHTML = '<span class="typing"><i></i><i></i><i></i></span>';
    sb.turns.set(id, { el, bubble, text: "", tokens: [], requests: [], live: true,
                       view: sb.cfg.logprobs ? "tokens" : "text", cfg: { ...sb.cfg }, th: null });
  },
  sb_request({ id, body }) {
    sb.turns.get(id)?.requests.push(body);
  },
  sb_thought({ id, t }) {
    const turn = sb.turns.get(id);
    if (!turn) return;
    const th = turn.th || (turn.th = makeThoughts(turn));
    th.body.append(t);
    if (th.el.open) th.body.scrollTop = th.body.scrollHeight;
  },
  sb_token({ id, t, tokens }) {
    const turn = sb.turns.get(id);
    if (!turn) return;
    if (turn.th) endThoughts(turn.th);
    turn.text += t;
    turn.tokens.push(...(tokens?.length ? tokens : [{ t, p: null, alt: [] }]));
    renderSbTurn(turn);
  },
  sb_note({ id, t }) {
    const turn = sb.turns.get(id);
    if (!turn) return;
    const el = document.createElement("div");
    el.className = "sb-note";
    el.textContent = t;
    turn.el.insertBefore(el, turn.bubble);
    scrollChat();
  },
  sb_done({ id, stats }) {
    const turn = sb.turns.get(id);
    if (!turn) return;
    finishSbTurn(turn, stats);
    sb.turns.delete(id);
    sbDone.set(id, turn); // kept for the token tooltips
  },
});
const sbDone = new Map(); // finished turn id -> turn

// Hovering a token: how sure it was, and what else it nearly picked.
function tokLabel(t) {
  return t.replace(/ /g, "·").replace(/\n/g, "↵").replace(/\t/g, "⇥") || "∅";
}

function showTokTip(span) {
  const msg = span.closest(".msg");
  const turn = [...sb.turns.values(), ...sbDone.values()].find((x) => x.el === msg);
  const tk = turn?.tokens[+span.dataset.i];
  const tip = $("tokTip");
  if (!tk || tk.p == null) return (tip.classList.remove("show"));
  const pct = (p) => (p >= 0.995 ? ">99%" : p < 0.001 ? "<0.1%" : `${(p * 100).toFixed(p < 0.1 ? 1 : 0)}%`);
  const top = tk.alt[0];
  const rows = tk.alt
    .map(([t, p]) => `<div class="tt-row${t === tk.t ? " picked" : ""}"><code>${escapeHtml(tokLabel(t))}</code>` +
                      `<span class="tt-bar"><i style="width:${Math.max(2, p * 100)}%"></i></span><span>${pct(p)}</span></div>`)
    .join("");
  const tookLess = top && top[0] !== tk.t && top[1] > tk.p;
  tip.innerHTML =
    `<div class="tt-head"><code>${escapeHtml(tokLabel(tk.t))}</code><b>${pct(tk.p)}</b></div>` +
    `<div class="tt-sub">How likely the model thought this token was. Its top ${tk.alt.length} options:</div>${rows}` +
    (tookLess ? `<div class="tt-foot">It didn't pick its favourite: sampling is random, and temperature, top-p and top-k decide how often an underdog wins.</div>`
              : tk.p > 0.9 ? `<div class="tt-foot">Almost no doubt here.</div>` : "");
  const r = span.getBoundingClientRect();
  tip.classList.add("show");
  const w = tip.offsetWidth, h = tip.offsetHeight;
  tip.style.left = `${Math.min(Math.max(8, r.left + r.width / 2 - w / 2), innerWidth - w - 8)}px`;
  tip.style.top = `${r.top - h - 8 < 8 ? r.bottom + 8 : r.top - h - 8}px`;
}

$("sbChat").addEventListener("mouseover", (e) => {
  const span = e.target.closest(".tok");
  if (span) showTokTip(span);
});
$("sbChat").addEventListener("mouseout", (e) => {
  if (e.target.closest(".tok") && !e.relatedTarget?.closest?.(".tok")) $("tokTip").classList.remove("show");
});
$("sbChat").addEventListener("scroll", () => $("tokTip").classList.remove("show"));

async function initSandbox() {
  buildSandboxPanel();
  const r = await api.sandbox_init();
  sb.defaults = r.defaults;
  sb.cfg = r.config;
  sb.presets = r.presets;
  fillSelect($("sbPreset"), [...r.presets.map((p) => p.id), "custom"], null,
             (id) => r.presets.find((p) => p.id === id)?.label || "Custom (your own)");
  applySbConfig();
  sbEmptyState();
  loadSbModel();
}

async function boot() {
  api = window.pywebview.api;
  bindSettings();
  const info = await api.init();
  applyFolder(info.code_folder);
  codeEmptyState();
  settings = info.settings;
  await initSandbox();
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

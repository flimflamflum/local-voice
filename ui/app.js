"use strict";

const $ = (id) => document.getElementById(id);
const body = document.body;

const STATUS = {
  idle:         ["Ready",       'Press <kbd>Space</kbd> or tap the mic to talk'],
  listening:    ["Listening…",  "Go ahead. I'll stop when you pause, or hit <kbd>Space</kbd>"],
  transcribing: ["Got it…",     "Turning your voice into words"],
  thinking:     ["Thinking…",   ""],
  speaking:     ["Speaking",    '<kbd>Esc</kbd> to interrupt &nbsp;·&nbsp; <kbd>Space</kbd> to cut in'],
};

// Orb colours per state: [core, edge] as RGB
const PALETTE = {
  idle:         [[79, 209, 255], [124, 92, 255]],
  listening:    [[62, 240, 180], [20, 184, 166]],
  transcribing: [[62, 240, 180], [79, 209, 255]],
  thinking:     [[177, 140, 255], [255, 92, 168]],
  speaking:     [[255, 181, 71], [255, 107, 92]],
};

let api = null;
let state = "idle";
let micLevel = 0;
let settings = {};
const bubbles = new Map(); // turn id -> {el, text}

// ------------------------------------------------------------------ chat
function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function renderMarkdownLite(text) {
  const parts = text.split(/```/);
  return parts
    .map((part, i) => {
      if (i % 2 === 1) {
        const code = part.replace(/^[\w+-]*\n/, "");
        return `<pre>${escapeHtml(code)}</pre>`;
      }
      if (i > 0) part = part.replace(/^\n/, "");
      if (i < parts.length - 1) part = part.replace(/\n$/, "");
      return escapeHtml(part)
        .replace(/`([^`\n]+)`/g, "<code>$1</code>")
        .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
        .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
    })
    .join("");
}

function addMessage(kind, text, label) {
  $("emptyState")?.remove();
  const wrap = document.createElement("div");
  wrap.className = `msg ${kind}`;
  wrap.innerHTML = `<span class="who"></span><div class="bubble"></div>`;
  wrap.querySelector(".who").textContent = label;
  const bubble = wrap.querySelector(".bubble");
  if (text != null) bubble.innerHTML = renderMarkdownLite(text);
  $("chat").appendChild(wrap);
  scrollChat();
  return wrap;
}

function scrollChat() {
  const chat = $("chat");
  chat.scrollTop = chat.scrollHeight;
}

function assistantLabel() {
  return settings.assistant_name || "Jarvis";
}

// --------------------------------------------------------------- events
const handlers = {
  state(s) {
    state = s;
    body.dataset.state = s;
    const [label, hint] = STATUS[s] || STATUS.idle;
    $("statusLabel").textContent = label;
    $("statusHint").innerHTML = s === "thinking" ? escapeHtml(prettyModel(settings.model)) : hint;
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
  assistant_done(id) {
    const b = bubbles.get(id);
    if (!b) return;
    finishReveal(b);
    closeBubble(id, b);
  },
  // Interrupted: keep only what was actually said.
  assistant_cut(id) {
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
    addMessage("bot error", msg, "System");
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
  b.el.querySelector(".bubble").innerHTML = renderMarkdownLite(b.text);
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
  if (!b.text.trim()) b.el.remove();
  bubbles.delete(id);
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
  api?.toggle_listen();
}
function stopAll() {
  api?.stop();
}

$("micBtn").addEventListener("click", talk);
$("stopBtn").addEventListener("click", stopAll);
$("newChatBtn").addEventListener("click", newChat);

function newChat() {
  api?.new_chat();
  bubbles.clear();
  $("chat").innerHTML =
    '<div class="empty" id="emptyState"><div class="empty-title">Fresh start.</div><div class="empty-sub">Memory wiped. What\'s on your mind?</div></div>';
}

$("textForm").addEventListener("submit", (e) => {
  e.preventDefault();
  const input = $("textInput");
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  api?.send_text(text);
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
  } else if (e.ctrlKey && e.key.toLowerCase() === "n") {
    e.preventDefault();
    newChat();
  }
});

// --------------------------------------------------------------- settings
function openDrawer() {
  body.classList.add("drawer-open");
}
function closeDrawer() {
  body.classList.remove("drawer-open");
}
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
  $("handsFree").addEventListener("change", (e) => {
    save({ hands_free: e.target.checked });
    toast(e.target.checked ? "Hands-free on: I'll keep listening after I reply" : "Hands-free off");
  });
  $("whisperSelect").addEventListener("change", (e) => save({ whisper_model: e.target.value }));
  $("userName").addEventListener("change", (e) => save({ user_name: e.target.value.trim() }));
  $("assistantName").addEventListener("change", (e) => {
    const name = e.target.value.trim() || "Jarvis";
    save({ assistant_name: name });
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
  $("whisperSelect").value = s.whisper_model;
  $("userName").value = s.user_name || "";
  $("assistantName").value = s.assistant_name || "Jarvis";
  $("brandName").textContent = (s.assistant_name || "Jarvis").toUpperCase();
}

async function boot() {
  api = window.pywebview.api;
  bindSettings();
  const info = await api.init();
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
  const t = now / 1000;
  const [tc, te] = PALETTE[state] || PALETTE.idle;
  for (let i = 0; i < 3; i++) {
    col.core[i] = lerp(col.core[i], tc[i], 0.06);
    col.edge[i] = lerp(col.edge[i], te[i], 0.06);
  }

  // what the blob should be doing
  let target;
  if (state === "listening") target = 0.08 + micLevel * 0.9;
  else if (state === "speaking") {
    if (now > nextSyllable) {
      speakTarget = Math.random() < 0.18 ? 0.05 : 0.25 + Math.random() * 0.55;
      nextSyllable = now + 70 + Math.random() * 110;
    }
    target = speakTarget;
  } else if (state === "thinking" || state === "transcribing") target = 0.18 + Math.sin(t * 4) * 0.05;
  else target = 0.05 + Math.sin(t * 1.2) * 0.025; // idle breathing
  energy = lerp(energy, target, state === "speaking" ? 0.25 : 0.15);

  const speed = state === "thinking" ? 3.2 : state === "idle" ? 0.7 : 1.6;
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
  const arcAlpha = state === "thinking" || state === "transcribing" ? 0.85 : 0.18;
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

"""Sandbox mode: a plain text chat where every knob is exposed, to learn how a language model works.

Nothing here is hidden from the user: the exact request sent to Ollama is shown, along with the
token counts, timings, why the reply stopped and (with logprobs on) how sure the model was about
every token it picked.
"""
import json
import math
import time

import requests

# What a fresh sandbox starts with. The sampling values are Ollama's own built-in defaults.
DEFAULTS = {
    "system": "",
    "think": False,
    "think_budget": 2048,     # max thinking tokens; 0 = no limit
    "temperature": 0.8,
    "top_p": 0.9,
    "top_k": 40,
    "min_p": 0.0,
    "repeat_penalty": 1.1,
    "presence_penalty": 0.0,
    "num_predict": 1024,      # max reply tokens; -1 = no limit
    "num_ctx": 8192,
    "memory": -1,             # earlier exchanges sent along; -1 = all of them
    "seed_on": False,
    "seed": 42,
    "stop": "",               # comma-separated stop sequences
    "logprobs": True,         # ask for each token's probability and the runners-up
    "show_request": False,
}
OLLAMA_SAMPLING = {"temperature": 0.8, "top_p": 0.9, "top_k": 40, "min_p": 0.0,
                   "repeat_penalty": 1.1, "presence_penalty": 0.0}
SAMPLING_KEYS = tuple(OLLAMA_SAMPLING)
TOP_LOGPROBS = 5  # runners-up shown when you hover a token

PRESETS = [
    ("none", "Nothing (blank)", ""),
    ("helpful", "Helpful assistant", "You are a helpful assistant."),
    ("eli5", "Explain like I'm 5",
     "Explain everything as if talking to a curious five-year-old. Use short sentences, simple words "
     "and one everyday comparison. Never use jargon."),
    ("pirate", "Pirate", "You are a jolly pirate. Talk like a pirate in every reply, but keep the facts right."),
    ("one_word", "One word only", "Answer every message with exactly one word. Never more."),
    ("json", "JSON only",
     'Reply only with a JSON object like {"answer": "...", "confidence": 0.0 to 1.0}. No other text, '
     "no code fences."),
    ("socratic", "Socratic tutor",
     "You are a tutor. Never give the answer directly. Reply with one guiding question that helps the "
     "student work it out themselves."),
    ("wrong", "Always wrong (quiz show)",
     "You are the host of a comedy quiz show where every answer you give is confidently and obviously "
     "wrong. Never admit it's wrong."),
]


def _prob(logprob):
    return round(math.exp(logprob), 4) if logprob is not None else None


def _tokens(chunk, text):
    """The chunk's logprobs, compacted for the UI: [{t, p, alt: [[token, p], ...]}].

    A token can be part of a character (an emoji or curly quote is several bytes), so when the
    chunk is a single token its streamed text is used: that's always whole characters."""
    out = []
    lps = chunk.get("logprobs") or []
    for lp in lps:
        out.append({
            "t": lp.get("token", ""),
            "p": _prob(lp.get("logprob")),
            "alt": [[a.get("token", ""), _prob(a.get("logprob"))] for a in lp.get("top_logprobs") or []],
        })
    if len(out) == 1:
        out[0]["t"] = text
    return out


def _secs(ns):
    return round((ns or 0) / 1e9, 3)


class Sandbox:
    def __init__(self, ollama):
        self.ollama = ollama
        self.history = []  # [{"role", "content"}], user/assistant pairs
        self._info = {}

    def clear(self):
        self.history.clear()

    def pop_last(self):
        """Forget the last exchange and return its question, so it can be asked again."""
        while self.history and self.history[-1]["role"] == "assistant":
            self.history.pop()
        if self.history and self.history[-1]["role"] == "user":
            return self.history.pop()["content"]
        return None

    # ------------------------------------------------------------ model facts
    def model_info(self, model):
        """What Ollama knows about the model: size, quantisation, abilities and its own defaults."""
        if model in self._info:
            return self._info[model]
        r = requests.post(f"{self.ollama.base_url}/api/show", json={"model": model}, timeout=10)
        r.raise_for_status()
        d = r.json()
        details = d.get("details") or {}
        trained_ctx = next((v for k, v in (d.get("model_info") or {}).items()
                            if k.endswith(".context_length")), None)
        sampling = dict(OLLAMA_SAMPLING)
        own = {}
        for line in (d.get("parameters") or "").splitlines():
            parts = line.split(None, 1)
            if len(parts) == 2 and parts[0] in sampling:
                try:
                    own[parts[0]] = float(parts[1])
                except ValueError:
                    pass
        sampling.update(own)
        info = {
            "model": model,
            "family": details.get("family", ""),
            "params": details.get("parameter_size", ""),
            "quant": details.get("quantization_level", ""),
            "format": details.get("format", ""),
            "trained_ctx": trained_ctx,
            "capabilities": d.get("capabilities") or [],
            "defaults": sampling,          # what the model runs with if you send nothing
            "own_defaults": sorted(own),   # which of those its Modelfile sets itself
            "size": self.ollama._sizes.get(model, 0),
        }
        self._info[model] = info
        return info

    # --------------------------------------------------------------- requests
    def messages(self, cfg, text):
        past = self.history
        memory = int(cfg.get("memory", -1))
        if memory >= 0:
            past = past[-2 * memory:] if memory else []
        system = (cfg.get("system") or "").strip()
        return ([{"role": "system", "content": system}] if system else []) + list(past) + \
            [{"role": "user", "content": text}]

    def request(self, model, cfg, messages, think):
        opts = {k: float(cfg.get(k, OLLAMA_SAMPLING[k])) for k in SAMPLING_KEYS}
        opts["top_k"] = int(opts["top_k"])
        opts["num_ctx"] = int(cfg.get("num_ctx") or 8192)
        reply = int(cfg.get("num_predict", 1024))
        budget = int(cfg.get("think_budget") or 0)
        # Thinking tokens count toward num_predict, so make room for the budget on top of the reply.
        opts["num_predict"] = -1 if reply < 0 or (think and not budget) else reply + (budget if think else 0)
        if cfg.get("seed_on"):
            opts["seed"] = int(cfg.get("seed") or 0)
        stops = [s for s in (x.strip() for x in str(cfg.get("stop") or "").split(",")) if s]
        if stops:
            opts["stop"] = stops
        body = {
            "model": model,
            "messages": messages,
            "stream": True,
            "think": bool(think),
            "keep_alive": "30m",
            "options": self.ollama._options(model, **opts),
        }
        if cfg.get("logprobs"):
            body["logprobs"] = True
            body["top_logprobs"] = TOP_LOGPROBS
        return body

    def _stream(self, body, cancel, ui):
        """Yield Ollama's raw chunks. Retries once without anything the model refuses."""
        for _ in range(4):
            with requests.post(f"{self.ollama.base_url}/api/chat", json=body, stream=True, timeout=300) as r:
                if r.status_code == 400 and body.get("think") and "think" in r.text.lower():
                    self.ollama._no_think.add(body["model"])
                    body["think"] = False
                    ui.note("This model can't think, so thinking was switched off for this reply.")
                    continue
                if r.status_code == 400 and body.get("logprobs") and "logprob" in r.text.lower():
                    body.pop("logprobs")
                    body.pop("top_logprobs", None)
                    ui.note("This model can't report token probabilities.")
                    continue
                if r.status_code >= 500 and "num_gpu" in body["options"]:
                    # Didn't fit fully on the GPU (e.g. a huge context window): let Ollama split it.
                    # Only for this request, so the main chat keeps its full-GPU speed.
                    body["options"].pop("num_gpu")
                    continue
                if r.status_code >= 400:
                    try:
                        msg = r.json().get("error") or r.text
                    except ValueError:
                        msg = r.text
                    raise RuntimeError(msg)
                for line in r.iter_lines():
                    if cancel.is_set():
                        return
                    if not line:
                        continue
                    chunk = json.loads(line)
                    if chunk.get("error"):
                        raise RuntimeError(chunk["error"])
                    yield chunk
                    if chunk.get("done"):
                        return
                return

    def run(self, model, text, cfg, cancel, ui):
        """One sandbox reply. Streams to `ui` and returns the stats shown under it."""
        think = bool(cfg.get("think")) and self.ollama.can_think(model)
        if cfg.get("think") and not think:
            ui.note("This model can't think, so thinking is off.")
        budget = int(cfg.get("think_budget") or 0) if think else 0
        messages = self.messages(cfg, text)
        self.history.append({"role": "user", "content": text})
        body = self.request(model, cfg, messages, think)
        ui.request(body)

        started = time.monotonic()
        stats = {"think_tokens": 0, "reply_tokens": 0, "prompt_tokens": 0, "requests": 1,
                 "done_reason": None, "ttft": None}
        reply, notes, over_budget = "", "", False
        stream = self._stream(body, cancel, ui)
        try:
            for chunk in stream:
                msg = chunk.get("message") or {}
                if msg.get("thinking"):
                    stats["think_tokens"] += 1
                    notes += msg["thinking"]
                    ui.thought(msg["thinking"])
                    if budget and stats["think_tokens"] >= budget and not reply:
                        over_budget = True
                        break  # closing the request stops Ollama generating
                if msg.get("content"):
                    if stats["ttft"] is None:
                        stats["ttft"] = round(time.monotonic() - started, 3)
                    stats["reply_tokens"] += 1
                    reply += msg["content"]
                    ui.token(msg["content"], _tokens(chunk, msg["content"]))
                if chunk.get("done"):
                    self._add_timings(stats, chunk)
        finally:
            stream.close()

        if over_budget and not cancel.is_set():
            stats["budget_hit"] = budget
            ui.thought_end(f"Hit the thinking budget of {budget:,} tokens.")
            ui.note(f"It used up its {budget:,}-token thinking budget, so the app stopped it and sent a "
                    "second request asking for an answer from its notes so far.")
            second = self._from_notes(model, cfg, messages, notes, text)
            ui.request(second, second=True)
            stats["requests"] = 2
            stream = self._stream(second, cancel, ui)
            try:
                for chunk in stream:
                    token = (chunk.get("message") or {}).get("content")
                    if token:
                        if stats["ttft"] is None:
                            stats["ttft"] = round(time.monotonic() - started, 3)
                        stats["reply_tokens"] += 1
                        reply += token
                        ui.token(token, _tokens(chunk, token))
                    if chunk.get("done"):
                        self._add_timings(stats, chunk)
            finally:
                stream.close()

        if cancel.is_set():
            stats["done_reason"] = "cancelled"
        stats["secs"] = round(time.monotonic() - started, 3)
        if reply.strip():
            self.history.append({"role": "assistant", "content": reply})
        else:
            self.history.pop()  # nothing came back: don't leave a question without an answer
        return stats

    def _from_notes(self, model, cfg, messages, notes, text):
        notes = notes.strip()
        if len(notes) > 6000:
            notes = "..." + notes[-6000:]
        nudge = ("[Private notes you made while thinking about my message. Use them, but never mention them.]\n"
                 f"{notes}\n[End of notes.]\n\nMy message: {text}\n\nReply to my message now with your final answer.")
        return self.request(model, cfg, messages[:-1] + [{"role": "user", "content": nudge}], think=False)

    @staticmethod
    def _add_timings(stats, chunk):
        stats["prompt_tokens"] += chunk.get("prompt_eval_count", 0)
        stats["done_reason"] = chunk.get("done_reason")
        stats["load_secs"] = stats.get("load_secs", 0) + _secs(chunk.get("load_duration"))
        stats["prompt_secs"] = stats.get("prompt_secs", 0) + _secs(chunk.get("prompt_eval_duration"))
        stats["eval_tokens"] = stats.get("eval_tokens", 0) + chunk.get("eval_count", 0)
        stats["eval_secs"] = stats.get("eval_secs", 0) + _secs(chunk.get("eval_duration"))

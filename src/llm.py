"""Streaming chat against the local Ollama HTTP API."""
import json
import threading
import time

import requests

from src.textutil import RepeatWatch

NUM_CTX = 4096  # small context keeps more of the model on your 4 GB GPU

# Ollama's VRAM estimate is cautious: on a 4 GB card it puts ~25% of a 4B model on the
# CPU even though it fits, which halves the speed (17 vs 43 tok/s). Models whose file is
# at most this big get every layer forced onto the GPU; if that ever fails to load
# (e.g. another app is using VRAM) we fall back to Ollama's own split for that model.
FULL_GPU_MAX_BYTES = 3_600_000_000

# Thinking budget. Small models can reason for thousands of tokens; left alone they use up
# the whole token allowance (and the 4096-token context) and never get to the answer.
# Past either limit we stop the reasoning and have the model answer from its notes so far.
THINK_MAX_TOKENS = 2000   # ~24 s on a 2B model, ~45 s on a 4B; leaves context for the answer
THINK_MAX_SECONDS = 45    # big models that spill onto the CPU think far slower
NOTES_MAX_CHARS = 6000    # how much of the cut-off reasoning to hand back (the end matters most)

# Sampling Qwen recommends for its Qwen 3.5 models. The presence penalty pushes the model
# away from words it has already used, which is what stops small models looping while they
# think; a temperature below 1 makes loops more likely. These replace the temperature setting.
THINK_SAMPLING = {"temperature": 1.0, "top_p": 0.95, "top_k": 20, "presence_penalty": 1.5}
ANSWER_SAMPLING = {"temperature": 0.7, "top_p": 0.8, "top_k": 20, "presence_penalty": 1.5}


class _ThinkingTooLong(Exception):
    def __init__(self, why):
        super().__init__(why)
        self.why = why


class OllamaClient:
    def __init__(self, base_url: str):
        # On Windows "localhost" tries IPv6 (::1) first; Ollama only listens on IPv4,
        # so every request stalled ~2 s before falling back. Go straight to IPv4.
        self.base_url = base_url.rstrip("/").replace("://localhost", "://127.0.0.1")
        self._sizes = {}  # model name -> file size in bytes
        self._no_force = set()  # models that wouldn't load fully onto the GPU
        self._no_think = set()  # models Ollama says can't think (e.g. llama3.1)

    def list_models(self):
        r = requests.get(f"{self.base_url}/api/tags", timeout=5)
        r.raise_for_status()
        models = r.json().get("models", [])
        self._sizes = {m["name"]: m.get("size", 0) for m in models}
        return sorted(self._sizes)

    def _options(self, model, **extra):
        """Chat and warm-up must send identical options, or Ollama reloads the model."""
        opts = {"num_ctx": NUM_CTX}
        if model not in self._sizes:
            try:
                self.list_models()
            except requests.RequestException:
                pass
        size = self._sizes.get(model, 0)
        if 0 < size <= FULL_GPU_MAX_BYTES and model not in self._no_force:
            opts["num_gpu"] = 999  # all layers on the GPU
        opts.update(extra)
        return opts

    def _gave_up_full_gpu(self, model, options, response):
        """A forced full-GPU load failed: remember that and tell the caller to retry without it."""
        if response.status_code >= 500 and "num_gpu" in options:
            self._no_force.add(model)
            options.pop("num_gpu")
            return True
        return False

    def warm_up(self, model: str):
        """Load the model into memory so the first reply is fast."""
        options = self._options(model)
        try:
            for _ in range(2):
                r = requests.post(
                    f"{self.base_url}/api/generate",
                    json={"model": model, "keep_alive": "30m", "options": options},
                    timeout=300,
                )
                if not self._gave_up_full_gpu(model, options, r):
                    break
        except requests.RequestException:
            pass

    def unload(self, model: str):
        """Evict a model from RAM/VRAM right now (keep_alive=0)."""
        try:
            requests.post(
                f"{self.base_url}/api/generate",
                json={"model": model, "keep_alive": 0},
                timeout=30,
            )
        except requests.RequestException:
            pass

    def switch_model(self, old: str, new: str):
        """Unload the old model first so both are never in memory at once, then preload the new one."""
        if old and old != new:
            self.unload(old)
        if new:
            self.warm_up(new)

    def can_think(self, model):
        """False once Ollama has refused thinking for this model."""
        return model not in self._no_think

    def stream_chat(self, model, messages, cancel: threading.Event, temperature=0.8,
                    think=False, on_thinking=None):
        """Yield content tokens. Stops early when `cancel` is set.

        With think=True the model reasons first; that reasoning arrives in a separate
        field and goes to on_thinking(text), never into the yielded reply.
        """
        think = bool(think) and self.can_think(model)
        sampling = THINK_SAMPLING if think else {"temperature": temperature}
        body = {
            "model": model,
            "messages": messages,
            "stream": True,
            "think": think,
            "keep_alive": "30m",
            # num_predict is a last-resort token cap (ReplyLimiter normally stops the reply far
            # sooner). Thinking tokens count toward it too, so give a thinking model more room.
            "options": self._options(model, num_predict=3072 if think else 1024, **sampling),
        }
        if not think:
            yield from self._chat(body, cancel, on_thinking)
            return

        tokens, start, watch = 0, time.monotonic(), RepeatWatch()
        on_thinking = on_thinking or (lambda _t: None)

        def collect(text):
            nonlocal tokens
            tokens += 1
            on_thinking(text)
            # Raising closes the request, so Ollama stops generating too.
            if watch.feed(text):
                raise _ThinkingTooLong("Started repeating itself")
            if tokens >= THINK_MAX_TOKENS or time.monotonic() - start > THINK_MAX_SECONDS:
                raise _ThinkingTooLong("Thought for too long")

        answered, meta = False, {}
        try:
            for token in self._chat(body, cancel, collect, meta):
                answered = True
                yield token
            # It stopped on its own without answering. Small models often write the
            # answer inside their thinking and end there; or the token cap ran out.
            why = ("Ran out of room while thinking" if meta.get("done_reason") == "length"
                   else "Finished thinking without a separate answer")
        except _ThinkingTooLong as e:
            why = e.why
        if answered or cancel.is_set() or not tokens:
            return
        # Answer from the notes (minus any loop) in a quick second, non-thinking request.
        on_thinking(f"\n\n[{why}, so it answered from these notes.]")
        yield from self._answer_from_notes(model, messages, watch.deduped(), cancel)

    def _answer_from_notes(self, model, messages, notes, cancel):
        notes = notes.strip()
        if len(notes) > NOTES_MAX_CHARS:
            notes = "..." + notes[-NOTES_MAX_CHARS:]
        last = messages[-1]
        # The question goes last: small models answer whatever they read most recently.
        nudge = (
            "[Private notes you made while thinking about my message. Use them, but never mention them.]\n"
            f"{notes}\n"
            "[End of notes.]\n\n"
            f"My message: {last['content']}\n\n"
            "Reply to my message now with your final answer."
        )
        body = {
            "model": model,
            "messages": messages[:-1] + [{"role": last["role"], "content": nudge}],
            "stream": True,
            "think": False,
            "keep_alive": "30m",
            "options": self._options(model, num_predict=1024, **ANSWER_SAMPLING),
        }
        yield from self._chat(body, cancel)

    def _chat(self, body, cancel, on_thinking=None, meta=None):
        retry = False
        with requests.post(f"{self.base_url}/api/chat", json=body, stream=True, timeout=300) as r:
            if r.status_code == 400 and "think" in body and "think" in r.text.lower():
                if body["think"]:
                    self._no_think.add(body["model"])
                    body["options"]["num_predict"] = 1024
                body.pop("think")  # model/older server doesn't understand the flag
                retry = True
            elif self._gave_up_full_gpu(body["model"], body["options"], r):
                retry = True
            else:
                r.raise_for_status()
                yield from self._iter(r, cancel, on_thinking, meta)
        if retry:
            yield from self._chat(body, cancel, on_thinking, meta)

    @staticmethod
    def _iter(resp, cancel, on_thinking=None, meta=None):
        for line in resp.iter_lines():
            if cancel.is_set():
                return
            if not line:
                continue
            chunk = json.loads(line)
            if chunk.get("error"):
                raise RuntimeError(chunk["error"])
            message = chunk.get("message", {})
            thought = message.get("thinking", "")
            if thought and on_thinking:
                on_thinking(thought)
            token = message.get("content", "")
            if token:
                yield token
            if chunk.get("done"):
                if meta is not None:
                    meta["done_reason"] = chunk.get("done_reason")
                return

"""Streaming chat against the local Ollama HTTP API."""
import json
import re
import threading
import time

import requests

from src.textutil import RepeatWatch

# Qwen 3.5's attention keeps its cache tiny, so 8192 costs no extra VRAM or speed on a 4 GB card
# (measured on a 3050: 43 tok/s at 4k, 8k and 12k; 16k fails to load). It leaves room for search results.
NUM_CTX = 8192  # default; the user can change it in Settings (OllamaClient.num_ctx)
MAX_TOOL_ROUNDS = 2  # searches the model may run before it has to answer

# Ollama's VRAM estimate is cautious: on a 4 GB card it puts ~25% of a 4B model on the
# CPU even though it fits, which halves the speed (17 vs 43 tok/s). Models whose file is
# at most this big get every layer forced onto the GPU; if that ever fails to load
# (e.g. another app is using VRAM) we fall back to Ollama's own split for that model.
FULL_GPU_MAX_BYTES = 3_600_000_000

# Thinking budget. Small models can reason for thousands of tokens; left alone they use up
# the whole token allowance (and the 4096-token context) and never get to the answer.
# Past either limit we stop the reasoning and have the model answer from its notes so far.
# Ollama can only switch Qwen's thinking on or off, so the effort setting (Settings > Thinking
# effort) is this budget: (max thinking tokens, max seconds). Medium is the old fixed budget:
# ~24 s on a 2B model, ~45 s on a 4B. The seconds matter for big models that spill onto the CPU.
THINK_EFFORT = {
    "low": (600, 20),
    "medium": (2000, 45),
    "high": (5000, 120),
    "max": (12000, 300),
}
THINK_CTX_SHARE = 0.6     # thinking may use at most this much of the context; the rest is prompt + answer
NOTES_MAX_CHARS = 6000    # how much of the cut-off reasoning to hand back (the end matters most)

# Sampling Qwen recommends for its Qwen 3.5 models. The presence penalty pushes the model
# away from words it has already used, which is what stops small models looping while they
# think; a temperature below 1 makes loops more likely. These replace the temperature setting.
THINK_SAMPLING = {"temperature": 1.0, "top_p": 0.95, "top_k": 20, "presence_penalty": 1.5}
ANSWER_SAMPLING = {"temperature": 0.7, "top_p": 0.8, "top_k": 20, "presence_penalty": 1.5}


_PROMISE = re.compile(
    r"\b(let me|i'll|i will|i'm going to|gonna|one sec|hang on|give me a (sec|second|moment))\b"
    r".{0,40}\b(check|look|search|find|pull|google|see what|dig)",
    re.IGNORECASE | re.DOTALL,
)


def _promised_to_look(reply):
    """A short reply that only promises to look something up, without having done it."""
    reply = reply.replace("’", "'").strip()
    return 0 < len(reply) <= 220 and bool(_PROMISE.search(reply))


class _ThinkingTooLong(Exception):
    def __init__(self, why):
        super().__init__(why)
        self.why = why


class OllamaClient:
    def __init__(self, base_url: str):
        # On Windows "localhost" tries IPv6 (::1) first; Ollama only listens on IPv4,
        # so every request stalled ~2 s before falling back. Go straight to IPv4.
        self.base_url = base_url.rstrip("/").replace("://localhost", "://127.0.0.1")
        self.num_ctx = NUM_CTX
        self.think_effort = "medium"
        # Called after each finished request with the tokens the conversation now fills: the prompt
        # plus the visible reply (thinking is thrown away afterwards, so it isn't counted). Ollama
        # counts the whole prompt, cached or not (checked on Qwen 3.5 and llama3.1), and drops the
        # oldest messages itself when the prompt won't fit.
        self.on_context = None
        self._sizes = {}  # model name -> file size in bytes
        self._no_force = set()  # models that wouldn't load fully onto the GPU
        self._no_think = set()  # models Ollama says can't think (e.g. llama3.1)
        self._no_tools = set()  # models Ollama says can't call tools

    def list_models(self):
        r = requests.get(f"{self.base_url}/api/tags", timeout=5)
        r.raise_for_status()
        models = r.json().get("models", [])
        self._sizes = {m["name"]: m.get("size", 0) for m in models}
        return sorted(self._sizes)

    def _options(self, model, **extra):
        """Chat and warm-up must send identical options, or Ollama reloads the model."""
        opts = {"num_ctx": self.num_ctx}
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

    def think_budget(self):
        """(max thinking tokens, max seconds) for the current effort, kept inside the context."""
        tokens, seconds = THINK_EFFORT.get(self.think_effort, THINK_EFFORT["medium"])
        return min(tokens, max(500, int(self.num_ctx * THINK_CTX_SHARE))), seconds

    def can_think(self, model):
        """False once Ollama has refused thinking for this model."""
        return model not in self._no_think

    def can_use_tools(self, model):
        """False once Ollama has refused tools for this model."""
        return model not in self._no_tools

    def stream_chat(self, model, messages, cancel: threading.Event, temperature=0.8,
                    think=False, on_thinking=None, tools=None, call_tool=None, usage=None):
        """Yield content tokens. Stops early when `cancel` is set.

        With think=True the model reasons first; that reasoning arrives in a separate
        field and goes to on_thinking(text), never into the yielded reply.

        With tools (Ollama tool definitions) and call_tool(name, args) -> str, the model may
        call a tool instead of answering; we run it, hand back the result and let it go on.

        With a `usage` dict, token counts for every request this reply makes are added to it
        (see _iter): "prompt" (tokens read) and "output" (tokens generated, thinking included).
        """
        messages = list(messages)
        for round_ in range(MAX_TOOL_ROUNDS + 1):
            offer = tools if tools and call_tool and round_ < MAX_TOOL_ROUNDS else None
            meta = {}
            yield from self._one_reply(model, messages, cancel, temperature, think, on_thinking, offer, meta,
                                       usage=usage)
            calls = meta.get("tool_calls")
            if not calls and offer and round_ == 0 and _promised_to_look(meta.get("content", "")):
                # Small models often say "let me check" and stop without calling the tool. It's
                # already been said, so do the search it promised and let it answer from that.
                asked = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
                calls = [{"function": {"name": "web_search", "arguments": {"query": asked[:200]}}}]
            if not calls or cancel.is_set():
                return
            messages.append({"role": "assistant", "content": meta.get("content", ""), "tool_calls": calls})
            if meta.get("content", "").strip():
                yield "\n\n"  # it said something first ("let me check"): keep the answer apart from it
            for call in calls:
                fn = call.get("function") or {}
                args = fn.get("arguments") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {"query": args}
                result = call_tool(fn.get("name", ""), args)
                messages.append({"role": "tool", "tool_name": fn.get("name", ""), "content": result})
                if cancel.is_set():
                    return

    def chat_step(self, model, messages, cancel, meta, tools=None, think=False, on_thinking=None,
                  num_predict=None, sampling=None, usage=None):
        """One model turn for an agent loop (Vesper Code): yields reply tokens, and any tool
        calls the model makes land in meta["tool_calls"] for the caller to run."""
        yield from self._one_reply(model, messages, cancel, 0.7, think, on_thinking, tools, meta,
                                   num_predict=num_predict, sampling=sampling, usage=usage)

    def _one_reply(self, model, messages, cancel, temperature, think, on_thinking, tools, meta,
                   num_predict=None, sampling=None, usage=None):
        """One request to the model. Tool calls it makes land in meta["tool_calls"]."""
        think = bool(think) and self.can_think(model)
        sampling = sampling or (THINK_SAMPLING if think else {"temperature": temperature})
        max_think_tokens, max_think_seconds = self.think_budget()
        # num_predict is the room for the reply; thinking tokens count toward it too, so add the budget.
        num_predict = (num_predict or 1024) + (max_think_tokens if think else 0)
        body = {
            "model": model,
            "messages": messages,
            "stream": True,
            "think": think,
            "keep_alive": "30m",
            # num_predict is a last-resort token cap (ReplyLimiter normally stops the reply far sooner).
            "options": self._options(model, num_predict=num_predict, **sampling),
        }
        if tools and self.can_use_tools(model):
            body["tools"] = tools
        if not think:
            yield from self._chat(body, cancel, on_thinking, meta, usage)
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
            if tokens >= max_think_tokens or time.monotonic() - start > max_think_seconds:
                raise _ThinkingTooLong("Thought for too long")

        answered = False
        try:
            for token in self._chat(body, cancel, collect, meta, usage):
                answered = True
                yield token
            if meta.get("tool_calls"):
                return  # it chose to search; the caller runs it and asks again
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
        yield from self._answer_from_notes(model, messages, watch.deduped(), cancel, usage)

    def _answer_from_notes(self, model, messages, notes, cancel, usage=None):
        notes = notes.strip()
        if len(notes) > NOTES_MAX_CHARS:
            notes = "..." + notes[-NOTES_MAX_CHARS:]
        last = messages[-1]
        asked = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        # The question goes last: small models answer whatever they read most recently.
        nudge = (
            "[Private notes you made while thinking about my message. Use them, but never mention them.]\n"
            f"{notes}\n"
            "[End of notes.]\n\n"
            f"My message: {asked}\n\n"
            "Reply to my message now with your final answer."
        )
        if last["role"] == "user":
            messages = messages[:-1] + [{"role": "user", "content": nudge}]
        else:  # after a web search: keep the results, add the nudge after them
            messages = messages + [{"role": "user", "content": nudge}]
        body = {
            "model": model,
            "messages": messages,
            "stream": True,
            "think": False,
            "keep_alive": "30m",
            "options": self._options(model, num_predict=1024, **ANSWER_SAMPLING),
        }
        yield from self._chat(body, cancel, usage=usage)

    def _chat(self, body, cancel, on_thinking=None, meta=None, usage=None):
        retry = False
        with requests.post(f"{self.base_url}/api/chat", json=body, stream=True, timeout=300) as r:
            if r.status_code == 400 and "tools" in body and "tool" in r.text.lower():
                self._no_tools.add(body["model"])  # e.g. "llama2 does not support tools"
                body.pop("tools")
                retry = True
            elif r.status_code == 400 and "think" in body and "think" in r.text.lower():
                if body["think"]:
                    self._no_think.add(body["model"])
                    body["options"]["num_predict"] = 1024
                body.pop("think")  # model/older server doesn't understand the flag
                retry = True
            elif self._gave_up_full_gpu(body["model"], body["options"], r):
                retry = True
            else:
                r.raise_for_status()
                yield from self._iter(r, cancel, on_thinking, meta, usage)
        if retry:
            yield from self._chat(body, cancel, on_thinking, meta, usage)

    def _iter(self, resp, cancel, on_thinking=None, meta=None, usage=None):
        # Ollama reports exact token counts only in its final chunk. When we stop a reply early
        # (cut short, interrupted, thinking too long) that chunk never comes, so count the
        # streamed chunks instead: Ollama sends one per generated token.
        chunks, counted, said = 0, False, 0  # said = reply tokens, without the thinking
        try:
            for line in resp.iter_lines():
                if cancel.is_set():
                    return
                if not line:
                    continue
                chunk = json.loads(line)
                if chunk.get("error"):
                    raise RuntimeError(chunk["error"])
                if chunk.get("done") and usage is not None:
                    usage["prompt"] = usage.get("prompt", 0) + chunk.get("prompt_eval_count", 0)
                    usage["output"] = usage.get("output", 0) + chunk.get("eval_count", chunks)
                    counted = True
                if chunk.get("done") and self.on_context:
                    self.on_context(chunk.get("prompt_eval_count", 0) + said)
                message = chunk.get("message", {})
                thought = message.get("thinking", "")
                if thought:
                    chunks += 1
                    if on_thinking:
                        on_thinking(thought)
                if message.get("tool_calls") and meta is not None:
                    meta.setdefault("tool_calls", []).extend(message["tool_calls"])
                token = message.get("content", "")
                if token:
                    chunks += 1
                    said += 1
                    if meta is not None:
                        meta["content"] = meta.get("content", "") + token
                    yield token
                if chunk.get("done"):
                    if meta is not None:
                        meta["done_reason"] = chunk.get("done_reason")
                    return
        finally:
            if usage is not None and not counted:
                usage["output"] = usage.get("output", 0) + chunks

"""Streaming chat against the local Ollama HTTP API."""
import json
import threading

import requests

NUM_CTX = 4096  # small context keeps more of the model on your 4 GB GPU

# Ollama's VRAM estimate is cautious: on a 4 GB card it puts ~25% of a 4B model on the
# CPU even though it fits, which halves the speed (17 vs 43 tok/s). Models whose file is
# at most this big get every layer forced onto the GPU; if that ever fails to load
# (e.g. another app is using VRAM) we fall back to Ollama's own split for that model.
FULL_GPU_MAX_BYTES = 3_600_000_000


class OllamaClient:
    def __init__(self, base_url: str):
        # On Windows "localhost" tries IPv6 (::1) first; Ollama only listens on IPv4,
        # so every request stalled ~2 s before falling back. Go straight to IPv4.
        self.base_url = base_url.rstrip("/").replace("://localhost", "://127.0.0.1")
        self._sizes = {}  # model name -> file size in bytes
        self._no_force = set()  # models that wouldn't load fully onto the GPU

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

    def stream_chat(self, model, messages, cancel: threading.Event, temperature=0.8):
        """Yield content tokens. Stops early when `cancel` is set."""
        body = {
            "model": model,
            "messages": messages,
            "stream": True,
            "think": False,
            "keep_alive": "30m",
            "options": self._options(model, temperature=temperature),
        }
        yield from self._chat(body, cancel)

    def _chat(self, body, cancel):
        retry = False
        with requests.post(f"{self.base_url}/api/chat", json=body, stream=True, timeout=300) as r:
            if r.status_code == 400 and "think" in body and "think" in r.text.lower():
                body.pop("think")  # model/older server doesn't understand the flag
                retry = True
            elif self._gave_up_full_gpu(body["model"], body["options"], r):
                retry = True
            else:
                r.raise_for_status()
                yield from self._iter(r, cancel)
        if retry:
            yield from self._chat(body, cancel)

    @staticmethod
    def _iter(resp, cancel):
        for line in resp.iter_lines():
            if cancel.is_set():
                return
            if not line:
                continue
            chunk = json.loads(line)
            if chunk.get("error"):
                raise RuntimeError(chunk["error"])
            token = chunk.get("message", {}).get("content", "")
            if token:
                yield token
            if chunk.get("done"):
                return

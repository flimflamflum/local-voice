"""Streaming chat against the local Ollama HTTP API."""
import json
import threading

import requests

NUM_CTX = 4096  # small context keeps more of the model on your 4 GB GPU


class OllamaClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def list_models(self):
        r = requests.get(f"{self.base_url}/api/tags", timeout=5)
        r.raise_for_status()
        return sorted(m["name"] for m in r.json().get("models", []))

    def warm_up(self, model: str):
        """Load the model into memory so the first reply is fast."""
        try:
            requests.post(
                f"{self.base_url}/api/generate",
                # same num_ctx as chat, otherwise Ollama reloads the model on the first message
                json={"model": model, "keep_alive": "30m", "options": {"num_ctx": NUM_CTX}},
                timeout=300,
            )
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
            "options": {"temperature": temperature, "num_ctx": NUM_CTX},
        }
        with requests.post(f"{self.base_url}/api/chat", json=body, stream=True, timeout=300) as r:
            if r.status_code == 400 and "think" in r.text.lower():
                body.pop("think")  # model/older server doesn't understand the flag
                yield from self._stream(body, cancel)
                return
            r.raise_for_status()
            yield from self._iter(r, cancel)

    def _stream(self, body, cancel):
        with requests.post(f"{self.base_url}/api/chat", json=body, stream=True, timeout=300) as r:
            r.raise_for_status()
            yield from self._iter(r, cancel)

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

"""Tiny JSON-backed settings store."""
import json
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SETTINGS_PATH = ROOT / "settings.json"

DEFAULTS = {
    "model": "",               # empty = first model Ollama reports
    "voice": "kokoro:bm_george",  # or a Windows voice, e.g. "Microsoft David Desktop"
    "rate": 1,                 # SAPI rate, -10..10
    "volume": 100,             # 0..100
    "speak_replies": True,
    "hands_free": False,       # auto re-listen after Jarvis finishes talking
    "whisper_model": "base.en",
    "user_name": "",
    "assistant_name": "Jarvis",
    "temperature": 0.8,
    "ollama_url": "http://localhost:11434",
}


class Settings:
    def __init__(self):
        self._lock = threading.Lock()
        self._data = dict(DEFAULTS)
        if SETTINGS_PATH.exists():
            try:
                self._data.update(json.loads(SETTINGS_PATH.read_text(encoding="utf-8-sig")))
            except (OSError, json.JSONDecodeError):
                pass

    def get(self, key):
        with self._lock:
            return self._data.get(key, DEFAULTS.get(key))

    def all(self):
        with self._lock:
            return dict(self._data)

    def update(self, changes: dict):
        with self._lock:
            for k, v in changes.items():
                if k in DEFAULTS:
                    self._data[k] = v
            SETTINGS_PATH.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
            return dict(self._data)

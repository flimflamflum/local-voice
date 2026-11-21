"""Jarvis - voice chat with your local Ollama models.

Run:  .venv\\Scripts\\pythonw.exe app.py   (or double-click Jarvis.bat)
"""
import json
import os
import threading
import traceback
from pathlib import Path

os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")

import webview  # noqa: E402

from src.llm import OllamaClient
from src.prompt import build_system_prompt
from src.settings import Settings
from src.stt import Recorder, Transcriber
from src.textutil import SentenceChunker, ThinkFilter, clean_for_speech
from src.tts import Speaker

ROOT = Path(__file__).resolve().parent
MAX_HISTORY = 24  # messages kept (user + assistant)


class Api:
    """Methods without a leading underscore are callable from JavaScript."""

    def __init__(self):
        self._window = None
        self._settings = Settings()
        self._ollama = OllamaClient(self._settings.get("ollama_url"))
        self._history = []
        self._gen = 0  # bumped on every interrupt so stale threads bail out
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self._state = "idle"
        self._interrupted = False
        self._model_switch = None  # thread unloading old model / loading new one

        self._recorder = Recorder(on_level=lambda lvl: self._emit("level", round(lvl, 3)))
        self._transcriber = Transcriber(self._settings.get("whisper_model"))
        self._speaker = Speaker(on_start=self._on_speech_start, on_idle=self._on_speech_idle)
        s = self._settings
        self._speaker.configure(voice=s.get("voice"), rate=s.get("rate"), volume=s.get("volume"))
        threading.Thread(target=self._watch_whisper, daemon=True).start()

    # ------------------------------------------------------------------ JS API
    def init(self):
        models, err = [], None
        try:
            models = self._ollama.list_models()
        except Exception as e:  # noqa: BLE001
            err = f"Can't reach Ollama at {self._settings.get('ollama_url')} ({e.__class__.__name__}). Is it running?"
        model = self._settings.get("model")
        if models and model not in models:
            model = models[0]
            self._settings.update({"model": model})
        if model:
            threading.Thread(target=self._ollama.warm_up, args=(model,), daemon=True).start()
        voice_ids = [v for v, _ in self._speaker.voices]
        voice = self._settings.get("voice")
        if voice_ids and voice not in voice_ids:
            match = next((v for v in voice_ids if v.startswith(voice)), voice_ids[0])
            self._settings.update({"voice": match})
            self._speaker.configure(voice=match)
        return {
            "settings": self._settings.all(),
            "models": models,
            "voices": self._speaker.voices,
            "whisper_ready": self._transcriber.ready.is_set() and self._transcriber.model is not None,
            "error": err,
        }

    def toggle_listen(self):
        if self._recorder.active:
            self._recorder.stop()
            return
        self._interrupt()
        gen = self._gen
        threading.Thread(target=self._listen_turn, args=(gen,), daemon=True).start()

    def send_text(self, text):
        text = (text or "").strip()
        if not text:
            return
        self._interrupt()
        gen = self._gen
        self._emit("user", text)
        threading.Thread(target=self._reply_turn, args=(text, gen), daemon=True).start()

    def stop(self):
        self._interrupt()
        self._set_state("idle")

    def new_chat(self):
        self._interrupt()
        self._history.clear()
        self._set_state("idle")

    def set_settings(self, changes):
        old_model = self._settings.get("model")
        data = self._settings.update(changes or {})
        if {"voice", "rate", "volume"} & set(changes):
            self._speaker.configure(
                voice=changes.get("voice"), rate=changes.get("rate"), volume=changes.get("volume")
            )
        if changes.get("model") and changes["model"] != old_model:
            self._model_switch = threading.Thread(
                target=self._ollama.switch_model, args=(old_model, changes["model"]), daemon=True
            )
            self._model_switch.start()
        if "whisper_model" in changes and changes["whisper_model"] != self._transcriber.model_name:
            self._transcriber = Transcriber(changes["whisper_model"])
            self._emit("whisper", {"ready": False})
            threading.Thread(target=self._watch_whisper, daemon=True).start()
        return data

    def refresh_models(self):
        try:
            return self._ollama.list_models()
        except Exception:  # noqa: BLE001
            return []

    def test_voice(self):
        self._interrupt()
        self._speaker.say("Systems online. Voice check complete, and I sound fantastic.")
        self._speaker.end_of_reply()

    # ---------------------------------------------------------------- turns
    def _listen_turn(self, gen):
        self._set_state("listening")
        try:
            audio = self._recorder.record()
            if gen != self._gen:
                return
            if audio is None:
                self._set_state("idle")
                return
            self._set_state("transcribing")
            if not self._transcriber.ready.is_set():
                self._emit("toast", "Speech model is still loading, one sec...")
            text = self._transcriber.transcribe(audio)
            if gen != self._gen:
                return
            if not _is_real_speech(text):
                self._emit("toast", "Didn't catch that. Try again?")
                self._set_state("idle")
                return
            self._emit("user", text)
            self._reply_turn(text, gen)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self._emit("error", f"Mic/transcription error: {e}")
            self._set_state("idle")

    def _reply_turn(self, text, gen):
        with self._lock:
            if gen != self._gen:
                return
            cancel = self._cancel
        self._history.append({"role": "user", "content": text})
        self._history[:] = self._history[-MAX_HISTORY:]
        s = self._settings
        messages = [
            {"role": "system", "content": build_system_prompt(s.get("assistant_name"), s.get("user_name"))}
        ] + self._history

        self._set_state("thinking")
        if self._model_switch is not None:
            self._model_switch.join(timeout=120)  # old model out before we ask the new one
        self._emit("assistant_start", gen)
        speak = s.get("speak_replies")
        think, chunker = ThinkFilter(), SentenceChunker()
        full, spoke_any, quoted = "", False, False

        def queue_sentences(sentences):
            # When speaking, each sentence's text is shown the moment its audio
            # starts playing, so the chat text and the voice move together.
            nonlocal spoke_any
            for raw in sentences:
                if gen != self._gen:
                    return
                clean = clean_for_speech(raw)
                spoke_any = spoke_any or bool(clean)
                self._speaker.say(
                    clean,
                    on_play=lambda dur, raw=raw: self._emit("reveal", {"id": gen, "t": raw, "dur": dur}),
                )

        try:
            for token in self._ollama.stream_chat(s.get("model"), messages, cancel, s.get("temperature")):
                visible = think.feed(token)
                if not visible:
                    continue
                if not full:
                    visible = visible.lstrip()
                    if visible[:1] in ('"', "“"):  # small models like to quote their whole reply
                        visible, quoted = visible[1:].lstrip(), True
                    if not visible:
                        continue
                full += visible
                if speak:
                    queue_sentences(chunker.feed(visible))
                else:
                    self._emit("token", {"id": gen, "t": visible})
            if speak and gen == self._gen:
                tail = chunker.flush()
                if quoted and tail:
                    tail[-1] = tail[-1].rstrip().rstrip('"”')
                queue_sentences(tail)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            if gen == self._gen:
                self._emit("error", f"Ollama error: {e}")
        finally:
            reply = full.strip()
            if quoted:
                reply = reply.rstrip('"”').rstrip()
            if reply:
                self._history.append({"role": "assistant", "content": reply})
            if speak and gen == self._gen:
                # fires after the last sentence has played
                self._speaker.say("", on_play=lambda _d: self._emit("assistant_done", gen))
                self._speaker.end_of_reply()
            else:
                self._emit("assistant_done", gen)

        if gen != self._gen:
            return
        if not spoke_any:  # otherwise _on_speech_idle takes it from here
            self._set_state("idle")
            self._maybe_hands_free(gen)

    # ------------------------------------------------------------- helpers
    def _interrupt(self):
        self._emit("assistant_cut", self._gen)  # freeze the bubble at what was actually said
        with self._lock:
            self._gen += 1
            self._cancel.set()
            self._cancel = threading.Event()
        self._interrupted = True
        self._recorder.cancel()
        self._speaker.stop()

    def _on_speech_start(self):
        self._interrupted = False
        self._set_state("speaking")

    def _on_speech_idle(self):
        if self._recorder.active:
            return
        if self._state == "speaking":
            self._set_state("idle")
        if not self._interrupted:
            self._maybe_hands_free(self._gen)

    def _maybe_hands_free(self, gen):
        if self._settings.get("hands_free") and gen == self._gen and not self._recorder.active:
            threading.Thread(target=self._listen_turn, args=(gen,), daemon=True).start()

    def _watch_whisper(self):
        t = self._transcriber
        t.ready.wait()
        if t is self._transcriber:
            self._emit("whisper", {"ready": t.model is not None, "error": t.error})

    def _set_state(self, state):
        self._state = state
        self._emit("state", state)

    def _emit(self, kind, payload):
        if not self._window:
            return
        js = f"window.jarvis && window.jarvis.onEvent({json.dumps(kind)}, {json.dumps(payload)})"
        try:
            self._window.evaluate_js(js)
        except Exception:  # noqa: BLE001 - window closing
            pass


_HALLUCINATIONS = {"", "you", "thank you.", "thanks for watching!", "thank you for watching.", "bye.", "."}


def _is_real_speech(text: str) -> bool:
    return text.strip().lower() not in _HALLUCINATIONS and any(c.isalnum() for c in text)


ICON = ROOT / "ui" / "jarvis.ico"
BG_COLORREF = 0x000C0706  # #06070c as 0x00BBGGRR - same as the page background
TITLE_TEXT_COLORREF = 0x00F5ECE9  # #e9ecf5


def _style_title_bar(window):
    """Windows 11: paint the title bar the app's background colour."""
    import ctypes

    try:
        hwnd = window.native.Handle.ToInt32()
    except Exception:  # noqa: BLE001
        hwnd = ctypes.windll.user32.FindWindowW(None, window.title)
    if not hwnd:
        return
    dwm = ctypes.windll.dwmapi.DwmSetWindowAttribute
    for attr, value in (
        (20, 1),                    # DWMWA_USE_IMMERSIVE_DARK_MODE (dark buttons/menus)
        (35, BG_COLORREF),          # DWMWA_CAPTION_COLOR
        (34, BG_COLORREF),          # DWMWA_BORDER_COLOR
        (36, TITLE_TEXT_COLORREF),  # DWMWA_TEXT_COLOR
    ):
        v = ctypes.c_int(value)
        dwm(hwnd, attr, ctypes.byref(v), ctypes.sizeof(v))


def main():
    import ctypes

    # Own taskbar identity, so Windows shows our icon instead of grouping us under Python.
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Jarvis.VoiceAssistant")
    except Exception:  # noqa: BLE001
        pass
    api = Api()
    window = webview.create_window(
        "Jarvis",
        str(ROOT / "ui" / "index.html"),
        js_api=api,
        width=1120,
        height=780,
        min_size=(760, 560),
        background_color="#06070c",
    )
    api._window = window
    window.events.shown += lambda: _style_title_bar(window)
    webview.start(debug=False, icon=str(ICON))


if __name__ == "__main__":
    main()

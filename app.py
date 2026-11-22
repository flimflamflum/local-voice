"""Vesper - voice (or text) chat with your local Ollama models.

Run:  .venv\\Scripts\\pythonw.exe app.py   (or double-click Vesper.bat)
"""
import json
import os
import threading
import traceback
from pathlib import Path

os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")

import webview  # noqa: E402

from src import link
from src.llm import OllamaClient
from src import search
from src.coder import CodeAgent
from src.prompt import build_system_prompt, style_options
from src.settings import Settings
from src.stt import Recorder, Transcriber, is_real_speech
from src.textutil import ReplyLimiter, SentenceChunker, ThinkFilter, ThoughtStream, clean_for_speech
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
        self._coder = None  # Vesper Code agent for the chosen folder
        self._code_thread = None  # the Vesper Code request running now, if any
        self._asks = {}  # Vesper Code permission prompts waiting for an answer: id -> [Event, answer]

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
            "styles": style_options(),
            "whisper_ready": self._transcriber.ready.is_set() and self._transcriber.model is not None,
            "error": err,
            "code_folder": self._folder_info(),
        }

    # ------------------------------------------------------------ Vesper Code
    def choose_folder(self):
        """Folder picker for Vesper Code. Returns the folder info, or None if cancelled."""
        start = self._settings.get("code_folder") or str(Path.home())
        picked = self._window.create_file_dialog(webview.FileDialog.FOLDER, directory=start)
        if not picked:
            return None
        folder = str(Path(picked[0] if isinstance(picked, (list, tuple)) else picked).resolve())
        if folder != self._settings.get("code_folder"):
            self._interrupt()
            self._set_state("idle")
            self._settings.update({"code_folder": folder})
            self._coder = None
        return self._folder_info()

    def code_send(self, text):
        text = (text or "").strip()
        if not text:
            return
        self._interrupt()
        gen = self._gen
        self._code_thread = threading.Thread(target=self._code_turn, args=(text, gen), daemon=True)
        self._code_thread.start()

    def code_new(self):
        self._interrupt()
        if self._coder:
            self._coder.clear()
        self._set_state("idle")

    def code_undo(self, turn):
        """Undo the file changes from Vesper Code request `turn` (and any after it)."""
        if not self._coder:
            return {"error": "There's nothing to undo."}
        worker = self._code_thread
        if worker is not None and worker.is_alive():
            self._interrupt()  # don't restore files under a turn that's still editing them
            self._set_state("idle")
            worker.join(timeout=10)
        try:
            undone, files = self._coder.undo(turn)
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)}
        return {"undone": undone, "files": files}

    def code_answer(self, ask_id, answer):
        """The user's choice on a permission prompt: "allow", "always" or "deny"."""
        waiting = self._asks.get(ask_id)
        if waiting:
            waiting[1] = answer
            waiting[0].set()

    def toggle_listen(self):
        if not self._settings.get("voice_mode"):
            return
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
        if changes.get("voice_mode") is False and (self._recorder.active or self._speaker.busy):
            self._interrupt()  # switched to text: stop listening / talking right away
            self._set_state("idle")
        if "app_mode" in changes and changes["app_mode"] != self._settings.get("app_mode"):
            self._interrupt()  # switching between Chat and Code stops whatever was running
            self._set_state("idle")
        if "code_auto_edits" in changes and self._coder:
            self._coder.allow_edits = bool(changes["code_auto_edits"])
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

    def phone_link(self, host=None):
        """QR code + status for linking the Android app (Settings > Phone)."""
        cfg = link.load_config()
        addresses = link.local_addresses()
        ips = [ip for ip, _ in addresses]
        if host not in ips:
            host = ips[0] if ips else ""
        return {
            "addresses": addresses,
            "host": host,
            "port": cfg["port"],
            "qr": link.qr_svg(link.link_url(host, cfg["port"], cfg["token"])) if host else "",
            "running": link.server_running(cfg["port"]),
        }

    def start_phone_server(self):
        try:
            if not link.server_running(link.load_config()["port"]):
                link.start_server()
            return None
        except Exception as e:  # noqa: BLE001
            return f"Couldn't start the phone server: {e}"

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
            if not is_real_speech(text):
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
        model = s.get("model")
        web = bool(s.get("web_search")) and self._ollama.can_use_tools(model)
        system = build_system_prompt(s.get("assistant_name"), s.get("user_name"), s.get("style"), web)
        messages = ([{"role": "system", "content": system}] if system else []) + self._history

        self._set_state("thinking")
        if self._model_switch is not None:
            self._model_switch.join(timeout=120)  # old model out before we ask the new one
        self._emit("assistant_start", gen)
        speak = s.get("voice_mode") and s.get("speak_replies")

        def call_tool(name, args):
            if name != "web_search":
                return f"There is no tool called {name}."
            query = str(args.get("query") or "").strip()
            if gen == self._gen:
                self._emit("search", {"id": gen, "query": query})
                if self._state != "speaking":  # it may still be saying "let me check"
                    self._set_state("searching")
            result = search.search(query)
            if gen == self._gen:
                self._emit("search_done", {"id": gen, "query": query})
                if self._state == "searching":
                    self._set_state("thinking")
            return result
        # Reasoning goes to the collapsible "See thinking" panel, never into the reply.
        thoughts = ThoughtStream(
            lambda t, secs: gen == self._gen and self._emit("thought", {"id": gen, "t": t, "secs": secs})
        )
        think, chunker = ThinkFilter(thoughts.add), SentenceChunker()
        # The length cap is about how long a reply takes to say; written replies can run long.
        limiter = ReplyLimiter(text) if speak else None
        full, said, spoke_any, quoted, cut = "", "", False, False, False

        def queue_sentences(sentences):
            # When speaking, each sentence's text is shown the moment its audio
            # starts playing, so the chat text and the voice move together.
            nonlocal spoke_any, said, cut
            for raw in sentences:
                if gen != self._gen or cut:
                    return
                clean = clean_for_speech(raw)
                if speak:
                    spoke_any = spoke_any or bool(clean)
                    self._speaker.say(
                        clean,
                        on_play=lambda dur, raw=raw: self._emit("reveal", {"id": gen, "t": raw, "dur": dur}),
                    )
                else:
                    self._emit("token", {"id": gen, "t": raw})
                said += raw
                cut = bool(limiter) and limiter.done(raw, clean)  # said enough: stop at this sentence

        try:
            stream = self._ollama.stream_chat(
                model, messages, cancel, s.get("temperature"), think=s.get("think"), on_thinking=thoughts.add,
                tools=[search.TOOL] if web else None, call_tool=call_tool,
            )
            for token in stream:
                visible = think.feed(token)
                if not visible:
                    continue
                thoughts.finish()  # the answer has started
                if not full:
                    visible = visible.lstrip()
                    if visible[:1] in ('"', "“"):  # small models like to quote their whole reply
                        visible, quoted = visible[1:].lstrip(), True
                    if not visible:
                        continue
                full += visible
                queue_sentences(chunker.feed(visible))
                if cut:
                    break  # closes the stream, so Ollama stops generating too
            if gen == self._gen and not cut:
                tail = chunker.flush()
                if quoted and tail:
                    tail[-1] = tail[-1].rstrip().rstrip('"”')
                queue_sentences(tail)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            if gen == self._gen:
                self._emit("error", f"Ollama error: {e}")
        finally:
            thoughts.finish()
            if s.get("think") and not self._ollama.can_think(model) and gen == self._gen:
                self._emit("cant_think", model)
            if s.get("web_search") and not self._ollama.can_use_tools(model) and gen == self._gen:
                self._emit("cant_search", model)
            reply = said.strip()
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

    def _code_turn(self, text, gen):
        with self._lock:
            if gen != self._gen:
                return
            cancel = self._cancel
        s = self._settings
        folder = s.get("code_folder")
        if not folder or not Path(folder).is_dir():
            self._emit("error", "Choose a folder for Vesper Code first.")
            return
        if self._coder is None or str(self._coder.root) != str(Path(folder).resolve()):
            self._coder = CodeAgent(self._ollama, folder, s.get("assistant_name"))
        agent = self._coder
        agent.allow_edits = agent.allow_edits or bool(s.get("code_auto_edits"))
        model = s.get("model")
        self._set_state("thinking")
        if self._model_switch is not None:
            self._model_switch.join(timeout=120)
        self._emit("code_start", gen)
        try:
            agent.run(model, text, cancel, _CodeUI(self, gen, cancel),
                      think=s.get("think"), web=bool(s.get("web_search")), turn=gen)
            if not self._ollama.can_use_tools(model) and gen == self._gen:
                self._emit("error", f"{model} can't use tools, so it can't read or change files. Pick a model "
                                    "that supports tools, like Qwen 3.5.")
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            if gen == self._gen:
                self._emit("error", f"Vesper Code error: {e}")
        finally:
            files = agent.changed_files(gen)
            if files:  # even when stopped part-way: those edits can be undone too
                self._emit("code_checkpoint", {"id": gen, "files": files})
            self._emit("code_done", gen)
            if gen == self._gen:
                self._set_state("idle")

    def _folder_info(self):
        folder = self._settings.get("code_folder")
        if not folder or not Path(folder).is_dir():
            return None
        return {"path": folder, "name": Path(folder).name or folder}

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
        s = self._settings
        if s.get("voice_mode") and s.get("hands_free") and gen == self._gen and not self._recorder.active:
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


class _CodeUI:
    """Sends a Vesper Code turn's progress to the window; asks the user for permission."""

    def __init__(self, api, gen, cancel):
        self.api, self.gen, self.cancel = api, gen, cancel

    def _send(self, kind, payload):
        if self.gen == self.api._gen:
            self.api._emit(kind, {"id": self.gen, **payload})

    def token(self, t):
        self._send("code_text", {"t": t})

    def thought(self, t):
        self._send("code_thought", {"t": t})

    def tool_start(self, tid, name, label):
        self._send("code_tool", {"tid": tid, "name": name, "label": label})
        if self.gen == self.api._gen:
            self.api._set_state("working")

    def tool_end(self, tid, ok, summary, output, diff):
        self._send("code_tool_done", {"tid": tid, "ok": ok, "summary": summary,
                                      "output": output[-6000:], "diff": diff})
        if self.gen == self.api._gen:
            self.api._set_state("thinking")

    def note(self, text):
        self._send("code_note", {"t": text})

    def ask(self, kind, label, detail):
        ask_id = f"a{self.gen}_{len(self.api._asks)}_{id(label)}"
        waiting = [threading.Event(), "deny"]
        self.api._asks[ask_id] = waiting
        self._send("code_ask", {"ask": ask_id, "kind": kind, "label": label, "detail": detail})
        if self.gen == self.api._gen:
            self.api._set_state("waiting")
        try:
            while not waiting[0].wait(0.2):
                if self.cancel.is_set():
                    return "deny"
            return waiting[1]
        finally:
            self.api._asks.pop(ask_id, None)
            if self.gen == self.api._gen and not self.cancel.is_set():
                self.api._set_state("working")


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
        "Vesper",
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

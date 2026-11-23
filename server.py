"""Vesper phone server - lets the Android app talk to Vesper from anywhere.

The phone streams your voice here; this PC transcribes it (Whisper), asks the
model (Ollama) and voices the reply (Kokoro), then streams text + audio back.
Nothing is ever played on this PC's speakers.

Run:  run-server.bat   (or .venv\\Scripts\\python.exe server.py)

Protocol (one WebSocket per conversation, at ws://<pc>:8765/ws):
  phone -> PC  JSON  {"type": "hello", "token", "session", "settings": {...}}   must be first
               JSON  {"type": "settings", "settings": {...}}
               JSON  {"type": "audio_start"}  then BINARY 16 kHz mono PCM16 frames,
                     then {"type": "audio_end"} or {"type": "audio_cancel"}
               JSON  {"type": "text", "text"} | {"type": "interrupt"} | {"type": "new_chat"} | {"type": "ping"}
  PC -> phone  JSON  {"type": "welcome", "models", "voices", "styles", "settings"}
               JSON  {"type": "state", "state": "transcribing" | "waiting" | "thinking" | "idle"}
               JSON  {"type": "transcript", "text"}            ("" = nothing understood)
               JSON  {"type": "turn", "id"}                    reply starting
               JSON  {"type": "thought", "id", "text", "secs"}   reasoning (think on); secs is set
                     on the last one, when the answer starts
               JSON  {"type": "chunk", "id", "text", "duration", "bytes", "rate"}
                     then (if bytes > 0) BINARY 24 kHz mono PCM16 audio for that chunk
               JSON  {"type": "done", "id"} | {"type": "error", "message"} | {"type": "pong"}
               JSON  {"type": "notice", "message"}             one-off heads-up for the user
  settings: model, style, voice, rate, server_voice (false = text only, phone speaks it),
            voice_mode (false = text chat, so replies aren't cut to a speakable length),
            think (true = thinking models reason first; the reasoning comes as "thought"),
            web_search (optional; when left out, the desktop app's web search switch decides)
"""
import asyncio
import collections
import concurrent.futures
import json
import os
import secrets
import threading
import traceback

os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")

import numpy as np  # noqa: E402
import uvicorn  # noqa: E402
from fastapi import FastAPI, WebSocket, WebSocketDisconnect  # noqa: E402

from src import search  # noqa: E402
from src.llm import OllamaClient  # noqa: E402
from src.prompt import DEFAULT_STYLE, build_system_prompt, style_options  # noqa: E402
from src.settings import Settings  # noqa: E402
from src.stt import Transcriber, is_real_speech  # noqa: E402
from src.textutil import ReplyLimiter, SentenceChunker, ThinkFilter, ThoughtStream, clean_for_speech  # noqa: E402
from src.tts import OUT_RATE, Synthesizer, get_kokoro, to_pcm16  # noqa: E402

from src.link import load_config, local_addresses  # noqa: E402

IN_RATE = 16000
MAX_HISTORY = 24
MAX_UTTERANCE_BYTES = IN_RATE * 2 * 60  # 60 s of speech

CONFIG = load_config()
SETTINGS = Settings()  # desktop settings provide the defaults
OLLAMA = OllamaClient(SETTINGS.get("ollama_url"))
OLLAMA.num_ctx = int(SETTINGS.get("num_ctx"))
OLLAMA.think_effort = SETTINGS.get("think_effort")
TRANSCRIBER = Transcriber(SETTINGS.get("whisper_model"))

# One worker each: there's one CPU-bound TTS engine and one Whisper model.
_tls = threading.local()


def _init_synth():
    _tls.synth = Synthesizer()  # SAPI is COM: must live on this thread
    get_kokoro()  # warm up


SYNTH_POOL = concurrent.futures.ThreadPoolExecutor(1, initializer=_init_synth, thread_name_prefix="tts")
STT_POOL = concurrent.futures.ThreadPoolExecutor(1, thread_name_prefix="stt")

TURN_LOCK = asyncio.Lock()  # one reply at a time across all phones: there's one GPU
_model_state = {"current": SETTINGS.get("model")}
HISTORIES = collections.OrderedDict()  # session id -> chat history (survives reconnects)

app = FastAPI()


@app.get("/")
def health():
    return {"app": "jarvis", "ok": True}


def default_settings():
    return {
        "model": SETTINGS.get("model"),
        "style": SETTINGS.get("style") or DEFAULT_STYLE,
        "voice": SETTINGS.get("voice"),
        "rate": SETTINGS.get("rate"),
        "server_voice": True,
        "voice_mode": True,  # false = text chat: replies aren't trimmed to a speakable length
        "think": False,  # off unless the phone asks (older app builds can't show the reasoning)
    }


class Conversation:
    def __init__(self, ws: WebSocket, session: str, settings: dict):
        self.ws = ws
        self.loop = asyncio.get_running_loop()
        self.settings = settings
        self.history = HISTORIES.setdefault(session, [])
        HISTORIES.move_to_end(session)
        while len(HISTORIES) > 20:
            HISTORIES.popitem(last=False)
        self.turn = 0
        self.cancel = threading.Event()
        self.audio = None  # bytearray while recording
        self.send_lock = asyncio.Lock()
        self.tasks = set()
        self.warned_no_think = set()  # models we've already said can't think

    # ------------------------------------------------------------ helpers
    async def send(self, obj, binary: bytes = b""):
        async with self.send_lock:
            await self.ws.send_text(json.dumps(obj))
            if binary:
                await self.ws.send_bytes(binary)

    def interrupt(self):
        self.turn += 1
        self.cancel.set()
        self.cancel = threading.Event()

    def spawn(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    # ------------------------------------------------------------ messages
    async def handle(self, msg: dict):
        kind = msg.get("type")
        if kind == "audio_start":
            self.interrupt()  # talking over the assistant = barge in
            self.audio = bytearray()
        elif kind == "audio_end":
            audio, self.audio = self.audio, None
            if audio is not None:
                self.spawn(self.from_audio(bytes(audio), self.turn))
        elif kind == "audio_cancel":
            self.audio = None
        elif kind == "text":
            text = (msg.get("text") or "").strip()
            if text:
                self.interrupt()
                self.spawn(self.reply(text, self.turn))
        elif kind == "interrupt":
            self.interrupt()
        elif kind == "new_chat":
            self.interrupt()
            self.history.clear()
        elif kind == "settings":
            self.settings.update({k: v for k, v in (msg.get("settings") or {}).items() if v is not None})
        elif kind == "ping":
            await self.send({"type": "pong"})

    def add_audio(self, data: bytes):
        if self.audio is not None and len(self.audio) < MAX_UTTERANCE_BYTES:
            self.audio.extend(data)

    # ------------------------------------------------------------ turns
    async def from_audio(self, pcm: bytes, turn: int):
        if len(pcm) < IN_RATE * 2 * 0.3:  # under 0.3 s
            await self.send({"type": "transcript", "text": ""})
            await self.send({"type": "state", "state": "idle"})
            return
        await self.send({"type": "state", "state": "transcribing"})
        audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768
        try:
            text = await self.loop.run_in_executor(STT_POOL, TRANSCRIBER.transcribe, audio)
        except Exception as e:  # noqa: BLE001
            await self.send({"type": "error", "message": f"Transcription failed: {e}"})
            return
        if turn != self.turn:
            return
        if not is_real_speech(text):
            await self.send({"type": "transcript", "text": ""})
            await self.send({"type": "state", "state": "idle"})
            return
        await self.send({"type": "transcript", "text": text})
        await self.reply(text, turn)

    async def reply(self, text: str, turn: int):
        if TURN_LOCK.locked():
            await self.send({"type": "state", "state": "waiting"})
        async with TURN_LOCK:
            if turn != self.turn:
                return
            await self.send({"type": "state", "state": "thinking"})
            s = dict(self.settings)
            await self.ensure_model(s["model"])
            if turn != self.turn:
                return
            await self.send({"type": "turn", "id": turn})

            self.history.append({"role": "user", "content": text})
            self.history[:] = self.history[-MAX_HISTORY:]
            # The desktop's web search switch applies here too (re-read: it can change while we run),
            # unless the phone sends its own "web_search" setting.
            web = s.get("web_search")
            if web is None:
                web = Settings().get("web_search")
            s["web_search"] = web = bool(web) and OLLAMA.can_use_tools(s["model"])
            desktop = Settings()  # the desktop's context and thinking effort settings apply too
            OLLAMA.num_ctx = int(desktop.get("num_ctx"))
            OLLAMA.think_effort = desktop.get("think_effort")
            system = build_system_prompt(SETTINGS.get("assistant_name"), SETTINGS.get("user_name"), s["style"], web)
            messages = ([{"role": "system", "content": system}] if system else []) + list(self.history)

            q = asyncio.Queue()
            cancel = self.cancel
            threading.Thread(
                target=self._produce, args=(q, turn, cancel, s, messages), daemon=True, name="llm"
            ).start()

            while True:
                item = await q.get()
                if item is None:
                    break
                if turn != self.turn:
                    continue  # drain quietly; the producer stops on cancel
                if isinstance(item, Exception):
                    await self.send({"type": "error", "message": f"Ollama error: {item}"})
                    continue
                if isinstance(item, dict):  # thought / notice
                    await self.send(item)
                    continue
                raw, fut = item
                pcm, duration = b"", 0.0
                if fut is not None:
                    result = await asyncio.wrap_future(fut)
                    if result is None or turn != self.turn:
                        continue
                    pcm, duration = result
                await self.send(
                    {"type": "chunk", "id": turn, "text": raw, "duration": round(duration, 3),
                     "bytes": len(pcm), "rate": OUT_RATE},
                    pcm,
                )
            if turn == self.turn:
                await self.send({"type": "done", "id": turn})

    def _produce(self, q, turn, cancel, s, messages):
        """LLM thread: stream tokens, cut them into speakable chunks, queue synthesis."""
        put = lambda item: self.loop.call_soon_threadsafe(q.put_nowait, item)  # noqa: E731
        thoughts = ThoughtStream(
            lambda t, secs: put({"type": "thought", "id": turn, "text": t, "secs": secs})
        )
        think, chunker = ThinkFilter(thoughts.add), SentenceChunker()
        # The length cap is about how long a reply takes to say; text mode can run long.
        limiter = ReplyLimiter(messages[-1]["content"]) if s.get("voice_mode", True) else None
        full, said, quoted, cut = "", "", False, False

        def emit(raws):
            nonlocal said, cut
            for raw in raws:
                if turn != self.turn or cut:
                    return
                clean = clean_for_speech(raw)
                fut = None
                if s.get("server_voice", True) and clean:
                    fut = SYNTH_POOL.submit(self._synth, turn, clean, s.get("voice"), s.get("rate", 0))
                put((raw, fut))
                said += raw
                cut = bool(limiter) and limiter.done(raw, clean)  # said enough: stop at this sentence

        def call_tool(name, args):
            if name != "web_search":
                return f"There is no tool called {name}."
            return search.search(str(args.get("query") or ""))

        try:
            stream = OLLAMA.stream_chat(
                s["model"], messages, cancel, SETTINGS.get("temperature"),
                think=s.get("think"), on_thinking=thoughts.add,
                tools=[search.TOOL] if s.get("web_search") else None, call_tool=call_tool,
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
                emit(chunker.feed(visible))
                if cut:
                    break  # closes the stream, so Ollama stops generating too
            if not cut:
                tail = chunker.flush()
                if quoted and tail:
                    tail[-1] = tail[-1].rstrip().rstrip('"”')
                emit(tail)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            put(e)
        finally:
            thoughts.finish()
            model = s["model"]
            if s.get("think") and not OLLAMA.can_think(model) and model not in self.warned_no_think:
                self.warned_no_think.add(model)
                name = model.split("/")[-1].removesuffix(":latest")
                put({"type": "notice", "message": f"{name} can't think, so it answered straight away."})
            reply = said.strip()
            if quoted:
                reply = reply.rstrip('"”').rstrip()
            if reply:
                self.history.append({"role": "assistant", "content": reply})
            put(None)

    def _synth(self, turn, text, voice, rate):
        if turn != self.turn:
            return None  # interrupted before we got to it
        audio, duration = _tls.synth.render(text, voice or "", rate or 0)
        return to_pcm16(audio), duration

    async def ensure_model(self, model):
        if model and model != _model_state["current"]:
            old, _model_state["current"] = _model_state["current"], model
            await self.loop.run_in_executor(None, OLLAMA.switch_model, old, model)


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    peer = f"{ws.client.host}:{ws.client.port}" if ws.client else "?"
    try:
        hello = json.loads(await asyncio.wait_for(ws.receive_text(), timeout=10))
    except Exception:  # noqa: BLE001
        await ws.close(code=4000)
        return
    if hello.get("type") != "hello" or not secrets.compare_digest(str(hello.get("token", "")), CONFIG["token"]):
        print(f"[!] rejected connection from {peer} (bad token)")
        await ws.close(code=4001, reason="bad token")
        return

    settings = default_settings()
    settings.update({k: v for k, v in (hello.get("settings") or {}).items() if v not in (None, "")})
    conv = Conversation(ws, str(hello.get("session") or peer), settings)
    print(f"[+] phone connected from {peer}")

    loop = asyncio.get_running_loop()
    try:
        models = await loop.run_in_executor(None, OLLAMA.list_models)
    except Exception:  # noqa: BLE001
        models = []
    voices = await asyncio.wrap_future(SYNTH_POOL.submit(lambda: _tls.synth.voices()))
    await conv.send({"type": "welcome", "models": models, "voices": voices,
                     "styles": style_options(), "settings": settings,
                     "name": SETTINGS.get("assistant_name") or "Vesper"})
    try:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            if msg.get("bytes") is not None:
                conv.add_audio(msg["bytes"])
            elif msg.get("text") is not None:
                try:
                    await conv.handle(json.loads(msg["text"]))
                except json.JSONDecodeError:
                    pass
    except WebSocketDisconnect:
        pass
    finally:
        conv.interrupt()
        print(f"[-] phone disconnected ({peer})")


def main():
    SYNTH_POOL.submit(lambda: None)  # start the TTS thread (loads voices + Kokoro)
    port = CONFIG["port"]
    print("\n  Vesper phone server")
    print("  -------------------")
    for ip, label in local_addresses():
        print(f"  Address: {ip}:{port}   ({label})")
    print(f"  Token:   {CONFIG['token']}")
    print("\n  Easiest: open the desktop app > Settings > Phone and scan the QR code with the")
    print("  Vesper Android app. Or type the address and token into the app's settings.")
    print("  Audio from the phone is never played on this PC.\n")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning", ws_ping_interval=20, ws_ping_timeout=20)


if __name__ == "__main__":
    main()

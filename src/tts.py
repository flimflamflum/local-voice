"""Text-to-speech pipeline: synthesize sentences on one thread, play on another.

Two engines:
  * Kokoro (neural, offline, ~115 MB in models/) - voice ids look like "kokoro:bm_george"
  * Windows SAPI (David, Zira, ...)              - voice ids are the SAPI description

Synthesis of sentence N+1 overlaps playback of sentence N, and stop() cuts
everything off instantly. Each queued item carries an "epoch"; stop() bumps it so
anything already in flight is discarded.
"""
import collections
import queue
import threading
import time
from pathlib import Path

import numpy as np
import sounddevice as sd

ROOT = Path(__file__).resolve().parent.parent
KOKORO_MODEL = ROOT / "models" / "kokoro-v1.0.int8.onnx"
KOKORO_VOICES = ROOT / "models" / "voices-v1.0.bin"

# Curated Kokoro English voices: id -> label
KOKORO_VOICE_LABELS = {
    "bm_george": "George (British)",
    "bm_daniel": "Daniel (British)",
    "bm_fable": "Fable (British)",
    "bm_lewis": "Lewis (British)",
    "bf_emma": "Emma (British)",
    "bf_isabella": "Isabella (British)",
    "am_michael": "Michael (American)",
    "am_fenrir": "Fenrir (American)",
    "am_puck": "Puck (American)",
    "am_adam": "Adam (American)",
    "af_heart": "Heart (American)",
    "af_bella": "Bella (American)",
    "af_nicole": "Nicole (American, whispery)",
    "af_sky": "Sky (American)",
}

SAFT_22K_16BIT_MONO = 22
OUT_RATE = 24000        # Kokoro's native rate; SAPI audio gets resampled to it
SENTENCE_PAUSE = 0.12   # seconds of breath between sentences
OUTPUT_TAIL = 0.15      # wait after the buffer empties before closing the device


def kokoro_available() -> bool:
    return KOKORO_MODEL.exists() and KOKORO_VOICES.exists()


class _SapiEngine:
    """Renders SAPI speech to an in-memory buffer so it plays through the same pipeline."""

    def __init__(self):
        import pythoncom
        import win32com.client

        pythoncom.CoInitialize()
        self._win32 = win32com.client
        self.voice = win32com.client.Dispatch("SAPI.SpVoice")
        self.tokens = {v.GetDescription(): v for v in self.voice.GetVoices()}

    def synth(self, text, voice_id, rate):
        token = self.tokens.get(voice_id) or next(
            (t for d, t in self.tokens.items() if d.startswith(voice_id)), None
        )
        if token is not None:
            self.voice.Voice = token
        self.voice.Rate = max(-10, min(10, int(rate)))
        stream = self._win32.Dispatch("SAPI.SpMemoryStream")
        fmt = self._win32.Dispatch("SAPI.SpAudioFormat")
        fmt.Type = SAFT_22K_16BIT_MONO
        stream.Format = fmt
        self.voice.AudioOutputStream = stream
        self.voice.Speak(text)
        pcm = np.frombuffer(bytes(stream.GetData()), dtype=np.int16).astype(np.float32) / 32768
        return pcm, 22050


class _KokoroEngine:
    def __init__(self):
        from kokoro_onnx import Kokoro

        self.k = Kokoro(str(KOKORO_MODEL), str(KOKORO_VOICES))

    def synth(self, text, voice_id, rate):
        name = voice_id.split(":", 1)[1]
        speed = max(0.5, min(2.0, 1.0 + int(rate) * 0.1))
        lang = "en-gb" if name.startswith("b") else "en-us"
        audio, sr = self.k.create(text, voice=name, speed=speed, lang=lang)
        return np.asarray(audio, dtype=np.float32), sr


class Speaker:
    def __init__(self, on_start=None, on_idle=None):
        self._on_start = on_start or (lambda: None)
        self._on_idle = on_idle or (lambda: None)
        self._text_q = queue.Queue()
        self._audio_q = queue.Queue()
        self._epoch = 0
        self._lock = threading.Lock()
        self._cfg = {"voice": "", "rate": 0, "volume": 100}
        self._kokoro = None
        self._kokoro_lock = threading.Lock()
        self.kokoro_error = None
        self.busy = False
        self._buf = collections.deque()
        self._buf_pos = 0
        self._buf_lock = threading.Lock()
        self._stream = None
        self._latency = 0.1
        self._notify_q = queue.Queue()
        self.voices = []  # list of (id, label)
        self._ready = threading.Event()
        threading.Thread(target=self._synth_loop, daemon=True, name="tts-synth").start()
        threading.Thread(target=self._play_loop, daemon=True, name="tts-play").start()
        threading.Thread(target=self._notify_loop, daemon=True, name="tts-cues").start()
        self._ready.wait(10)

    # ---------------------------------------------------------------- public
    def configure(self, voice=None, rate=None, volume=None):
        with self._lock:
            if voice is not None:
                self._cfg["voice"] = voice
            if rate is not None:
                self._cfg["rate"] = int(rate)
            if volume is not None:
                self._cfg["volume"] = int(volume)
        if voice and voice.startswith("kokoro:"):
            self.preload_kokoro()

    def preload_kokoro(self):
        threading.Thread(target=self._get_kokoro, daemon=True).start()

    def say(self, text: str, on_play=None):
        """Queue a sentence. on_play(duration_s) fires the moment it starts coming out
        of the speakers. text may be empty when only the on_play cue is wanted - it
        still fires in order with the audio around it."""
        if text.strip() or on_play:
            self._text_q.put((self._epoch, text, on_play))

    def end_of_reply(self):
        self._text_q.put((self._epoch, None, None))

    def stop(self):
        with self._lock:
            self._epoch += 1
            epoch = self._epoch
        for q in (self._text_q, self._audio_q):
            while True:
                try:
                    q.get_nowait()
                except queue.Empty:
                    break
        self._clear_buffer()  # silence is instant: the callback now outputs zeros
        self._audio_q.put((epoch, None, 0, None))  # lets the player report idle

    # --------------------------------------------------------------- workers
    def _get_kokoro(self):
        with self._kokoro_lock:
            if self._kokoro is None and self.kokoro_error is None:
                try:
                    self._kokoro = _KokoroEngine()
                except Exception as e:  # noqa: BLE001
                    self.kokoro_error = str(e)
            return self._kokoro

    def _synth_loop(self):
        sapi = _SapiEngine()
        self.voices = [(d, d.replace("Microsoft ", "").split(" - ")[0].replace(" Desktop", "") + " (Windows)")
                       for d in sapi.tokens]
        if kokoro_available():
            self.voices = [(f"kokoro:{v}", f"{label} · Kokoro") for v, label in KOKORO_VOICE_LABELS.items()] + self.voices
        self._ready.set()

        while True:
            epoch, text, on_play = self._text_q.get()
            if epoch != self._epoch:
                continue
            if text is None:
                self._audio_q.put((epoch, None, 0, None))
                continue
            audio, sr = np.zeros(0, np.float32), OUT_RATE
            if text.strip():
                with self._lock:
                    cfg = dict(self._cfg)
                try:
                    engine = sapi
                    if cfg["voice"].startswith("kokoro:"):
                        engine = self._get_kokoro() or sapi
                        if engine is sapi:
                            cfg["voice"] = ""  # kokoro failed to load: default SAPI voice
                    audio, sr = engine.synth(text, cfg["voice"], cfg["rate"])
                    audio = audio * (cfg["volume"] / 100.0)
                except Exception as e:  # noqa: BLE001
                    print("TTS synth error:", e)  # still pass on_play through so the text shows
            if epoch == self._epoch:
                self._audio_q.put((epoch, audio, sr, on_play))

    # ------------------------------------------------------------ playback
    # One output stream stays open for a whole reply and is fed sentence after
    # sentence, so there's no device open/close gap between sentences.

    def _callback(self, outdata, frames, timeinfo, _status):
        out = outdata[:, 0]
        filled = 0
        # how far in the future this block actually reaches the speakers
        latency = timeinfo.outputBufferDacTime - timeinfo.currentTime
        if not 0 < latency < 1:
            latency = self._latency
        now = time.monotonic()
        with self._buf_lock:
            while self._buf and (filled < frames or len(self._buf[0][0]) == 0):
                entry = self._buf[0]  # [audio, on_play, duration, epoch]
                chunk = entry[0]
                if self._buf_pos == 0 and entry[1] is not None:
                    self._notify_q.put((entry[1], entry[2], entry[3], now + latency + filled / OUT_RATE))
                    entry[1] = None
                n = min(frames - filled, len(chunk) - self._buf_pos)
                out[filled:filled + n] = chunk[self._buf_pos:self._buf_pos + n]
                filled += n
                self._buf_pos += n
                if self._buf_pos >= len(chunk):
                    self._buf.popleft()
                    self._buf_pos = 0
        out[filled:] = 0

    def _buffered(self):
        with self._buf_lock:
            return bool(self._buf)

    def _clear_buffer(self):
        with self._buf_lock:
            self._buf.clear()
            self._buf_pos = 0

    def _open_stream(self):
        if self._stream is None:
            self._stream = sd.OutputStream(
                samplerate=OUT_RATE, channels=1, dtype="float32", callback=self._callback
            )
            self._stream.start()
            self._latency = float(self._stream.latency or 0.1)

    def _notify_loop(self):
        """Fires on_play cues at the moment their audio reaches the speakers."""
        while True:
            fn, duration, epoch, at = self._notify_q.get()
            delay = at - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            if epoch == self._epoch:
                try:
                    fn(duration)
                except Exception as e:  # noqa: BLE001
                    print("TTS cue error:", e)

    def _close_stream(self):
        if self._stream is not None:
            try:
                self._stream.close()
            except Exception:  # noqa: BLE001
                pass
            self._stream = None

    def _play_loop(self):
        while True:
            epoch, audio, sr, on_play = self._audio_q.get()
            if epoch != self._epoch:
                continue
            if audio is None:  # end of reply (or stop): let the buffer drain, then go idle
                while self._buffered() and epoch == self._epoch:
                    sd.sleep(20)
                if epoch == self._epoch:
                    sd.sleep(int(1000 * OUTPUT_TAIL))  # let the device play out its last block
                    self._close_stream()
                    if self.busy:
                        self.busy = False
                        self._on_idle()
                continue
            audible = len(audio) > 0
            audio, duration = _prepare(audio, sr) if audible else (audio, 0.0)
            try:
                self._open_stream()
            except Exception as e:  # noqa: BLE001
                print("Audio output error:", e)
                if on_play:
                    on_play(0.0)
                continue
            with self._buf_lock:
                if epoch != self._epoch:
                    continue
                self._buf.append([audio, on_play, duration, epoch])
            if audible and not self.busy:
                self.busy = True
                self._on_start()


def _prepare(audio, sr):
    """Resample to the output rate, trim the engine's built-in silence, add a natural pause.
    Returns (audio, seconds_of_actual_speech)."""
    if sr != OUT_RATE:
        n = int(len(audio) * OUT_RATE / sr)
        audio = np.interp(np.linspace(0, len(audio) - 1, n), np.arange(len(audio)), audio)
    audio = audio.astype(np.float32)
    loud = np.flatnonzero(np.abs(audio) > 0.006)
    if len(loud):
        pad = int(0.02 * OUT_RATE)  # keep 20 ms so soft consonants aren't clipped
        audio = audio[max(0, loud[0] - pad): loud[-1] + pad]
    duration = len(audio) / OUT_RATE
    return np.concatenate([audio, np.zeros(int(SENTENCE_PAUSE * OUT_RATE), np.float32)]), duration

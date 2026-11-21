"""Microphone capture with simple energy-based endpointing + faster-whisper."""
import threading
import time

import numpy as np
import sounddevice as sd

SAMPLE_RATE = 16000
BLOCK = 512  # 32 ms


class Transcriber:
    def __init__(self, model_name: str):
        self.model_name = model_name
        self.model = None
        self.ready = threading.Event()
        self.error = None
        threading.Thread(target=self._load, daemon=True, name="whisper-load").start()

    def _load(self):
        try:
            from faster_whisper import WhisperModel

            self.model = WhisperModel(self.model_name, device="cpu", compute_type="int8")
            # Warm-up pass so the first real transcription isn't slow.
            self.model.transcribe(np.zeros(SAMPLE_RATE, dtype=np.float32), language="en")
        except Exception as e:  # noqa: BLE001
            self.error = str(e)
        finally:
            self.ready.set()

    def transcribe(self, audio: np.ndarray) -> str:
        self.ready.wait()
        if self.model is None:
            raise RuntimeError(f"Whisper failed to load: {self.error}")
        segments, _ = self.model.transcribe(
            audio,
            language="en",
            beam_size=1,
            vad_filter=True,
            condition_on_previous_text=False,
        )
        return " ".join(s.text.strip() for s in segments).strip()


class Recorder:
    """Records until the user stops talking (or stop() is called).

    on_level(float 0..1) is called ~20x/sec for the UI visualizer.
    """

    def __init__(self, on_level=None):
        self.on_level = on_level or (lambda _l: None)
        self._stop = threading.Event()
        self._cancel = threading.Event()
        self.active = False

    def stop(self):
        """Finish recording and keep the audio."""
        self._stop.set()

    def cancel(self):
        """Abort recording and discard the audio."""
        self._cancel.set()
        self._stop.set()

    def record(self, silence_after=1.1, no_speech_timeout=8.0, max_seconds=60.0):
        """Blocking. Returns float32 mono audio, or None if cancelled / nothing said."""
        self._stop.clear()
        self._cancel.clear()
        self.active = True
        chunks = []
        noise_floor = None
        calib = []
        speech_started = False
        last_voice = time.monotonic()
        t0 = time.monotonic()
        last_ui = 0.0

        try:
            with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32", blocksize=BLOCK) as stream:
                while not self._stop.is_set():
                    data, _ = stream.read(BLOCK)
                    mono = data[:, 0].copy()
                    chunks.append(mono)
                    rms = float(np.sqrt(np.mean(mono**2)) + 1e-9)
                    now = time.monotonic()

                    if noise_floor is None:
                        calib.append(rms)
                        if len(calib) >= 8:  # ~250 ms of room tone
                            noise_floor = float(np.median(calib))
                    else:
                        threshold = max(0.012, noise_floor * 3.0)
                        if rms > threshold:
                            speech_started = True
                            last_voice = now
                        elif speech_started and now - last_voice > silence_after:
                            break
                        elif not speech_started:
                            # slowly track room noise while waiting
                            noise_floor = 0.95 * noise_floor + 0.05 * rms
                            if now - t0 > no_speech_timeout:
                                return None
                    if now - t0 > max_seconds:
                        break
                    if now - last_ui > 0.05:
                        last_ui = now
                        self.on_level(min(1.0, rms * 12))
        finally:
            self.active = False
            self.on_level(0.0)

        if self._cancel.is_set() or not chunks:
            return None
        audio = np.concatenate(chunks)
        if len(audio) < SAMPLE_RATE * 0.3:
            return None
        return audio

# local-voice

**Jarvis** is a fully offline voice assistant for Windows that lets you talk to your local [Ollama](https://ollama.com) models. You speak, Whisper transcribes, your model answers, and a Kokoro neural voice reads the reply back while the text types out in sync with the speech.

Nothing leaves your machine.

## Requirements
- Windows 10/11
- Python 3.10+
- [Ollama](https://ollama.com) running, with at least one model pulled (e.g. `ollama pull llama3.2:3b`)
- A microphone

## Setup
```powershell
git clone https://github.com/flimflamflum/local-voice.git
cd local-voice
powershell -ExecutionPolicy Bypass -File setup.ps1 -Shortcut
```
The script:
- creates a `.venv`
- installs the dependencies
- downloads the Kokoro voice model (about 115 MB, into `models/`)
- creates a desktop shortcut, if you pass `-Shortcut`

The Whisper speech model (about 145 MB) downloads automatically the first time you run Jarvis.

## Run
Double-click **`Jarvis.bat`** or the desktop shortcut.

## Controls
| | |
|---|---|
| `Space` / mic button | Start talking. It stops by itself when you pause, or press Space again |
| `Space` while it's talking | Interrupt and talk |
| `Esc` / stop button | Shut it up |
| `Ctrl+N` | New chat (clears memory) |

The settings (gear icon) cover:
- the model
- the voice (14 Kokoro voices plus the Windows ones), speed and volume
- your name
- the personality: Playful, Chill, Professional, Sarcastic, Butler, Hype or Pirate
- hands-free mode, which keeps listening after each reply
- the Whisper model size

Each personality is a short tone prompt placed at the start of the system prompt. Add your own in `STYLES` in `src/prompt.py`.

## How it works
| File | What it does |
|---|---|
| `src/stt.py` | Captures the mic, stops on silence, and runs **faster-whisper** on the CPU so the GPU stays free for the LLM |
| `src/llm.py` | Streams from Ollama's `/api/chat` with thinking off. When you switch models, it unloads the old one first |
| `src/textutil.py` | Splits the stream into speakable sentences or clauses, strips markdown and emoji for speech |
| `src/tts.py` | **Kokoro** (neural, ONNX) or Windows SAPI voices. Synthesis runs ahead of playback, and everything plays through one continuous audio stream so there are no gaps between sentences |
| `src/prompt.py` | The personality and voice-friendly rules. Edit it to change how Jarvis talks |
| `ui/` | The interface (HTML/CSS/JS in a pywebview window) |

## Model tips
On a 4 GB GPU, models around 2–3B parameters fit entirely in VRAM and respond much faster than 7–8B models that spill over to the CPU.

## Credits
- [kokoro-onnx](https://github.com/thewh1teagle/kokoro-onnx) for the Kokoro voice model
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) for speech recognition
- [pywebview](https://pywebview.flowrl.com/) for the app window

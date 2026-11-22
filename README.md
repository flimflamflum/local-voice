# local-voice

**Vesper** is a fully offline voice assistant for Windows that lets you talk to your local [Ollama](https://ollama.com) models. You speak, Whisper transcribes, your model answers, and a Kokoro neural voice reads the reply back while the text types out in sync with the speech.

Nothing leaves your machine, unless you switch on **web search**.

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

The Whisper speech model (about 145 MB) downloads automatically the first time you run Vesper.

## Run
Double-click **`Vesper.bat`** or the desktop shortcut.

(If you set the app up back when it was called Jarvis, run `setup.ps1 -Shortcut` again to swap the old desktop shortcut for a Vesper one.)

## Controls
| | |
|---|---|
| `Space` / mic button | Start talking. It stops by itself when you pause, or press Space again |
| `Space` while it's talking | Interrupt and talk |
| `Esc` / stop button | Shut it up |
| `Ctrl+N` | New chat (clears memory) |
| `Ctrl+Shift+V` / **Voice** switch (top left) | Voice mode on/off. Off turns the app into a plain text chat: the orb and mic disappear and nothing is spoken |
| `Ctrl+Shift+W` / **Web** switch (top left) | Web search on/off. On lets the model look things up (news, weather, scores, prices, anything recent) before it answers. Off by default |

The settings (gear icon) cover:
- the model
- the voice (14 Kokoro voices plus the Windows ones), speed and volume
- your name
- the personality: Normal, Playful, Chill, Professional, Sarcastic, Butler, Hype or Pirate
- hands-free mode, which keeps listening after each reply
- the Whisper model size

Each personality is a short tone prompt placed at the start of the system prompt. **Normal** sends no system prompt at all, so you get the model's raw behaviour. Add your own in `STYLES` in `src/prompt.py`.

## How it works
| File | What it does |
|---|---|
| `src/stt.py` | Captures the mic, stops on silence, and runs **faster-whisper** on the CPU so the GPU stays free for the LLM |
| `src/llm.py` | Streams from Ollama's `/api/chat`. Thinking is off unless you turn on **Think before answering** in Settings; the reasoning then appears under a collapsed **See thinking** tab above the reply and is never spoken. When you switch models, it unloads the old one first. With web search on, it offers the model a `web_search` tool, runs the search when the model calls it, and hands back the results |
| `src/search.py` | Web search via the `ddgs` package (DuckDuckGo, Bing and Brave, no API key), plus the most relevant lines from the top two pages. Weather questions get live numbers from [wttr.in](https://wttr.in) |
| `src/textutil.py` | Splits the stream into speakable sentences or clauses, strips markdown and emoji for speech |
| `src/tts.py` | **Kokoro** (neural, ONNX) or Windows SAPI voices. Synthesis runs ahead of playback, and everything plays through one continuous audio stream so there are no gaps between sentences |
| `src/prompt.py` | The personality and voice-friendly rules, plus the rules for when to search the web. Edit it to change how Vesper talks |
| `ui/` | The interface (HTML/CSS/JS in a pywebview window) |
| `server.py` | The phone server. It does the same speech-to-text, Ollama and Kokoro work, then streams the text and audio to the Android app. It never plays sound on the PC |
| `android/` | The Android app (Kotlin + Jetpack Compose) |

## Talk to Vesper from your phone (Android)
Your PC does all the work: Whisper, the LLM and the Kokoro voice. The phone only records your voice and plays back the reply. The app is about 2 MB and needs no models.

**1. Build the app.** Open the `android/` folder in Android Studio, let Gradle sync, then use **Run** (phone plugged in with USB debugging on) or **Build → Generate App Bundles or APKs → Build APK(s)**.

**2. Link the phone.** In the desktop app, open **Settings → Phone**:
- Click **Start phone server** if it isn't running yet. It runs in the background and logs to `server.log`. Instead, you can run `.\run-server.bat` to see it in a console window.
- The first time, Windows Firewall asks whether to allow Python. Allow it on **private** networks.
- Pick the address to put in the QR code:
  - **At home:** your Wi-Fi address, e.g. `192.168.1.20`.
  - **From anywhere:** install [Tailscale](https://tailscale.com/) (free) on both the PC and the phone, sign in with the same account, and pick the PC's `100.x.y.z` address. Your PC isn't exposed to the internet, and Tailscale encrypts the connection.

Then, in the Android app, tap **Settings → Scan QR code** and point the phone at the screen. That's it. Scanning with the phone's normal camera app works too. You can also type the address and token by hand: the token is in `server_config.json`.

**In the app**
- Tap the orb to talk. It stops listening when you go quiet, or tap again to send right away.
- Tap the orb while Vesper is talking to interrupt.
- You can type instead.
- The **Voice** switch in the top left turns voice mode off: the orb goes away, the chat fills the screen, and replies arrive as text only (no audio is sent). The send button becomes a stop button while a reply is coming.
- Settings cover:
  - model, personality, voice, speed
  - **Hands-free**: keeps listening after each reply
  - **PC voice**: turn it off to have the PC send only text, which the phone reads with its own built-in voice (uses less data)
- Each phone keeps its own chat history on the PC, so it survives reconnects. **+** starts a new chat.

## Model tips
On a 4 GB GPU, models around 2–3B parameters fit entirely in VRAM and respond much faster than 7–8B models that spill over to the CPU.

## Credits
- [kokoro-onnx](https://github.com/thewh1teagle/kokoro-onnx) for the Kokoro voice model
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) for speech recognition
- [pywebview](https://pywebview.flowrl.com/) for the app window

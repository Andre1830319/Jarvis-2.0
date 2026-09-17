# Desktop wake word → Jarvis-style welcome

Python script that listens to your default microphone and runs a welcome flow (YouTube, Opera GX windows, ElevenLabs voice, Cursor) when it hears you say the wake word ("**jarvis**" by default). Say "jarvis" and then a question — about anything, in the same breath or after a short pause — and it's answered out loud using Groq's free API. It can also open or close apps on your PC when you ask it to (e.g. *"jarvis, abre o spotify"*, *"jarvis, fecha o chrome"*), Windows only for now. It remembers the last few exchanges, so you can keep talking without repeating "jarvis" each time; a new conversation starts on its own after a couple of minutes of silence. See constants at the top of `jarvis.py` for behavior and tuning.

## Setup

From this project directory:

```bash
python -m pip install -r requirements.txt
```

## Environment variables

The script loads a **`.env` file** in the same folder as `jarvis.py` (via `python-dotenv`). You can also set variables in the shell.

### Required (ElevenLabs welcome line)

| Variable | Purpose |
| -------- | ------- |
| `ELEVENLABS_API_KEY` | API key from [ElevenLabs](https://elevenlabs.io). |
| `ELEVENLABS_VOICE_ID` | Voice ID from the ElevenLabs app (My Voices / library). |

Without these, the welcome speech is skipped (other actions may still run).

### Required (spoken Q&A: "jarvis, <question>")

| Variable | Purpose |
| -------- | ------- |
| `GROQ_API_KEY` | Free API key from [console.groq.com](https://console.groq.com/keys) — no credit card needed, just sign up with email/Google. |

Without this, bare "jarvis" still runs the welcome flow, but "jarvis, \<question\>" logs a warning and stays silent. The free tier is rate-limited (30 requests/min, 14,400/day) — plenty for personal use.

### Optional

| Variable | Purpose |
| -------- | ------- |
| `GROQ_MODEL` | Model used to answer questions (default in code: `openai/gpt-oss-20b`). |
| `ELEVENLABS_MODEL_ID` | TTS model (default in code: `eleven_multilingual_v2`). |
| `ELEVENLABS_OUTPUT_FORMAT` | e.g. `pcm_24000` (must match playback expectations). |
| `ELEVENLABS_PCM_SAMPLE_RATE` | Override PCM sample rate if it differs from the format name. |
| `JARVIS_WELCOME_CACHE_DIR` | Custom folder for cached welcome WAV (default: `.cache/jarvis_welcome/` under the project). |
| `JARVIS_INPUT_DEVICE` | Optional mic override: **integer** index or **substring** of the device name. If unset, the script uses the Windows default; when that mic is silent, it auto-picks the loudest working input. List devices: `python -c "import sounddevice as sd; print(sd.query_devices())"`. |
| `CLAUDE_CODE_URL` | URL opened for Claude in Opera GX (default: new chat). |
| `BROWSER_NEW_WINDOW_WAIT_S` | Seconds to wait for a new Opera GX window on Windows (default `25`). |
| `BROWSER_WINDOW_WIDTH` / `BROWSER_WINDOW_HEIGHT` | Windowed Opera GX size when not fullscreen. |

Example `.env`:

```env
ELEVENLABS_API_KEY=your_key_here
ELEVENLABS_VOICE_ID=your_voice_id_here
GROQ_API_KEY=your_key_here
```

## Run

```bash
python jarvis.py
```

Allow the microphone if Windows prompts you. Stop with **Ctrl+C**.

## Tuning

Edit the constants at the top of `jarvis.py`:

| Constant               | Effect                                                                       |
| ---------------------- | ----------------------------------------------------------------------------|
| `WAKE_WORD`             | Word to listen for (matched case/accent-insensitively).                    |
| `WAKE_LANGUAGE`         | Language code passed to the speech recognizer (e.g. `pt-BR`).              |
| `WAKE_MIN_RMS`          | Audio level above which a block counts as speech; raise in noisy rooms.    |
| `WAKE_SILENCE_HANG_S`   | Seconds of quiet after speech before the utterance is sent for recognition.|
| `WAKE_MIN_UTTERANCE_S`  | Utterances shorter than this are discarded (avoids noise blips).           |
| `WAKE_MAX_UTTERANCE_S`  | Hard cap on utterance length before it is cut and sent anyway (raised so longer questions aren't clipped). |
| `WAKE_COOLDOWN_S`       | Minimum time between recognition attempts.                                 |
| `JARVIS_FOLLOWUP_WINDOW_S` | Seconds after a bare "jarvis" — or after each answer — during which the next thing you say is treated as a question, no need to repeat the wake word. |
| `JARVIS_ASSISTANT_HISTORY_TURNS` | How many recent Q&A exchanges are kept as context for follow-ups. |
| `JARVIS_ASSISTANT_HISTORY_IDLE_RESET_S` | Seconds of silence after which the conversation history resets. |
| `JARVIS_APP_CONTROL_ENABLED` | Turn voice-controlled app open/close on or off. |
| `APP_ALIASES`           | Friendly name → `.exe` mapping for apps that don't already register under Windows' App Paths. Add your own here. |
| `BLOCK_MS`              | Larger = slightly less CPU, a bit less precise timing.                     |
| `SAMPLE_RATE`           | Try `48000` if your device does not like `44100`.                          |

## Troubleshooting

- **Wrong or quiet mic:** On startup the script probes your default Windows input. If it is silent, it **auto-selects** the loudest working mic. To force a specific device, set `JARVIS_INPUT_DEVICE` in `.env` (index or name substring from `sounddevice.query_devices()`).
- **PortAudio / audio errors:** Update audio drivers or try another `SAMPLE_RATE`.
- **No reaction to "jarvis":** Lower `WAKE_MIN_RMS` slightly, speak closer to the mic, or check the terminal log for `Heard: '...'` to see what was actually transcribed.
- **Triggers on other words:** Raise `WAKE_MIN_RMS` (cuts out background noise) or check `WAKE_LANGUAGE` matches the language you're speaking.
- **"Utterance not understood" a lot:** Needs an internet connection (uses Google's free speech recognition); check connectivity.
- **No welcome speech:** Set `ELEVENLABS_API_KEY` and `ELEVENLABS_VOICE_ID` in `.env` and restart the terminal so variables load.
- **"jarvis, \<question\>" stays silent:** Set `GROQ_API_KEY` in `.env`. Check the terminal log for `Assistant request failed: ...` (bad key, rate limit, network) — the transcribed question still needs `ELEVENLABS_API_KEY`/`ELEVENLABS_VOICE_ID` to be spoken back.
- **Song/Claude open in the wrong browser:** Both require Opera GX to be installed (default per-user path: `%LOCALAPPDATA%\Programs\Opera GX\opera.exe`). If it's not found, the terminal logs `Opera GX not found; opening ... in default browser.` and falls back to your OS default instead.
- **"jarvis, abre/fecha o \<app\>" doesn't work:** Only implemented on Windows. `open_app` tries the name as-is (works for anything registered under Windows' App Paths — most installed apps), then falls back to a PATH lookup; Discord and WhatsApp (installed per-user under `%LOCALAPPDATA%`, not registered like most apps) are special-cased to find the real `.exe`. If it still can't find a specific app, add its `.exe` name to `APP_ALIASES` in `jarvis.py`. `close_app` uses `taskkill /IM <name>.exe` — check the terminal log line `Tool open_app(...)`/`Tool close_app(...)` to see exactly what was attempted and whether it actually succeeded.

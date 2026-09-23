# Desktop wake word → Jarvis assistant + visual HUD

Python script that listens to your default microphone and, when it hears you say the wake word ("**jarvis**" by default), answers questions out loud using Groq's free API, can open/close apps on your PC ("*jarvis, abre o spotify*"), and can read your recent inbox emails out loud ("*jarvis, lê meus emails*"), skipping spam/newsletters. It remembers the last few exchanges, so you can keep talking without repeating "jarvis" each time.

All of this runs with **no terminal window**: `jarvis.py` drives a small local WebSocket link to `jarvis_orb.html` — an animated holographic orb that opens in your browser and shows live status (listening/thinking/speaking) plus what was heard and answered. Logs go to `jarvis.log` instead of a console. See constants at the top of `jarvis.py` for behavior and tuning.

## Setup

From this project directory:

```bash
python -m pip install -r requirements.txt
```

## Environment variables

The script loads a **`.env` file** in the same folder as `jarvis.py` (via `python-dotenv`). You can also set variables in the shell.

### TTS (spoken welcome line + spoken answers)

By default Jarvis speaks using **edge-tts** — Microsoft Edge's free online voice service. It needs no API key and no quota, only an internet connection. Nothing to configure to get started with it.

If you'd rather use ElevenLabs (higher voice quality, voice cloning), set these and set `JARVIS_TTS_ENGINE=elevenlabs` in `.env`:

| Variable | Purpose |
| -------- | ------- |
| `ELEVENLABS_API_KEY` | API key from [ElevenLabs](https://elevenlabs.io). |
| `ELEVENLABS_VOICE_ID` | Voice ID from the ElevenLabs app (My Voices / library). |

Whichever engine you don't pick as primary is still tried automatically as a **fallback** if the first one fails (edge-tts has no internet reach, ElevenLabs quota exceeded/misconfigured, etc.) — so as long as at least one of the two works, Jarvis still speaks. Without either configured, the welcome/spoken-answer lines are skipped (other actions may still run).

### Required (spoken Q&A: "jarvis, <question>")

| Variable | Purpose |
| -------- | ------- |
| `GROQ_API_KEY` | Free API key from [console.groq.com](https://console.groq.com/keys) — no credit card needed, just sign up with email/Google. |

Without this, bare "jarvis" still runs the welcome flow, but "jarvis, \<question\>" logs a warning and stays silent. The free tier is rate-limited (30 requests/min, 14,400/day) — plenty for personal use.

### Required (reading emails: "jarvis, lê meus emails")

| Variable | Purpose |
| -------- | ------- |
| `EMAIL_ADDRESS` | Your email address (e.g. Gmail). |
| `EMAIL_APP_PASSWORD` | An **app password** — not your normal login password. For Gmail: enable 2-Step Verification, then create one at [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords). |

Without these, "jarvis, lê meus emails" answers that it can't reach the inbox. Only reads INBOX via IMAP (read-only) — Gmail's own Spam folder is never touched — and additionally skips anything that looks like a newsletter/marketing email (presence of a `List-Unsubscribe` header, `Precedence: bulk/list/junk`, etc.), even if it landed in INBOX.

### Optional

| Variable | Purpose |
| -------- | ------- |
| `JARVIS_TTS_ENGINE` | Which TTS engine to try first: `edge` (default, free, no key) or `elevenlabs`. The other is still tried automatically as a fallback. |
| `EDGE_TTS_VOICE` | Voice for edge-tts (default `pt-BR-AntonioNeural`; e.g. `pt-BR-FranciscaNeural` for a female voice). Full list: run `edge-tts --list-voices`. |
| `GROQ_MODEL` | Model used to answer questions (default in code: `openai/gpt-oss-20b`). |
| `EMAIL_IMAP_HOST` | IMAP server (default: `imap.gmail.com`). Change if not using Gmail. |
| `EMAIL_IMAP_PORT` | IMAP-over-SSL port (default: `993`). |
| `JARVIS_HUD_PORT` | Local WebSocket port the orb connects to (default `8765`). Change if that port is taken. |
| `JARVIS_WAKE_MIN_RMS` | Overrides `WAKE_MIN_RMS` (mic sensitivity) without editing the code — see Troubleshooting below for how to calibrate it from `jarvis.log`. |
| `JARVIS_WAKE_SILENCE_HANG_S` | Overrides `WAKE_SILENCE_HANG_S` (default `1.1`) — how long a pause has to be before it's treated as "done talking". Raise it if longer sentences keep getting cut off mid-way; lower it for snappier responses to short commands. |
| `ELEVENLABS_MODEL_ID` | TTS model (default in code: `eleven_multilingual_v2`). |
| `ELEVENLABS_OUTPUT_FORMAT` | e.g. `pcm_24000` (must match playback expectations). |
| `ELEVENLABS_PCM_SAMPLE_RATE` | Override PCM sample rate if it differs from the format name. |
| `JARVIS_WELCOME_CACHE_DIR` | Custom folder for cached speech audio (default: `.cache/jarvis_welcome/` under the project). |
| `JARVIS_INPUT_DEVICE` | Optional mic override: **integer** index or **substring** of the device name. If unset, the script uses the Windows default; when that mic is silent, it auto-picks the loudest working input. List devices: `python -c "import sounddevice as sd; print(sd.query_devices())"`. |
| `CLAUDE_CODE_URL` | URL opened for Claude in Opera GX (default: new chat). |
| `BROWSER_NEW_WINDOW_WAIT_S` | Seconds to wait for a new Opera GX window on Windows (default `25`). |
| `BROWSER_WINDOW_WIDTH` / `BROWSER_WINDOW_HEIGHT` | Windowed Opera GX size when not fullscreen. |

Example `.env`:

```env
GROQ_API_KEY=your_key_here
EMAIL_ADDRESS=you@gmail.com
EMAIL_APP_PASSWORD=your_app_password_here
```

(No ElevenLabs keys needed — edge-tts, the default TTS engine, works with no configuration at all. Add `ELEVENLABS_API_KEY`/`ELEVENLABS_VOICE_ID`/`JARVIS_TTS_ENGINE=elevenlabs` only if you want to switch to ElevenLabs.)

## Run

Three ways to start it, from the `jarvis-main` folder:

| Way | Shows a terminal? | When to use |
| --- | --- | --- |
| `python jarvis.py` | Yes | Debugging — see raw log lines as they happen. |
| Double-click **`Jarvis.bat`** | Yes (stays open, `pause` at the end) | Quick manual start with a visible log. |
| Double-click **`Jarvis.vbs`** | **No** | Everyday use — silent start, same as what auto-start (below) uses. |

However you start it, `jarvis_orb.html` opens automatically in your default browser — that's your HUD from now on. Say "jarvis" near the mic (headphones/earphones reduce false triggers from any audio played by the assistant itself). Logs always go to **`jarvis.log`** in this folder, terminal or not.

## Visual HUD (jarvis_orb.html)

The orb is a passive display: it connects to `jarvis.py` over `ws://127.0.0.1:8765` (change the port with `JARVIS_HUD_PORT`) and shows:
- **Status** — EM ESPERA (idle) / PROCESSANDO (thinking, orb shifts amber) / RESPONDENDO (speaking, orb brightens).
- **"Você: ..."** — the last thing understood from your voice.
- The assistant's last spoken answer.

If `jarvis.py` isn't running (or hasn't started yet), the orb dims and shows "DESCONECTADO" — it retries the connection every 2 seconds on its own, so just start (or restart) `jarvis.py` and it reconnects automatically. You never need to manually refresh or reopen it.

Set `OPEN_ORB_VISUAL_ON_START = False` in `jarvis.py` if you don't want the browser tab to open automatically (e.g. you keep one pinned tab open permanently instead).

## Start automatically on login (no visible window)

1. Confirm `Jarvis.vbs` works: double-click it — `jarvis_orb.html` should open and `jarvis.log` should start filling up, with **no terminal window** appearing.
2. In File Explorer, right-click **`Jarvis.vbs`** → **Show more options** (Windows 11) → **Create shortcut**. This adds a `Jarvis.vbs - Shortcut` file in the same folder.
3. Press **Win+R**, type `shell:startup`, press Enter — this opens your personal Startup folder.
4. Move (cut/paste) the shortcut from step 2 into that Startup folder.

That's it — from your next login onward, Jarvis starts silently in the background, no terminal, and the orb opens on its own. To stop it from auto-starting, just delete the shortcut from the Startup folder (the original `Jarvis.vbs` and the rest of the project stay untouched).

**Important:** create a *shortcut* to `Jarvis.vbs` and put that in Startup — don't move or copy `Jarvis.vbs` itself out of the `jarvis-main` folder, since it locates `jarvis.py` relative to its own location.

To stop a silently-running instance, use Task Manager (Details tab → find `pythonw.exe` → End task), since there's no terminal window with a Ctrl+C to press.

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
| `JARVIS_HUD_ENABLED`    | Turn the local WebSocket link to the orb on or off entirely.               |
| `JARVIS_HUD_PORT`       | Port for the local HUD WebSocket (default `8765`).                         |
| `OPEN_ORB_VISUAL_ON_START` | Whether to auto-open `jarvis_orb.html` in the browser on startup.       |
| `BLOCK_MS`              | Larger = slightly less CPU, a bit less precise timing.                     |
| `SAMPLE_RATE`           | Try `48000` if your device does not like `44100`.                          |

## Troubleshooting

- **Wrong or quiet mic:** On startup the script probes your default Windows input. If it is silent, it **auto-selects** the loudest working mic. To force a specific device, set `JARVIS_INPUT_DEVICE` in `.env` (index or name substring from `sounddevice.query_devices()`).
- **PortAudio / audio errors:** Update audio drivers or try another `SAMPLE_RATE`.
- **No reaction to "jarvis":** Every 3 seconds, `jarvis.log` now logs a line like `mic level check: peak rms in last 3s = 0.00812 (WAKE_MIN_RMS = 0.01500) — stayed below threshold`. Talk right after a check and see the next one: if your voice's peak still stays below `WAKE_MIN_RMS`, either speak closer to the mic or lower sensitivity by adding `JARVIS_WAKE_MIN_RMS=0.008` (try progressively lower values) to `.env` — no code edit needed. If the peak clearly crosses the threshold but nothing still happens, check for `Heard: '...'` right after to see what was actually transcribed.
- **Triggers on other words:** Raise `WAKE_MIN_RMS` (cuts out background noise) or check `WAKE_LANGUAGE` matches the language you're speaking.
- **"Utterance not understood" a lot:** Needs an internet connection (uses Google's free speech recognition); check connectivity.
- **No welcome speech:** With the default edge-tts engine this needs internet only — check `jarvis.log` for `edge-tts request failed: ...` (network/DNS issue) or `Could not play audio file ...` (missing the `playsound` dependency — rerun `pip install -r requirements.txt`). If you switched to ElevenLabs (`JARVIS_TTS_ENGINE=elevenlabs`), set `ELEVENLABS_API_KEY` and `ELEVENLABS_VOICE_ID` in `.env` and restart the script so variables load; also check for `quota_exceeded` in the log.
- **"jarvis, \<question\>" stays silent:** Set `GROQ_API_KEY` in `.env`. Check `jarvis.log` for `Assistant request failed: ...` (bad key, rate limit, wrong/decommissioned `GROQ_MODEL`, network) — since a recent fix, a failed assistant call is now also spoken/shown on the HUD as an apology instead of staying silent, so you should see/hear something either way.
- **Song/Claude open in the wrong browser:** Both require Opera GX to be installed (default per-user path: `%LOCALAPPDATA%\Programs\Opera GX\opera.exe`). If it's not found, `jarvis.log` logs `Opera GX not found; opening ... in default browser.` and falls back to your OS default instead.
- **"jarvis, abre/fecha o \<app\>" doesn't work:** Only implemented on Windows. `open_app` tries the name as-is (works for anything registered under Windows' App Paths — most installed apps), then falls back to a PATH lookup; Discord and WhatsApp (installed per-user under `%LOCALAPPDATA%`, not registered like most apps) are special-cased to find the real `.exe`. If it still can't find a specific app, add its `.exe` name to `APP_ALIASES` in `jarvis.py`. `close_app` uses `taskkill /IM <name>.exe` — check `jarvis.log` for the line `Tool open_app(...)`/`Tool close_app(...)` to see exactly what was attempted and whether it actually succeeded.
- **"jarvis, lê meus emails" says it can't reach the inbox:** Set `EMAIL_ADDRESS` and `EMAIL_APP_PASSWORD` in `.env`. For Gmail, `EMAIL_APP_PASSWORD` must be an **app password** (myaccount.google.com/apppasswords, requires 2-Step Verification) — your normal Google login password will not work over IMAP.
- **Email login fails / "confira EMAIL_ADDRESS e EMAIL_APP_PASSWORD":** Double-check the app password was copied without spaces, that IMAP is enabled on the account (Gmail: Settings → Forwarding and POP/IMAP → Enable IMAP), and — if not using Gmail — that `EMAIL_IMAP_HOST`/`EMAIL_IMAP_PORT` match your provider.
- **A relevant email got skipped as "spam":** The bulk-mail heuristic looks at the `List-Unsubscribe`/`Precedence`/`Auto-Submitted` headers, so a handful of legitimate senders (some transactional receipts, automated notifications) can get filtered too. Check `jarvis.log` for the line `Tool read_emails(...)` to see how many candidates were scanned; adjust `_email_looks_like_bulk()` in `jarvis.py` if it's too aggressive for your inbox.
- **Orb shows "DESCONECTADO":** `jarvis.py` isn't running, hasn't finished starting yet, or crashed — check `jarvis.log`. The page retries every 2 seconds on its own; no need to refresh once `jarvis.py` is back up.
- **Orb never opens / opens in the wrong browser:** `OPEN_ORB_VISUAL_ON_START` uses your OS default browser (`webbrowser.open`). You can always open `jarvis_orb.html` manually by double-clicking it — it will connect to `jarvis.py` as long as that's running.
- **Two orb tabs open (one stuck offline):** Harmless — every connected tab gets the same live updates. Close the extra one.
- **Nothing happens and no window appears (`Jarvis.vbs`):** Check `jarvis.log` for errors (e.g. `pythonw.exe` not on PATH — reinstall Python with "Add to PATH" checked, or edit `Jarvis.vbs` to use the full path to `pythonw.exe`). Use `Jarvis.bat` instead to see errors live in a terminal while debugging.
- **Want to stop a silently-running instance:** Task Manager → Details tab → find `pythonw.exe` → End task (there's no console window to Ctrl+C).

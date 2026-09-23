#!/usr/bin/env python3
"""
Desktop wake-word listener: reads the default microphone, segments speech using a
simple energy-based VAD, and runs the welcome flow when it hears the wake word
("jarvis" by default) in the transcribed utterance.

Run:
  python -m pip install -r requirements.txt
  python jarvis.py

Tuning (constants below):
  SAMPLE_RATE        — usually 44100 or 48000; match your device if needed.
  BLOCK_MS           — analysis window size; smaller = snappier, noisier.
  WAKE_WORD          — word to listen for (matched case/accent-insensitively).
  WAKE_LANGUAGE      — BCP-47 language code passed to the speech recognizer.
  WAKE_MIN_RMS       — audio level above which a block counts as "speech" (float audio ~ [-1, 1]).
  WAKE_SILENCE_HANG_S — seconds of quiet after speech before the utterance is considered finished.
  WAKE_MIN_UTTERANCE_S — utterances shorter than this are discarded (avoids noise blips).
  WAKE_MAX_UTTERANCE_S — hard cap on utterance length before it is cut and sent anyway.
  WAKE_COOLDOWN_S    — minimum seconds between recognition attempts (debounce).
  SONG_URI      — YouTube URL to open on each wake word (empty = log only).
  FOCUS_EXISTING_CURSOR_ON_WAKE_WORD — if True, launch Cursor without -n (reuse / focus existing instance).
  OPEN_NEW_CURSOR_ON_WAKE_WORD — if True, also launch Cursor with -n (extra new window; runs after focus launch if both).
  CURSOR_OPEN_FULLSCREEN — Windows: after focus/launch, send F11 to enter Cursor/VS Code-style fullscreen (toggle off with F11).
  OPEN_CLAUDE_IN_BROWSER — Claude in Opera GX after the song (CLAUDE_CODE_URL).
  CLAUDE_BROWSER_MONITOR — 1-based display index (Windows: sorted left-to-top).
  BROWSER_SEPARATE_SITE_PROFILES — Windows: if True, uses temp --user-data-dir per site (not your normal profile).
    Default False so Claude uses your usual Opera GX profile and logins; enable only if the window keeps
    opening on the wrong monitor and you accept a separate profile for automation.
  OPEN_BROWSER_FULLSCREEN — Fullscreen on the chosen monitor (Windows: new window is detected and snapped with SetWindowPos).
    Default False.
  JARVIS_WELCOME_* — TTS after the song. Configure via environment or a `.env`
    file next to this script. Default engine is edge-tts (Microsoft Edge's free
    online voice service, no API key needed); set JARVIS_TTS_ENGINE=elevenlabs
    to use ElevenLabs instead (ELEVENLABS_API_KEY, ELEVENLABS_VOICE_ID, etc.),
    or leave the default and Jarvis still falls back to ElevenLabs automatically
    if edge-tts fails and an ElevenLabs key/voice are configured (and vice versa).
    With JARVIS_WELCOME_CACHE_ENABLED, audio is saved under `.cache/jarvis_welcome/`
    and replayed when phrase + engine + voice match—no repeat network call. Delete
    that folder or set JARVIS_WELCOME_CACHE_ENABLED=False to force a fresh fetch.
  The welcome sequence runs only once per process. The assistant speaks in the background so Cursor
    opens without waiting for playback to finish (restart the script to run again).
"""

from __future__ import annotations

import asyncio
import email
import hashlib
import imaplib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import wave
import webbrowser
from datetime import datetime
from email.header import decode_header
from email.utils import parseaddr
from pathlib import Path

from dotenv import load_dotenv
import numpy as np
import sounddevice as sd
import speech_recognition as sr

try:
    import websockets
except ImportError:
    websockets = None  # HUD server disables itself gracefully if missing

# Load .env FIRST, before any constant below reads os.environ — otherwise
# overrides (JARVIS_WAKE_MIN_RMS, GROQ_MODEL, JARVIS_INPUT_DEVICE, etc.) would
# silently fall back to their hardcoded defaults every time.
load_dotenv(Path(__file__).resolve().parent / ".env")

# --- tuning knobs -----------------------------------------------------------
SAMPLE_RATE = 44100
BLOCK_MS = 40
CHANNELS = 1

# Word to listen for. Matched case/accent-insensitively against the transcript
# (so "Jarvis", "jarvis", "JÁRVIS" etc. all count).
WAKE_WORD = "jarvis"
# Language passed to the speech recognizer (pt-BR, en-US, ...).
WAKE_LANGUAGE = "pt-BR"

WAKE_MIN_RMS = float((os.environ.get("JARVIS_WAKE_MIN_RMS") or "0.015").strip() or 0.015)
WAKE_SILENCE_HANG_S = float((os.environ.get("JARVIS_WAKE_SILENCE_HANG_S") or "1.1").strip() or 1.1)
WAKE_MIN_UTTERANCE_S = 0.3
WAKE_MAX_UTTERANCE_S = 15.0
WAKE_COOLDOWN_S = 2.0

# After a bare "jarvis" (no question in the same breath), keep listening for
# this many seconds: the *next* utterance is treated as the question even
# without repeating the wake word (e.g. say "jarvis", pause, then "que dia é
# hoje?"). Saying "jarvis, que dia é hoje?" in one breath still works too.
JARVIS_FOLLOWUP_WINDOW_S = 6.0

# Startup mic probe: if default input RMS stays below this, scan for a louder device.
INPUT_PROBE_S = 0.5
INPUT_SILENT_RMS = 0.001

# YouTube: https://www.youtube.com/watch?v=...
SONG_URI = "https://www.youtube.com/watch?v=pAgnJDJN4VA"  # AC/DC - Back In Black (Official 4K Video)

# Cursor: focus existing instance (no -n). Set OPEN_NEW_CURSOR_ON_WAKE_WORD for a new window as well.
FOCUS_EXISTING_CURSOR_ON_WAKE_WORD = True
OPEN_NEW_CURSOR_ON_WAKE_WORD = False
CURSOR_OPEN_FULLSCREEN = True

# Opera GX (fallback: default browser if Opera GX isn't found). URLs overridable in .env.
OPEN_CLAUDE_IN_BROWSER = True
OPEN_BROWSER_FULLSCREEN = False
# False = default Opera GX profile (your normal user, extensions, cookies). True = temp dirs under %TEMP% per site.
BROWSER_SEPARATE_SITE_PROFILES = False
# Which physical screen (1 = leftmost/top-first after sorting). Windows only; ignored elsewhere.
CLAUDE_BROWSER_MONITOR = 1

JARVIS_WELCOME_ENABLED = True
JARVIS_WELCOME_PHRASE = (
    "Bem-vindo de volta, senhor. "
    "O que iremos fazer hoje?"
)
# Seconds after launching SONG_URI before speaking (gives YouTube/browser time to start).
JARVIS_AFTER_SONG_DELAY_S = 1.0
# Save TTS audio under .cache/jarvis_welcome/; replay skips the network call when
# phrase + engine + voice match.
JARVIS_WELCOME_CACHE_ENABLED = True

# Which TTS engine to try first: "edge" (Microsoft Edge's free online voice
# service — no API key, no quota) or "elevenlabs" (paid/credit-based, higher
# voice quality and cloning). Whichever is NOT chosen here is still tried as an
# automatic fallback if the first one fails (missing key, quota exceeded, no
# internet, etc.) — see _speak() — so a problem in one engine doesn't leave
# Jarvis mute, it just uses the other.
JARVIS_TTS_ENGINE = (os.environ.get("JARVIS_TTS_ENGINE") or "edge").strip().lower()
# Voice for edge-tts. Full list: run `edge-tts --list-voices` in a terminal.
# Other pt-BR options include pt-BR-FranciscaNeural (female).
EDGE_TTS_VOICE = (os.environ.get("EDGE_TTS_VOICE") or "pt-BR-AntonioNeural").strip()

# Real Q&A: say "jarvis" followed by a question/request in the same utterance
# (e.g. "jarvis, que dia é hoje?") and it's sent to Groq's free API; the answer
# is spoken back with ElevenLabs. Bare "jarvis" (no question attached) still runs the
# one-time welcome sequence above instead. Requires GROQ_API_KEY (free, no card).
JARVIS_ASSISTANT_ENABLED = True
JARVIS_ASSISTANT_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-20b")
JARVIS_ASSISTANT_MAX_TOKENS = 600
JARVIS_ASSISTANT_SYSTEM_PROMPT = (
    "Você é Jarvis, um assistente de voz falado em português do Brasil, capaz de "
    "conversar e ajudar com qualquer assunto que perguntarem. Responda de forma "
    "natural e completa — explique de verdade quando o assunto pedir, sem se "
    "prender a um número fixo de frases — mas sem enrolação. A resposta vai ser "
    "lida em voz alta, então nunca use markdown, listas, títulos ou emojis: só "
    "texto corrido, como numa fala. {now}"
)
# Keep the last N exchanges as context so follow-up questions ("e sobre amanhã?")
# work naturally. A new conversation starts on its own after this much silence.
JARVIS_ASSISTANT_HISTORY_TURNS = 6
JARVIS_ASSISTANT_HISTORY_IDLE_RESET_S = 120.0

# Voice-controlled app open/close ("jarvis, abre o spotify" / "jarvis, fecha
# o chrome"). Windows only for now. Add friendly-name -> .exe mappings below
# for apps that don't already register themselves under Windows' App Paths
# (most installed apps do and "just work" with their common name/.exe).
JARVIS_APP_CONTROL_ENABLED = True
APP_ALIASES: dict[str, str] = {
    "bloco de notas": "notepad.exe",
    "notepad": "notepad.exe",
    "calculadora": "calc.exe",
    "calculator": "calc.exe",
    "explorador de arquivos": "explorer.exe",
    "paint": "mspaint.exe",
    "chrome": "chrome.exe",
    "google chrome": "chrome.exe",
    "opera": "opera.exe",
    "opera gx": "opera.exe",
    "spotify": "Spotify.exe",
    "word": "WINWORD.EXE",
    "microsoft word": "WINWORD.EXE",
    "excel": "EXCEL.EXE",
    "microsoft excel": "EXCEL.EXE",
    "powerpoint": "POWERPNT.EXE",
    "cursor": "Cursor.exe",
    "vscode": "Code.exe",
    "vs code": "Code.exe",
    "visual studio code": "Code.exe",
    "discord": "Discord.exe",
    "steam": "steam.exe",
    "whatsapp": "WhatsApp.exe",
    "telegram": "Telegram.exe",
}

# Voice-read emails ("jarvis, tem email novo?" / "jarvis, lê meus emails").
# Reads the INBOX via IMAP (Gmail by default) and skips anything that looks
# like spam/newsletter/marketing mail — true spam is already filtered by
# Gmail into its Spam folder (never read here since we only open INBOX); on
# top of that, messages carrying a List-Unsubscribe header, or
# Precedence: bulk/list/junk, are treated as bulk mail and skipped too.
# Requires EMAIL_ADDRESS + EMAIL_APP_PASSWORD in .env (see README).
JARVIS_EMAIL_ENABLED = True
EMAIL_MAX_RESULTS = 15
EMAIL_BODY_PREVIEW_CHARS = 220

# Opens the visual orb (jarvis_orb.html, in the same folder as this script)
# in the default browser as soon as the script starts — a HUD-style
# companion that replaces the terminal window. It connects back to this
# process over a local WebSocket (see JARVIS_HUD_* below) and shows live
# status (idle/thinking/speaking), what was heard, and what Jarvis says.
OPEN_ORB_VISUAL_ON_START = True
ORB_VISUAL_HTML_PATH = Path(__file__).resolve().parent / "jarvis_orb.html"

# Local-only WebSocket link to jarvis_orb.html, so the HUD can show live
# status/transcript/answers instead of (or alongside) the terminal. No
# external network exposure — binds to 127.0.0.1 only.
JARVIS_HUD_ENABLED = True
JARVIS_HUD_PORT = int((os.environ.get("JARVIS_HUD_PORT") or "8765").strip() or 8765)

# Where logs go. With pythonw.exe (no console attached, used for silent
# startup-on-login), there is nothing to print to — logs go to this file
# instead. LOG_FILE_PATH is always written to; the console handler is only
# added when a real console is attached (python.exe).
LOG_FILE_PATH = Path(__file__).resolve().parent / "jarvis.log"

_log_handlers: list[logging.Handler] = [
    logging.FileHandler(LOG_FILE_PATH, encoding="utf-8")
]
try:
    # Under pythonw.exe (no console), sys.stdout is sometimes None and
    # sometimes a dummy object that raises on write, depending on the Python
    # build — a bare "is not None" check misses the second case, so verify
    # with an actual write before trusting it as a real console.
    if sys.stdout is not None:
        sys.stdout.write("")
        sys.stdout.flush()
        _log_handlers.append(logging.StreamHandler(sys.stdout))
except Exception:
    pass
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=_log_handlers,
)
log = logging.getLogger("jarvis")


# --- HUD (jarvis_orb.html) live link ----------------------------------------
_hud_loop: asyncio.AbstractEventLoop | None = None
_hud_clients: set = set()
_hud_ready = threading.Event()


def _hud_start_server() -> None:
    """Runs in its own background thread for the whole process lifetime: owns
    an asyncio event loop and a tiny local WebSocket server that
    jarvis_orb.html connects to. Never touches the internet — 127.0.0.1 only."""
    global _hud_loop
    if websockets is None:
        log.warning(
            "The 'websockets' package is missing; the HUD (jarvis_orb.html) "
            "will show 'not connected'. Run: pip install -r requirements.txt"
        )
        return

    async def _handler(websocket):
        _hud_clients.add(websocket)
        try:
            await websocket.send(json.dumps({"type": "state", "mode": "idle"}))
            async for _ in websocket:
                pass  # the page is a passive display; it never sends anything back
        except Exception:
            pass
        finally:
            _hud_clients.discard(websocket)

    async def _serve_forever() -> None:
        try:
            async with websockets.serve(_handler, "127.0.0.1", JARVIS_HUD_PORT):
                _hud_ready.set()
                await asyncio.Future()  # run until the process exits
        except OSError as e:
            log.warning("HUD server could not bind to port %d: %s", JARVIS_HUD_PORT, e)

    loop = asyncio.new_event_loop()
    _hud_loop = loop
    asyncio.set_event_loop(loop)
    loop.run_until_complete(_serve_forever())


def hud_broadcast(payload: dict) -> None:
    """Thread-safe: send one JSON event to every connected jarvis_orb.html
    tab. A no-op (never raises) if the HUD is disabled, not started yet, or
    no page is currently connected — callers don't need to check first."""
    if not JARVIS_HUD_ENABLED or _hud_loop is None or not _hud_clients:
        return
    data = json.dumps(payload)

    async def _send_all() -> None:
        stale = []
        for ws in list(_hud_clients):
            try:
                await ws.send(data)
            except Exception:
                stale.append(ws)
        for ws in stale:
            _hud_clients.discard(ws)

    try:
        asyncio.run_coroutine_threadsafe(_send_all(), _hud_loop)
    except RuntimeError:
        pass  # loop already shutting down


def hud_state(mode: str) -> None:
    hud_broadcast({"type": "state", "mode": mode})


def hud_heard(text: str) -> None:
    hud_broadcast({"type": "heard", "text": text})


def hud_response(text: str) -> None:
    hud_broadcast({"type": "response", "text": text})


def block_samples() -> int:
    n = int(SAMPLE_RATE * BLOCK_MS / 1000)
    return max(n, 1)


def rms_mono(block: np.ndarray) -> float:
    if block.ndim > 1:
        block = np.mean(block.astype(np.float64), axis=1)
    else:
        block = block.astype(np.float64)
    if block.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(block**2)))


def _input_devices() -> list[tuple[int, dict]]:
    return [
        (i, dev)
        for i, dev in enumerate(sd.query_devices())
        if dev["max_input_channels"] >= 1
    ]


def _resolve_input_device_index(spec: str) -> int:
    spec = spec.strip()
    if spec.isdigit():
        idx = int(spec)
        sd.query_devices(idx)
        return idx
    needle = spec.lower()
    for idx, dev in _input_devices():
        if needle in dev["name"].lower():
            return idx
    raise ValueError(f"No input device matches {spec!r}")


def _probe_input_max_rms(device: int, blocksize: int) -> float | None:
    try:
        with sd.InputStream(
            device=device,
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="float32",
            blocksize=blocksize,
        ) as stream:
            peak = 0.0
            deadline = time.monotonic() + INPUT_PROBE_S
            while time.monotonic() < deadline:
                data, _ = stream.read(blocksize)
                peak = max(peak, rms_mono(data))
            return peak
    except sd.PortAudioError:
        return None


def _choose_input_device(blocksize: int) -> int:
    log.info("Audio devices:\n%s", sd.query_devices())

    override = (os.environ.get("JARVIS_INPUT_DEVICE") or "").strip()
    if override:
        try:
            idx = _resolve_input_device_index(override)
        except ValueError as e:
            log.error("%s", e)
            log.error("Set JARVIS_INPUT_DEVICE to a device index or name substring.")
            raise SystemExit(1) from e
        name = sd.query_devices(idx)["name"]
        peak = _probe_input_max_rms(idx, blocksize)
        log.info("Using JARVIS_INPUT_DEVICE [%d]: %s", idx, name)
        if peak is None:
            log.warning("Could not open configured mic; trying anyway.")
        elif peak < INPUT_SILENT_RMS:
            log.warning(
                "Configured mic looks silent (probe rms=%.5f). "
                "Check Windows input level or try another JARVIS_INPUT_DEVICE.",
                peak,
            )
        else:
            log.info("Mic probe OK (rms=%.5f).", peak)
        return idx

    default = sd.default.device[0]
    if default is not None and default >= 0:
        default_name = sd.query_devices(default)["name"]
        peak = _probe_input_max_rms(default, blocksize)
        if peak is not None and peak >= INPUT_SILENT_RMS:
            log.info(
                "Using default microphone [%d]: %s (probe rms=%.5f)",
                default,
                default_name,
                peak,
            )
            return default
        log.warning(
            "Default mic [%d] %s is silent or unavailable (probe rms=%s); "
            "scanning other inputs...",
            default,
            default_name,
            f"{peak:.5f}" if peak is not None else "unopenable",
        )

    best_idx: int | None = None
    best_peak = -1.0
    for idx, dev in _input_devices():
        if default is not None and idx == default:
            continue
        peak = _probe_input_max_rms(idx, blocksize)
        if peak is not None and peak > best_peak:
            best_peak = peak
            best_idx = idx

    if best_idx is not None and best_peak >= INPUT_SILENT_RMS:
        log.info(
            "Auto-selected microphone [%d]: %s (probe rms=%.5f)",
            best_idx,
            sd.query_devices(best_idx)["name"],
            best_peak,
        )
        return best_idx

    if default is not None and default >= 0:
        log.warning("No active mic found; falling back to default [%d].", default)
        return default
    inputs = _input_devices()
    if not inputs:
        log.error("No input devices found.")
        raise SystemExit(1)
    idx, dev = inputs[0]
    log.warning("No active mic found; falling back to [%d] %s.", idx, dev["name"])
    return idx


def _elevenlabs_pcm_sample_rate(output_format: str) -> int:
    override = (os.environ.get("ELEVENLABS_PCM_SAMPLE_RATE") or "").strip()
    if override.isdigit():
        return int(override)
    if output_format.startswith("pcm_"):
        try:
            return int(output_format.split("_", maxsplit=1)[1])
        except (ValueError, IndexError):
            pass
    return 24000


def elevenlabs_env_config() -> tuple[str, str, str, int]:
    """voice_id, model_id, output_format, pcm_sample_rate."""
    voice = (os.environ.get("ELEVENLABS_VOICE_ID") or "").strip()
    model = (os.environ.get("ELEVENLABS_MODEL_ID") or "eleven_multilingual_v2").strip()
    fmt = (os.environ.get("ELEVENLABS_OUTPUT_FORMAT") or "pcm_24000").strip()
    rate = _elevenlabs_pcm_sample_rate(fmt)
    return voice, model, fmt, rate


def _jarvis_welcome_cache_dir() -> Path:
    base = Path(__file__).resolve().parent
    override = (os.environ.get("JARVIS_WELCOME_CACHE_DIR") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return base / ".cache" / "jarvis_welcome"


def _speech_cache_path(text: str, cache_key: str, ext: str) -> Path:
    """Generic cache path for any TTS engine: same text + same engine/voice/
    settings (cache_key) always hashes to the same file, so a repeated phrase
    (e.g. the welcome line) replays instantly instead of hitting the network."""
    key = f"{text}|{cache_key}".encode()
    digest = hashlib.sha256(key).hexdigest()[:24]
    return _jarvis_welcome_cache_dir() / f"{digest}.{ext}"


def _jarvis_welcome_cache_path(
    text: str, voice_id: str, model_id: str, output_format: str
) -> Path:
    return _speech_cache_path(
        text, f"elevenlabs|{voice_id}|{model_id}|{output_format}", "wav"
    )


def _play_pcm_wav_file(path: Path) -> bool:
    try:
        with wave.open(str(path), "rb") as wf:
            ch = wf.getnchannels()
            sw = wf.getsampwidth()
            rate = wf.getframerate()
            if ch != 1 or sw != 2:
                log.warning("Unsupported cached WAV (channels=%s, width=%s).", ch, sw)
                return False
            raw = wf.readframes(wf.getnframes())
    except (OSError, wave.Error) as e:
        log.warning("Could not read cached welcome audio: %s", e)
        return False
    if not raw:
        return False
    pcm_i16 = np.frombuffer(raw, dtype=np.int16)
    pcm_f = pcm_i16.astype(np.float32) / 32768.0
    try:
        sd.play(pcm_f, rate)
        sd.wait()
    except Exception as e:
        log.warning("Could not play cached welcome audio: %s", e)
        return False
    return True


def _play_audio_file(path: Path) -> bool:
    """Play a compressed audio file (mp3, as returned by edge-tts) via the
    'playsound' package, which on Windows uses the built-in MCI/winmm codec —
    no ffmpeg or extra decoding dependency needed."""
    try:
        from playsound import playsound
    except ImportError:
        log.warning(
            "Install dependencies: pip install -r requirements.txt (missing 'playsound')."
        )
        return False
    try:
        playsound(str(path))
    except Exception as e:
        log.warning("Could not play audio file %s: %s", path, e)
        return False
    return True


def _save_pcm_wav_file(path: Path, pcm_bytes: bytes, sample_rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with wave.open(str(tmp), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(pcm_bytes)
        tmp.replace(path)
    except OSError:
        if tmp.is_file():
            tmp.unlink(missing_ok=True)
        raise


def _speak(text: str) -> None:
    """Fetch TTS audio and play it — tries JARVIS_TTS_ENGINE first, then
    automatically falls back to the other engine if the first one fails or
    isn't configured (missing key, quota exceeded, no internet, edge-tts not
    installed, ...), so a problem in one engine doesn't leave Jarvis mute.
    Shared by the fixed welcome phrase and by spoken assistant answers."""
    text = text.strip()
    if not text:
        return
    hud_response(text)
    hud_state("speaking")
    try:
        engines = [_edge_tts_speak_impl, _elevenlabs_speak_impl]
        if JARVIS_TTS_ENGINE == "elevenlabs":
            engines.reverse()
        for engine in engines:
            if engine(text):
                return
        log.warning(
            "No TTS engine could speak the text (see the warnings above for why)."
        )
    finally:
        hud_state("idle")


def _edge_tts_speak_impl(text: str) -> bool:
    """Free TTS via Microsoft Edge's online voice service — no API key, no
    quota. Requires internet (it talks to Microsoft's service) but not an
    installed copy of Edge itself."""
    try:
        import edge_tts
    except ImportError:
        log.warning(
            "Install dependencies: pip install -r requirements.txt (missing 'edge-tts')."
        )
        return False

    voice = EDGE_TTS_VOICE
    cache_path = _speech_cache_path(text, f"edge|{voice}", "mp3")
    if JARVIS_WELCOME_CACHE_ENABLED and cache_path.is_file():
        log.info("Playing speech from cache: %s", cache_path)
        if _play_audio_file(cache_path):
            return True
        log.warning("Cache miss after read failure; fetching from edge-tts.")

    if JARVIS_WELCOME_CACHE_ENABLED:
        out_path = cache_path
        out_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        out_path = (
            Path(tempfile.gettempdir())
            / f"jarvis_edge_tts_{os.getpid()}_{int(time.time() * 1000)}.mp3"
        )

    async def _run() -> None:
        communicate = edge_tts.Communicate(text, voice)
        await communicate.save(str(out_path))

    try:
        asyncio.run(_run())
    except Exception as e:
        log.warning("edge-tts request failed: %s", e)
        return False

    if not out_path.is_file() or out_path.stat().st_size == 0:
        log.warning("edge-tts returned no audio.")
        return False

    ok = _play_audio_file(out_path)
    if ok and JARVIS_WELCOME_CACHE_ENABLED:
        log.info("Saved speech audio to cache: %s", out_path)
    if not JARVIS_WELCOME_CACHE_ENABLED:
        out_path.unlink(missing_ok=True)
    return ok


def _elevenlabs_speak_impl(text: str) -> bool:
    vid, model_id, output_format, pcm_rate = elevenlabs_env_config()
    if not vid:
        log.warning("Set ELEVENLABS_VOICE_ID in the environment for ElevenLabs TTS.")
        return False
    if not output_format.startswith("pcm_"):
        # Everything downstream (cache file, sd.play) assumes raw 16-bit PCM.
        # Any other ELEVENLABS_OUTPUT_FORMAT (mp3_*, ulaw_8000, ...) would be
        # decoded as garbage noise or crash the playback thread.
        log.warning(
            "ELEVENLABS_OUTPUT_FORMAT=%r is not a pcm_* format; this script only "
            "knows how to play raw PCM. Set it to e.g. pcm_24000.",
            output_format,
        )
        return False

    cache_path = _jarvis_welcome_cache_path(text, vid, model_id, output_format)
    if JARVIS_WELCOME_CACHE_ENABLED and cache_path.is_file():
        log.info("Playing speech from cache: %s", cache_path)
        if _play_pcm_wav_file(cache_path):
            return True
        log.warning("Cache miss after read failure; fetching from ElevenLabs.")

    api_key = (os.environ.get("ELEVENLABS_API_KEY") or "").strip()
    if not api_key:
        log.warning("Set ELEVENLABS_API_KEY in the environment for ElevenLabs TTS.")
        return False
    try:
        from elevenlabs.client import ElevenLabs
    except ImportError:
        log.warning("Install dependencies: pip install -r requirements.txt")
        return False
    try:
        client = ElevenLabs(api_key=api_key)
        chunks = client.text_to_speech.convert(
            voice_id=vid,
            text=text,
            model_id=model_id,
            output_format=output_format,
        )
        raw = b"".join(chunks)
    except Exception as e:
        log.warning("ElevenLabs TTS failed: %s", e)
        return False
    if not raw:
        log.warning("ElevenLabs returned empty audio.")
        return False
    if len(raw) % 2:
        # 16-bit PCM must come in 2-byte samples; an odd length means a
        # truncated/corrupted response. Drop the stray trailing byte.
        log.warning(
            "ElevenLabs returned an odd number of bytes (%d); dropping the last byte.",
            len(raw),
        )
        raw = raw[:-1]
        if not raw:
            return False
    try:
        pcm_i16 = np.frombuffer(raw, dtype=np.int16)
        pcm_f = pcm_i16.astype(np.float32) / 32768.0
    except ValueError as e:
        # Decode failure (e.g. response wasn't actually PCM) — don't cache garbage.
        log.warning("Could not decode ElevenLabs audio as PCM: %s", e)
        return False
    if JARVIS_WELCOME_CACHE_ENABLED:
        try:
            _save_pcm_wav_file(cache_path, raw, pcm_rate)
            log.info("Saved speech audio to cache: %s", cache_path)
        except OSError as e:
            log.warning("Could not save speech cache: %s", e)
    try:
        sd.play(pcm_f, pcm_rate)
        sd.wait()
    except Exception as e:
        log.warning("Could not play ElevenLabs audio: %s", e)
        return False
    return True


def say_jarvis_welcome() -> None:
    if not JARVIS_WELCOME_ENABLED or not JARVIS_WELCOME_PHRASE.strip():
        return
    _speak(JARVIS_WELCOME_PHRASE.strip())


_PT_WEEKDAYS = (
    "segunda-feira",
    "terça-feira",
    "quarta-feira",
    "quinta-feira",
    "sexta-feira",
    "sábado",
    "domingo",
)
_PT_MONTHS = (
    "janeiro", "fevereiro", "março", "abril", "maio", "junho",
    "julho", "agosto", "setembro", "outubro", "novembro", "dezembro",
)


def _now_pt_br_str() -> str:
    """Local date/time as a Portuguese sentence, computed manually so it does
    not depend on the OS locale being set to pt-BR (unreliable on Windows)."""
    now = datetime.now()
    weekday = _PT_WEEKDAYS[now.weekday()]
    month = _PT_MONTHS[now.month - 1]
    return (
        f"Hoje é {weekday}, {now.day} de {month} de {now.year}. "
        f"Agora são {now.strftime('%H:%M')}."
    )


def _resolve_app_exe(name: str) -> str:
    return APP_ALIASES.get(name.strip().lower(), name.strip())


def _find_squirrel_app_exe(app_folder: str, exe_name: str) -> str | None:
    """Squirrel-installed apps (Discord, WhatsApp, and many other Electron
    apps) live under %LOCALAPPDATA%\\<app_folder>\\app-<version>\\<exe> and
    don't register themselves under Windows' App Paths, so a name-only launch
    (os.startfile) can't find them. Look up the newest version folder."""
    base = Path(os.environ.get("LOCALAPPDATA", "")) / app_folder
    if not base.is_dir():
        return None
    for folder in sorted(base.glob("app-*"), reverse=True):
        candidate = folder / exe_name
        if candidate.is_file():
            return str(candidate)
    return None


# exe name (lowercase) -> (%LOCALAPPDATA% subfolder, exe inside app-<version>/)
SQUIRREL_APPS: dict[str, tuple[str, str]] = {
    "discord.exe": ("Discord", "Discord.exe"),
    "whatsapp.exe": ("WhatsApp", "WhatsApp.exe"),
}


def open_app(name: str) -> str:
    """Opens an app by its common name. Returns a short spoken-friendly result
    (never raises — errors come back as a message so the assistant can say
    what happened)."""
    if sys.platform != "win32":
        return "Abrir aplicativos só é suportado no Windows por enquanto."
    exe = _resolve_app_exe(name)

    squirrel = SQUIRREL_APPS.get(exe.lower())
    if squirrel:
        full_path = _find_squirrel_app_exe(*squirrel)
        if full_path:
            try:
                subprocess.Popen([full_path])
                log.info("Opened app %r via %s", name, full_path)
                return f"Abri {name}."
            except OSError as e:
                log.warning("Could not launch %r at %s: %s", name, full_path, e)
                return f"Não consegui abrir {name}."

    if exe.lower() == "opera.exe":
        # Same problem as Discord/WhatsApp: Opera GX installs per-user and
        # doesn't register under Windows' App Paths, so reuse the finder
        # already used to launch the song/Claude in it.
        opera_path = _opera_gx_executable()
        if opera_path:
            try:
                subprocess.Popen([opera_path])
                log.info("Opened app %r via %s", name, opera_path)
                return f"Abri {name}."
            except OSError as e:
                log.warning("Could not launch %r at %s: %s", name, opera_path, e)
                return f"Não consegui abrir {name}."

    try:
        os.startfile(exe)  # resolves via PATH / Windows App Paths registry
        log.info("Opened app %r (%s)", name, exe)
        return f"Abri {name}."
    except OSError:
        pass

    # Last resort: check PATH directly. (Deliberately not falling back to
    # `cmd /c start`: when the target isn't found it still returns success to
    # Python — Windows shows a GUI error box instead — so it can't tell us
    # whether the app actually opened.)
    path = shutil.which(exe)
    if path:
        try:
            subprocess.Popen([path])
            log.info("Opened app %r via PATH (%s)", name, path)
            return f"Abri {name}."
        except OSError as e:
            log.warning("Could not launch %r at %s: %s", name, path, e)
            return f"Não consegui abrir {name}."

    log.warning("Could not resolve app %r (tried %s)", name, exe)
    return (
        f"Não achei o {name} instalado nem consegui localizar o executável. "
        f"Se o programa tiver outro nome de arquivo, adicione em APP_ALIASES no jarvis.py."
    )


def close_app(name: str) -> str:
    """Closes a running app by its common name via taskkill. Returns a short
    spoken-friendly result."""
    if sys.platform != "win32":
        return "Fechar aplicativos só é suportado no Windows por enquanto."
    exe = _resolve_app_exe(name)
    if not exe.lower().endswith(".exe"):
        exe += ".exe"
    try:
        result = subprocess.run(
            ["taskkill", "/IM", exe, "/F"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            log.info("Closed app %r (%s)", name, exe)
            return f"Fechei {name}."
        log.info(
            "taskkill for %r (%s) returned %d: %s",
            name, exe, result.returncode, result.stderr.strip(),
        )
        return f"Não encontrei {name} aberto."
    except Exception as e:
        log.warning("Could not close app %r (%s): %s", name, exe, e)
        return f"Não consegui fechar {name}."


def _decode_mime_header(value: str | None) -> str:
    if not value:
        return ""
    decoded = ""
    for text, enc in decode_header(value):
        if isinstance(text, bytes):
            try:
                decoded += text.decode(enc or "utf-8", errors="replace")
            except (LookupError, TypeError):
                decoded += text.decode("utf-8", errors="replace")
        else:
            decoded += text
    return decoded.strip()


def _email_looks_like_bulk(msg: email.message.Message) -> bool:
    """Heuristic: skip newsletters/marketing even if Gmail didn't flag them as
    spam. A List-Unsubscribe header is one of the most reliable signals bulk
    senders use; Precedence/Auto-Submitted catch most of the rest."""
    if msg.get("List-Unsubscribe"):
        return True
    if (msg.get("Precedence") or "").strip().lower() in ("bulk", "list", "junk"):
        return True
    auto_submitted = (msg.get("Auto-Submitted") or "").strip().lower()
    if auto_submitted and auto_submitted != "no":
        return True
    return False


def _email_body_preview(msg: email.message.Message) -> str:
    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        if part.get_content_type() != "text/plain" or part.get_filename():
            continue
        try:
            payload = part.get_payload(decode=True) or b""
            charset = part.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="replace")
            return " ".join(text.split())[:EMAIL_BODY_PREVIEW_CHARS]
        except (LookupError, TypeError, ValueError):
            continue
    return ""


def read_emails(count: int = 5, unread_only: bool = True) -> str:
    """Reads the most recent inbox emails via IMAP, skipping anything that
    looks like spam/bulk/marketing mail. Returns a short text summary for the
    assistant to read out loud (never raises — errors come back as a message
    the assistant can relay)."""
    address = (os.environ.get("EMAIL_ADDRESS") or "").strip()
    app_password = (os.environ.get("EMAIL_APP_PASSWORD") or "").strip()
    if not address or not app_password:
        return (
            "Não consegui acessar os emails: EMAIL_ADDRESS e EMAIL_APP_PASSWORD "
            "não estão configurados no .env."
        )
    host = (os.environ.get("EMAIL_IMAP_HOST") or "imap.gmail.com").strip()
    try:
        port = int((os.environ.get("EMAIL_IMAP_PORT") or "993").strip() or 993)
    except ValueError:
        port = 993
    try:
        count = max(1, min(int(count), EMAIL_MAX_RESULTS))
    except (TypeError, ValueError):
        count = 5

    try:
        conn = imaplib.IMAP4_SSL(host, port)
    except OSError as e:
        log.warning("IMAP connection to %s:%d failed: %s", host, port, e)
        return "Não consegui conectar ao servidor de email."

    try:
        try:
            conn.login(address, app_password)
        except imaplib.IMAP4.error as e:
            log.warning("IMAP login failed for %s: %s", address, e)
            return "Não consegui entrar na caixa de email — confira EMAIL_ADDRESS e EMAIL_APP_PASSWORD."

        conn.select("INBOX", readonly=True)
        criterion = "UNSEEN" if unread_only else "ALL"
        status, data = conn.search(None, criterion)
        if status != "OK":
            return "Não consegui listar os emails da caixa de entrada."
        ids = data[0].split()
        if not ids:
            return "Não há emails novos." if unread_only else "A caixa de entrada está vazia."

        # Newest first; fetch a few extra candidates since some get filtered as bulk.
        candidate_ids = list(reversed(ids))[: count * 3]
        summaries: list[str] = []
        for msg_id in candidate_ids:
            if len(summaries) >= count:
                break
            status, msg_data = conn.fetch(msg_id, "(RFC822)")
            if status != "OK" or not msg_data or not msg_data[0]:
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            if _email_looks_like_bulk(msg):
                continue
            sender_name, sender_addr = parseaddr(_decode_mime_header(msg.get("From")))
            sender = sender_name or sender_addr or "remetente desconhecido"
            subject = _decode_mime_header(msg.get("Subject")) or "(sem assunto)"
            preview = _email_body_preview(msg)
            line = f'De {sender}: "{subject}"'
            if preview:
                line += f" — {preview}"
            summaries.append(line)
    finally:
        try:
            conn.logout()
        except Exception:
            pass

    if not summaries:
        return "Só encontrei emails que pareciam spam ou newsletter; nada relevante pra ler."
    return "; ".join(summaries)


JARVIS_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "open_app",
            "description": "Abre um aplicativo no PC do usuário pelo nome comum (ex: 'chrome', 'bloco de notas', 'spotify').",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Nome do aplicativo a abrir, como o usuário mencionou.",
                    }
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "close_app",
            "description": "Fecha um aplicativo em execução no PC do usuário pelo nome comum (ex: 'chrome', 'spotify').",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Nome do aplicativo a fechar, como o usuário mencionou.",
                    }
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_emails",
            "description": (
                "Lê os emails mais recentes da caixa de entrada do usuário. Ignora "
                "automaticamente spam, newsletters e emails de marketing/bulk. Use "
                "quando o usuário pedir para ver, checar, ler ou resumir os emails."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "count": {
                        "type": "integer",
                        "description": "Quantos emails retornar (padrão 5, máximo 15).",
                    },
                    "unread_only": {
                        "type": "boolean",
                        "description": (
                            "True (padrão) retorna só emails não lidos. False retorna "
                            "os mais recentes independente de já terem sido lidos."
                        ),
                    },
                },
                "required": [],
            },
        },
    },
]
JARVIS_TOOL_IMPL = {
    "open_app": open_app,
    "close_app": close_app,
    "read_emails": read_emails,
}


def _run_tool_call(name: str, arguments_json: str) -> str:
    try:
        args = json.loads(arguments_json or "{}")
    except json.JSONDecodeError:
        args = {}
    fn = JARVIS_TOOL_IMPL.get(name)
    if not fn:
        return f"Ferramenta desconhecida: {name}"
    try:
        return fn(**args)
    except TypeError as e:
        log.warning("Bad arguments for tool %s(%r): %s", name, args, e)
        return f"Argumentos inválidos para {name}."


_assistant_history: list[dict] = []
_assistant_history_lock = threading.Lock()
_assistant_last_turn_at = 0.0


def _take_conversation_messages(question: str) -> list[dict]:
    """Returns recent history + the new question, ready to send as `messages`.
    Starts a fresh conversation if it's been quiet for
    JARVIS_ASSISTANT_HISTORY_IDLE_RESET_S (does not mutate history itself —
    call _remember_turn once the answer comes back)."""
    global _assistant_last_turn_at
    now = time.monotonic()
    with _assistant_history_lock:
        if now - _assistant_last_turn_at > JARVIS_ASSISTANT_HISTORY_IDLE_RESET_S:
            _assistant_history.clear()
        _assistant_last_turn_at = now
        history_copy = list(_assistant_history)
    return history_copy + [{"role": "user", "content": question}]


def _remember_turn(question: str, answer: str) -> None:
    with _assistant_history_lock:
        _assistant_history.append({"role": "user", "content": question})
        _assistant_history.append({"role": "assistant", "content": answer})
        max_msgs = JARVIS_ASSISTANT_HISTORY_TURNS * 2
        if len(_assistant_history) > max_msgs:
            del _assistant_history[: len(_assistant_history) - max_msgs]


def _active_tools() -> list[dict]:
    names_enabled = set()
    if JARVIS_APP_CONTROL_ENABLED:
        names_enabled.update({"open_app", "close_app"})
    if JARVIS_EMAIL_ENABLED:
        names_enabled.add("read_emails")
    return [t for t in JARVIS_TOOLS if t["function"]["name"] in names_enabled]


def ask_assistant(question: str) -> str | None:
    """Send `question` (plus recent conversation history) to Groq's free API
    and return a short spoken-friendly answer, or None if the assistant is
    unavailable/misconfigured."""
    api_key = (os.environ.get("GROQ_API_KEY") or "").strip()
    if not api_key:
        log.warning("Set GROQ_API_KEY in the environment to enable Q&A.")
        return None
    try:
        from groq import Groq
    except ImportError:
        log.warning(
            "Install dependencies: pip install -r requirements.txt (missing 'groq')."
        )
        return None

    system = JARVIS_ASSISTANT_SYSTEM_PROMPT.format(now=_now_pt_br_str())
    convo = _take_conversation_messages(question)
    kwargs: dict = {}
    if JARVIS_ASSISTANT_MODEL.startswith(("openai/gpt-oss", "qwen/qwen3")):
        # These are reasoning models: without this they spend part of the
        # token budget on hidden chain-of-thought before answering, which can
        # eat the whole max_tokens for a short spoken reply. "low" keeps
        # latency down for a voice assistant.
        kwargs["reasoning_effort"] = "low"
    active_tools = _active_tools()
    if active_tools:
        kwargs["tools"] = active_tools
        kwargs["tool_choice"] = "auto"
    try:
        client = Groq(api_key=api_key)
        messages: list[dict] = [{"role": "system", "content": system}, *convo]
        response = client.chat.completions.create(
            model=JARVIS_ASSISTANT_MODEL,
            max_tokens=JARVIS_ASSISTANT_MAX_TOKENS,
            messages=messages,
            **kwargs,
        )
        msg = response.choices[0].message
        tool_calls = getattr(msg, "tool_calls", None)
        if tool_calls:
            messages.append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in tool_calls
                ],
            })
            for tc in tool_calls:
                result = _run_tool_call(tc.function.name, tc.function.arguments)
                log.info("Tool %s(%s) -> %s", tc.function.name, tc.function.arguments, result)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                })
            follow_up = client.chat.completions.create(
                model=JARVIS_ASSISTANT_MODEL,
                max_tokens=JARVIS_ASSISTANT_MAX_TOKENS,
                messages=messages,
                **{k: v for k, v in kwargs.items() if k not in ("tools", "tool_choice")},
            )
            answer = (follow_up.choices[0].message.content or "").strip()
        else:
            answer = (msg.content or "").strip()
    except Exception as e:
        log.warning("Assistant request failed: %s", e)
        return None
    if answer:
        _remember_turn(question, answer)
    return answer or None


def _handle_jarvis_question(question: str) -> None:
    """Runs in a background thread: ask the assistant and speak the answer
    back, then re-open the follow-up window so the conversation can continue
    without repeating the wake word."""
    if not JARVIS_ASSISTANT_ENABLED:
        return
    log.info("Asking assistant: %r", question)
    answer = ask_assistant(question)
    if not answer:
        log.warning("No answer from assistant for: %r", question)
        # Previously this just logged a warning and silently went back to
        # idle — from the HUD/voice side it looked exactly like nothing had
        # happened at all (no "Você: ..." update beyond what was already
        # shown, no spoken reply), which is indistinguishable from a wake
        # word or speech-recognition failure. Surface it instead, so a
        # broken/misconfigured assistant (bad GROQ_API_KEY, wrong/decommissioned
        # GROQ_MODEL, network issue, etc.) is obvious instead of looking like
        # Jarvis just isn't responding. See jarvis.log for the actual error.
        _speak(
            "Desculpe, não consegui falar com o assistente agora. "
            "Confira o jarvis.log para mais detalhes."
        )
        return
    log.info("Assistant answered: %r", answer)
    _speak(answer)
    _arm_followup_window()


def play_song(uri: str) -> None:
    u = uri.strip()
    if not u:
        return
    opera = _opera_gx_executable()
    if opera:
        try:
            subprocess.Popen(
                [opera, "--new-window", u],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            return
        except OSError as e:
            log.warning("Could not open SONG_URI in Opera GX: %s", e)
    else:
        log.warning("Opera GX not found; opening SONG_URI in default browser.")
    try:
        if sys.platform == "win32":
            os.startfile(u)
        else:
            webbrowser.open(u)
    except OSError as e:
        log.warning("Could not open SONG_URI: %s", e)


def _opera_gx_executable() -> str | None:
    if sys.platform == "win32":
        for base in (
            os.environ.get("LOCALAPPDATA", ""),  # default per-user install
            os.environ.get("ProgramFiles", r"C:\Program Files"),
            os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        ):
            if not base:
                continue
            p = os.path.join(base, "Programs", "Opera GX", "opera.exe")
            if os.path.isfile(p):
                return p
            p = os.path.join(base, "Opera GX", "opera.exe")
            if os.path.isfile(p):
                return p
    return shutil.which("opera")


def _win32_sorted_monitor_rects() -> list[tuple[int, int, int, int]]:
    """Each monitor as (left, top, right, bottom), sorted left-to-right then top-to-bottom."""
    if sys.platform != "win32":
        return []
    import ctypes
    from ctypes import wintypes

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", wintypes.LONG),
            ("top", wintypes.LONG),
            ("right", wintypes.LONG),
            ("bottom", wintypes.LONG),
        ]

    collected: list[tuple[int, int, int, int]] = []

    @ctypes.WINFUNCTYPE(
        wintypes.BOOL,
        wintypes.HMONITOR,
        wintypes.HDC,
        ctypes.POINTER(RECT),
        wintypes.LPARAM,
    )
    def _cb(_hm, _hdc, lprc, _lp):
        r = lprc.contents
        collected.append((int(r.left), int(r.top), int(r.right), int(r.bottom)))
        return True

    ctypes.windll.user32.EnumDisplayMonitors(None, None, _cb, 0)
    collected.sort(key=lambda t: (t[0], t[1]))
    return collected


def _browser_monitor_top_left(one_based_index: int) -> tuple[int, int]:
    """Top-left corner on virtual desktop for monitor N (1-based)."""
    l, t, _, _ = _browser_monitor_bounds(one_based_index)
    return (l, t)


def _browser_monitor_bounds(one_based_index: int) -> tuple[int, int, int, int]:
    """Monitor N as (left, top, right, bottom), 1-based index (sorted like other browser helpers)."""
    rects = _win32_sorted_monitor_rects()
    if not rects:
        return (0, 0, 1920, 1080)
    idx = one_based_index - 1
    if idx < 0:
        idx = 0
    if idx >= len(rects):
        log.warning(
            "Monitor %d requested but only %d found; using last monitor.",
            one_based_index,
            len(rects),
        )
        idx = len(rects) - 1
    return rects[idx]


def _browser_monitor_pixel_size(one_based_index: int) -> tuple[int, int]:
    l, t, r, b = _browser_monitor_bounds(one_based_index)
    return (max(320, r - l), max(240, b - t))


def _browser_window_size() -> tuple[int, int]:
    w = (os.environ.get("BROWSER_WINDOW_WIDTH") or "1400").strip()
    h = (os.environ.get("BROWSER_WINDOW_HEIGHT") or "900").strip()
    try:
        return (max(400, int(w)), max(300, int(h)))
    except ValueError:
        return (1400, 900)


def _browser_site_user_data_dir(site_key: str) -> str:
    p = Path(tempfile.gettempdir()) / "jarvis-wake-opera" / site_key
    p.mkdir(parents=True, exist_ok=True)
    return str(p)


def _browser_new_window_wait_timeout_s() -> float:
    try:
        return max(3.0, float((os.environ.get("BROWSER_NEW_WINDOW_WAIT_S") or "25").strip()))
    except ValueError:
        return 25.0


def _opera_top_level_hwnds_win32() -> set[int]:
    """HWND ints for visible-or-minimized top-level Opera GX browser windows."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    GW_OWNER = 4
    GWL_EXSTYLE = -20
    WS_EX_TOOLWINDOW = 0x00000080
    found: set[int] = set()

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _enum(hwnd: wintypes.HWND, _lp: wintypes.LPARAM) -> bool:
        if user32.GetWindow(hwnd, GW_OWNER):
            return True
        if user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW:
            return True
        if not user32.IsWindowVisible(hwnd) and not user32.IsIconic(hwnd):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == 0:
            return True
        hproc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not hproc:
            return True
        try:
            buf = ctypes.create_unicode_buffer(4096)
            sz = wintypes.DWORD(len(buf))
            if not kernel32.QueryFullProcessImageNameW(hproc, 0, buf, ctypes.byref(sz)):
                return True
            exe_path = buf.value
        finally:
            kernel32.CloseHandle(hproc)
        if os.path.basename(exe_path).lower() != "opera.exe":
            return True
        r = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
            return True
        w, h = r.right - r.left, r.bottom - r.top
        if w < 80 or h < 80:
            return True
        found.add(int(hwnd))
        return True

    user32.EnumWindows(_enum, 0)
    return found


def _wait_new_opera_hwnd_win32(before: set[int], timeout: float) -> int | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(0.12)
        now = _opera_top_level_hwnds_win32()
        new = now - before
        if not new:
            continue
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        best: int | None = None
        best_area = 0
        for h in new:
            r = wintypes.RECT()
            if user32.GetWindowRect(h, ctypes.byref(r)):
                a = max(0, r.right - r.left) * max(0, r.bottom - r.top)
                if a > best_area:
                    best_area = a
                    best = h
        if best is not None:
            return best
    return None


def _browser_snap_window_to_monitor_win32(
    hwnd: int,
    one_based_monitor: int,
    *,
    fullscreen: bool,
    windowed_size: tuple[int, int] | None,
) -> None:
    import ctypes
    from ctypes import wintypes

    ml, mt, mr, mb = _browser_monitor_bounds(one_based_monitor)
    user32 = ctypes.windll.user32
    SW_RESTORE = 9
    SW_SHOWMAXIMIZED = 3
    HWND_TOP = 0
    SWP_SHOWWINDOW = 0x0040
    SWP_FRAMECHANGED = 0x0020
    flags = SWP_SHOWWINDOW | SWP_FRAMECHANGED

    user32.ShowWindow(hwnd, SW_RESTORE)
    if fullscreen:
        w, h = mr - ml, mb - mt
        x, y = ml, mt
    else:
        ww, wh = windowed_size or _browser_window_size()
        w, h = ww, wh
        x = ml + max(0, (mr - ml - w) // 2)
        y = mt + max(0, (mb - mt - h) // 2)
    user32.SetWindowPos(hwnd, HWND_TOP, x, y, w, h, flags)

    if fullscreen:
        user32.ShowWindow(hwnd, SW_SHOWMAXIMIZED)
        KEYEVENTF_KEYUP = 0x0002
        VK_F11 = 0x7A
        fg = user32.GetForegroundWindow()
        tid_tgt = user32.GetWindowThreadProcessId(hwnd, None)
        tid_fg = user32.GetWindowThreadProcessId(fg, None) if fg else 0
        if tid_fg and tid_tgt:
            user32.AttachThreadInput(tid_fg, tid_tgt, True)
        user32.SetForegroundWindow(hwnd)
        if tid_fg and tid_tgt:
            user32.AttachThreadInput(tid_fg, tid_tgt, False)
        user32.keybd_event(VK_F11, 0, 0, 0)
        user32.keybd_event(VK_F11, 0, KEYEVENTF_KEYUP, 0)


def _open_url_in_opera_gx(
    url: str,
    *,
    new_window: bool = True,
    label: str = "URL",
    window_position: tuple[int, int] | None = None,
    window_size: tuple[int, int] | None = None,
    fullscreen: bool = False,
    win32_post_fullscreen_monitor: int | None = None,
    user_data_dir: str | None = None,
) -> None:
    u = url.strip()
    if not u:
        return
    opera = _opera_gx_executable()
    try:
        if opera:
            args = [opera]
            if user_data_dir:
                args.append(f"--user-data-dir={user_data_dir}")
                args.append("--no-first-run")
            if new_window:
                args.append("--new-window")
            if window_position is not None:
                x, y = window_position
                args.append(f"--window-position={x},{y}")
            if window_size:
                args.append(f"--window-size={window_size[0]},{window_size[1]}")
            if fullscreen and not (
                sys.platform == "win32" and win32_post_fullscreen_monitor is not None
            ):
                args.append("--start-fullscreen")
            args.append(u)
            popen_kw: dict = {
                "args": args,
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
            }
            if sys.platform == "win32":
                popen_kw["creationflags"] = subprocess.CREATE_NO_WINDOW
            before: set[int] | None = None
            if sys.platform == "win32" and win32_post_fullscreen_monitor is not None:
                before = _opera_top_level_hwnds_win32()
            subprocess.Popen(**popen_kw)
            if sys.platform == "win32" and win32_post_fullscreen_monitor is not None:
                mon = win32_post_fullscreen_monitor
                hwnd = _wait_new_opera_hwnd_win32(before, _browser_new_window_wait_timeout_s())
                if hwnd is not None:
                    _browser_snap_window_to_monitor_win32(
                        hwnd,
                        mon,
                        fullscreen=fullscreen,
                        windowed_size=window_size if not fullscreen else None,
                    )
                else:
                    log.warning(
                        "Opera GX: timed out waiting for new window (%s); check "
                        "BROWSER_NEW_WINDOW_WAIT_S or close extra Opera GX instances.",
                        label,
                    )
        else:
            log.warning("Opera GX not found; opening %s in default browser.", label)
            webbrowser.open(u)
    except OSError as e:
        log.warning("Could not open %s in Opera GX: %s", label, e)


def open_claude_in_opera_gx() -> None:
    if not OPEN_CLAUDE_IN_BROWSER:
        return
    url = (os.environ.get("CLAUDE_CODE_URL") or "https://claude.ai/new").strip()
    pos: tuple[int, int] | None = None
    size: tuple[int, int] | None = None
    fs = OPEN_BROWSER_FULLSCREEN
    post_mon: int | None = None
    user_data: str | None = None
    if sys.platform == "win32":
        post_mon = CLAUDE_BROWSER_MONITOR
        pos = _browser_monitor_top_left(CLAUDE_BROWSER_MONITOR)
        if fs:
            size = _browser_monitor_pixel_size(CLAUDE_BROWSER_MONITOR)
        else:
            size = _browser_window_size()
        if BROWSER_SEPARATE_SITE_PROFILES:
            user_data = _browser_site_user_data_dir("claude")
    elif not fs:
        size = _browser_window_size()
    else:
        size = None
    _open_url_in_opera_gx(
        url,
        new_window=True,
        label="Claude",
        window_position=pos,
        window_size=size,
        fullscreen=fs,
        win32_post_fullscreen_monitor=post_mon,
        user_data_dir=user_data,
    )


def _cursor_executable() -> str | None:
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "")
        for sub in ("Programs\\cursor\\Cursor.exe", "Programs\\Cursor\\Cursor.exe"):
            if local:
                p = os.path.join(local, *sub.split("\\"))
                if os.path.isfile(p):
                    return p
    return shutil.which("cursor")


def _cursor_largest_main_hwnd_win32() -> int | None:
    """Largest top-level Cursor.exe window (visible or minimized)."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    GW_OWNER = 4
    GWL_EXSTYLE = -20
    WS_EX_TOOLWINDOW = 0x00000080
    candidates: list[tuple[int, wintypes.HWND]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _enum(hwnd: wintypes.HWND, _lp: wintypes.LPARAM) -> bool:
        if user32.GetWindow(hwnd, GW_OWNER):
            return True
        if user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW:
            return True
        if not user32.IsWindowVisible(hwnd) and not user32.IsIconic(hwnd):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == 0:
            return True
        hproc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not hproc:
            return True
        try:
            buf = ctypes.create_unicode_buffer(4096)
            sz = wintypes.DWORD(len(buf))
            if not kernel32.QueryFullProcessImageNameW(hproc, 0, buf, ctypes.byref(sz)):
                return True
            exe_path = buf.value
        finally:
            kernel32.CloseHandle(hproc)
        if os.path.basename(exe_path).lower() != "cursor.exe":
            return True
        r = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
            return True
        w, h = r.right - r.left, r.bottom - r.top
        if w < 200 or h < 200:
            return True
        candidates.append((w * h, hwnd))
        return True

    user32.EnumWindows(_enum, 0)
    if not candidates:
        return None
    return int(max(candidates, key=lambda t: t[0])[1])


def _cursor_foreground_hwnd_win32(hwnd: int) -> None:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    SW_RESTORE = 9
    user32.ShowWindow(hwnd, SW_RESTORE)
    fg = user32.GetForegroundWindow()
    tid_tgt = user32.GetWindowThreadProcessId(hwnd, None)
    tid_fg = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    if tid_fg and tid_tgt:
        user32.AttachThreadInput(tid_fg, tid_tgt, True)
    user32.SetForegroundWindow(hwnd)
    if tid_fg and tid_tgt:
        user32.AttachThreadInput(tid_fg, tid_tgt, False)


def _cursor_send_f11_fullscreen_win32(hwnd: int) -> None:
    """F11 toggles Zen/fullscreen in Cursor (Electron)."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    KEYEVENTF_KEYUP = 0x0002
    VK_F11 = 0x7A
    _cursor_foreground_hwnd_win32(hwnd)
    user32.keybd_event(VK_F11, 0, 0, 0)
    user32.keybd_event(VK_F11, 0, KEYEVENTF_KEYUP, 0)


def _focus_existing_cursor_window_win32() -> bool:
    """Bring an existing Cursor.exe main window to the foreground (no new process)."""
    if sys.platform != "win32":
        return False
    hwnd = _cursor_largest_main_hwnd_win32()
    if hwnd is None:
        return False
    _cursor_foreground_hwnd_win32(hwnd)
    return True


def run_wake_word_actions() -> None:
    """Run outside the mic loop so sleeps do not stall capture."""
    play_song(SONG_URI)
    open_claude_in_opera_gx()
    if JARVIS_WELCOME_ENABLED and JARVIS_WELCOME_PHRASE.strip():
        delay = max(0.0, JARVIS_AFTER_SONG_DELAY_S)
        if delay:
            time.sleep(delay)
        threading.Thread(target=say_jarvis_welcome, daemon=True).start()
    open_cursor_window()


def open_cursor_window() -> None:
    if not FOCUS_EXISTING_CURSOR_ON_WAKE_WORD and not OPEN_NEW_CURSOR_ON_WAKE_WORD:
        return
    exe = _cursor_executable()
    if not exe:
        log.warning(
            "Could not find Cursor (install app or add the `cursor` command to PATH)."
        )
        return
    popen_kw: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        popen_kw["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        if FOCUS_EXISTING_CURSOR_ON_WAKE_WORD:
            focused = (
                sys.platform == "win32" and _focus_existing_cursor_window_win32()
            )
            if not focused:
                subprocess.Popen([exe], **popen_kw)
        if OPEN_NEW_CURSOR_ON_WAKE_WORD:
            subprocess.Popen([exe, "-n"], **popen_kw)
    except OSError as e:
        log.warning("Could not start or focus Cursor: %s", e)
        return
    if sys.platform == "win32" and CURSOR_OPEN_FULLSCREEN:
        time.sleep(0.5)
        hwnd = _cursor_largest_main_hwnd_win32()
        if hwnd is not None:
            _cursor_send_f11_fullscreen_win32(hwnd)
        else:
            log.warning("Cursor fullscreen: no Cursor window found to send F11.")


def _normalize_for_match(text: str) -> str:
    """Lowercase and strip accents so 'Jarvis'/'jarvis'/'JÁRVIS' all match."""
    import unicodedata

    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()


_WAKE_WORD_NORM_RE = re.compile(re.escape(_normalize_for_match(WAKE_WORD)))

# Shared across recognition threads: monotonic deadline until which the next
# utterance (even with no wake word in it) is treated as a follow-up question.
_listen_for_question_until = 0.0
_listen_for_question_lock = threading.Lock()


def _extract_query_after_wake_word(text: str) -> str:
    """Whatever follows WAKE_WORD in the same utterance, e.g. 'jarvis, que dia
    é hoje?' -> 'que dia é hoje?'. Empty string if it's just the wake word.

    Matches against the accent-stripped text (same normalization as
    has_wake_word in _process_wake_utterance), so an accented mishear like
    'Járvis, que dia é hoje?' still has its question extracted correctly
    instead of silently falling back to the bare-wake-word (welcome-only)
    path. NFKD decomposition + combining-mark removal preserves character
    offsets for single-codepoint Latin accents (á, é, ã, ç, ...), so indices
    found in the normalized string can be used directly on the original
    text."""
    normalized = _normalize_for_match(text)
    m = _WAKE_WORD_NORM_RE.search(normalized)
    if not m:
        return ""
    return text[m.end():].strip(" ,.!?-–—")


def _arm_followup_window() -> None:
    global _listen_for_question_until
    with _listen_for_question_lock:
        _listen_for_question_until = time.monotonic() + JARVIS_FOLLOWUP_WINDOW_S


def _consume_followup_window() -> bool:
    """True (and disarms) if we're still within the post-'jarvis' window."""
    global _listen_for_question_until
    now = time.monotonic()
    with _listen_for_question_lock:
        armed = now <= _listen_for_question_until
        _listen_for_question_until = 0.0
    return armed


def _process_wake_utterance(
    frames: list[np.ndarray], wake_event: threading.Event
) -> None:
    """Runs in a background thread: transcribe the utterance. A question after
    WAKE_WORD in the same breath ('jarvis, que dia é hoje?') is answered right
    away. A bare WAKE_WORD triggers the fixed welcome sequence (once per
    process) and then keeps listening for JARVIS_FOLLOWUP_WINDOW_S seconds —
    the *next* utterance, even without the wake word, is treated as the
    question (e.g. say "jarvis", pause, then "que dia é hoje?")."""
    pcm = np.concatenate(frames) if frames else np.array([], dtype=np.float32)
    pcm_i16 = np.clip(pcm * 32767.0, -32768, 32767).astype(np.int16)
    audio = sr.AudioData(pcm_i16.tobytes(), SAMPLE_RATE, 2)

    recognizer = sr.Recognizer()
    try:
        text = recognizer.recognize_google(audio, language=WAKE_LANGUAGE)
    except sr.UnknownValueError:
        log.info("Utterance not understood; ignoring.")
        hud_state("idle")
        return
    except sr.RequestError as e:
        log.warning("Speech recognition request failed: %s", e)
        hud_state("idle")
        return

    log.info("Heard: %r", text)
    hud_heard(text)
    has_wake_word = _normalize_for_match(WAKE_WORD) in _normalize_for_match(text)

    if not has_wake_word:
        if _consume_followup_window():
            log.info("Treating as follow-up question (no need to repeat %r): %r", WAKE_WORD, text)
            hud_state("thinking")
            threading.Thread(
                target=_handle_jarvis_question, args=(text,), daemon=True
            ).start()
        else:
            hud_state("idle")
        return

    query = _extract_query_after_wake_word(text)
    if query:
        log.info("Wake word %r detected with question: %r", WAKE_WORD, query)
        hud_state("thinking")
        threading.Thread(
            target=_handle_jarvis_question, args=(query,), daemon=True
        ).start()
        return

    # Bare wake word: open the follow-up window regardless of whether the
    # welcome sequence below has already run once.
    _arm_followup_window()

    if wake_event.is_set():
        log.info("Wake word %r heard again in %r — welcome already ran, skipping actions.", WAKE_WORD, text)
        hud_state("idle")
        return
    wake_event.set()
    log.info("Wake word %r detected in %r — running welcome once.", WAKE_WORD, text)
    hud_state("thinking")
    run_wake_word_actions()


def open_orb_visual() -> None:
    """Opens jarvis_orb.html in the default browser, if present next to this
    script. Best-effort only — never raises, just logs a warning."""
    if not ORB_VISUAL_HTML_PATH.is_file():
        log.warning(
            "OPEN_ORB_VISUAL_ON_START is True but %s was not found; skipping.",
            ORB_VISUAL_HTML_PATH,
        )
        return
    try:
        webbrowser.open(ORB_VISUAL_HTML_PATH.resolve().as_uri())
        log.info("Opened the orb visual: %s", ORB_VISUAL_HTML_PATH)
    except OSError as e:
        log.warning("Could not open the orb visual: %s", e)


def main() -> int:
    blocksize = block_samples()
    wake_event = threading.Event()

    speaking = False
    utterance_frames: list[np.ndarray] = []
    utterance_start = 0.0
    last_voice_time = 0.0
    last_recognition_attempt = 0.0

    if JARVIS_HUD_ENABLED:
        threading.Thread(target=_hud_start_server, daemon=True).start()
        _hud_ready.wait(timeout=2.0)  # best-effort: let the server bind before the page opens
        if _hud_ready.is_set():
            log.info("HUD server listening on ws://127.0.0.1:%d", JARVIS_HUD_PORT)

    if OPEN_ORB_VISUAL_ON_START:
        open_orb_visual()

    log.info(
        "Listening for the wake word %r (language=%s, rate=%d, block=%d ms, "
        "min_rms=%.4f, silence_hang=%.2fs). Ctrl+C to stop.",
        WAKE_WORD,
        WAKE_LANGUAGE,
        SAMPLE_RATE,
        BLOCK_MS,
        WAKE_MIN_RMS,
        WAKE_SILENCE_HANG_S,
    )
    if SONG_URI.strip():
        log.info("wake word opens this track: %s", SONG_URI.strip())
    else:
        log.info("SONG_URI is empty — set it to play one song on each wake word.")
    if FOCUS_EXISTING_CURSOR_ON_WAKE_WORD:
        log.info(
            "wake word will foreground an existing Cursor window (Windows API); "
            "falls back to launching Cursor if none is running."
        )
    if OPEN_NEW_CURSOR_ON_WAKE_WORD:
        log.info("wake word will also open a new Cursor window (-n).")
    if CURSOR_OPEN_FULLSCREEN and sys.platform == "win32":
        log.info("Cursor will be sent F11 for fullscreen after focus/launch.")
    if OPEN_CLAUDE_IN_BROWSER:
        cu = (os.environ.get("CLAUDE_CODE_URL") or "https://claude.ai/new").strip()
        log.info(
            "After the song, open Claude in Opera GX%s on monitor %d: %s",
            " fullscreen" if OPEN_BROWSER_FULLSCREEN else "",
            CLAUDE_BROWSER_MONITOR,
            cu,
        )
    if JARVIS_WELCOME_ENABLED:
        ev, em, ef, er = elevenlabs_env_config()
        log.info(
            "After song + %.2fs: %r (TTS engine=%s first, edge-tts voice=%s, "
            "ElevenLabs voice=%s/model=%s/format=%s/pcm_rate=%d as fallback)",
            JARVIS_AFTER_SONG_DELAY_S,
            JARVIS_WELCOME_PHRASE.strip(),
            JARVIS_TTS_ENGINE,
            EDGE_TTS_VOICE,
            ev or "(unset)",
            em,
            ef,
            er,
        )
    if JARVIS_ASSISTANT_ENABLED:
        log.info(
            "Say %r followed by a question (e.g. %r, que dia é hoje?) to get a "
            "spoken answer (model=%s, via Groq's free API). Requires GROQ_API_KEY.",
            WAKE_WORD,
            WAKE_WORD,
            JARVIS_ASSISTANT_MODEL,
        )
        if JARVIS_APP_CONTROL_ENABLED and sys.platform == "win32":
            log.info(
                "Also say %r + 'abre o <app>' / 'fecha o <app>' to open or close apps."
                " Known aliases: %s",
                WAKE_WORD,
                ", ".join(sorted(APP_ALIASES)),
            )
        elif JARVIS_APP_CONTROL_ENABLED:
            log.info("App open/close is enabled but only implemented for Windows.")
        if JARVIS_EMAIL_ENABLED:
            if os.environ.get("EMAIL_ADDRESS") and os.environ.get("EMAIL_APP_PASSWORD"):
                log.info(
                    "Also say %r + 'lê meus emails' to hear recent inbox mail "
                    "(spam/newsletters skipped automatically).",
                    WAKE_WORD,
                )
            else:
                log.info(
                    "Email reading is enabled but EMAIL_ADDRESS/EMAIL_APP_PASSWORD "
                    "are not set — set them in .env to use it."
                )

    input_idx = _choose_input_device(blocksize)

    last_level_log = 0.0
    level_log_peak = 0.0

    try:
        with sd.InputStream(
            device=input_idx,
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="float32",
            blocksize=blocksize,
        ) as stream:
            while True:
                data, overflowed = stream.read(blocksize)
                if overflowed:
                    log.warning("Input overflow; try a larger BLOCK_MS")

                level = rms_mono(data)
                now = time.monotonic()
                block = data.reshape(-1).copy()

                level_log_peak = max(level_log_peak, level)
                if now - last_level_log >= 3.0:
                    log.info(
                        "mic level check: peak rms in last 3s = %.5f (WAKE_MIN_RMS = %.5f) — %s",
                        level_log_peak,
                        WAKE_MIN_RMS,
                        "crossed threshold" if level_log_peak >= WAKE_MIN_RMS else "stayed below threshold",
                    )
                    level_log_peak = 0.0
                    last_level_log = now

                if level >= WAKE_MIN_RMS:
                    if not speaking:
                        speaking = True
                        utterance_start = now
                        utterance_frames = []
                    utterance_frames.append(block)
                    last_voice_time = now
                elif speaking:
                    utterance_frames.append(block)
                    silence_for = now - last_voice_time
                    too_long = (now - utterance_start) >= WAKE_MAX_UTTERANCE_S
                    if silence_for >= WAKE_SILENCE_HANG_S or too_long:
                        speaking = False
                        duration = now - utterance_start
                        frames_snapshot = utterance_frames
                        utterance_frames = []
                        if (
                            duration >= WAKE_MIN_UTTERANCE_S
                            and (now - last_recognition_attempt) >= WAKE_COOLDOWN_S
                        ):
                            last_recognition_attempt = now
                            hud_state("thinking")
                            threading.Thread(
                                target=_process_wake_utterance,
                                args=(frames_snapshot, wake_event),
                                daemon=True,
                            ).start()

    except KeyboardInterrupt:
        log.info("Stopped.")
        return 0
    except sd.PortAudioError as e:
        log.error("Audio error: %s", e)
        log.error("If PortAudio fails, install/repair drivers or try another SAMPLE_RATE.")
        return 1

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        log.exception("Unhandled error — Jarvis stopped. See jarvis.log.")
        sys.exit(1)

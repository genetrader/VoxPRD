"""Voice Hotkey — local-first dictation + Discord poster.

System tray app that:
  1. Listens for a global hotkey (default: pagedown; pause/break aborts).
  2. Records audio from the default mic with a live waveform overlay.
  3. Transcribes via a configured provider chain
     (fleet Whisper -> OpenAI Whisper -> local openai-whisper).
  4. Shows a Send / Copy / PRD choice overlay.
  5. Routes the message to the right Discord channel via webhook.

PRD generation uses its own provider chain (DeepSeek V4 Flash -> GLM/GRM
local -> OpenAI).

State is split:
  - Source (committed): this file, config.json, prompts.py, providers.py,
    secrets.py, .gitignore.
  - Secrets (gitignored): secrets/.env (preferred) or ./.env (legacy).
  - Runtime state (gitignored): state/recordings/, state/last_*.txt,
    state/prd_*.txt, state/voice_hotkey_errors.log.

Google Remote Desktop notes: scroll_lock / pause / ctrl+space pass through
the GRD filter; ctrl+alt+v does not. We register the primary hotkey via
Win32 RegisterHotKey and fall back to the `keyboard` library if that fails.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import keyboard

try:
    import mouse
except ImportError:
    mouse = None  # mouse-button hotkeys need the `mouse` package
import numpy as np
import pystray
import requests
import sounddevice as sd
import soundfile as sf
import tkinter as tk
from tkinter import ttk
from PIL import Image, ImageDraw

from providers import generate_prd, transcribe_with_retry
from prompts import PRD_SYSTEM_PROMPT
from secrets import get as get_secret, get_required

# ---------------------------------------------------------------------------
# Paths (absolute — works from any cwd)
# ---------------------------------------------------------------------------
APP_DIR = Path(__file__).resolve().parent
SECRETS_DIR = APP_DIR / "secrets"
STATE_DIR = APP_DIR / "state"
RECORDINGS_DIR = STATE_DIR / "recordings"
PRD_ARCHIVE_DIR = STATE_DIR / "prd_archive"
ERROR_LOG_PATH = STATE_DIR / "voice_hotkey_errors.log"
LAST_TRANSCRIPTION_PATH = STATE_DIR / "last_transcription.txt"
LAST_PRD_PATH = STATE_DIR / "last_prd.txt"
CONFIG_PATH = APP_DIR / "config.json"

# Legacy paths (top-level). Used only to migrate old state on first run.
_LEGACY_RECORDINGS = APP_DIR / "recordings"
_LEGACY_LAST_TX = APP_DIR / "last_transcription.txt"
_LEGACY_LAST_PRD = APP_DIR / "last_prd.txt"
_LEGACY_LOG = APP_DIR / "voice_hotkey_errors.log"


def _ensure_dirs() -> None:
    for d in (STATE_DIR, RECORDINGS_DIR, PRD_ARCHIVE_DIR):
        d.mkdir(parents=True, exist_ok=True)


def _migrate_legacy_state() -> None:
    """One-shot: move legacy top-level state into state/. Idempotent."""
    pairs = [
        (_LEGACY_RECORDINGS, RECORDINGS_DIR),
        (_LEGACY_LAST_TX, LAST_TRANSCRIPTION_PATH),
        (_LEGACY_LAST_PRD, LAST_PRD_PATH),
        (_LEGACY_LOG, ERROR_LOG_PATH),
    ]
    for src, dst in pairs:
        if not src.exists():
            continue
        try:
            if src.is_dir():
                # Merge directory contents
                dst.mkdir(parents=True, exist_ok=True)
                for child in src.iterdir():
                    target = dst / child.name
                    if not target.exists():
                        shutil.move(str(child), str(target))
                # Remove now-empty source
                try: src.rmdir()
                except OSError: pass
            else:
                if not dst.exists():
                    shutil.move(str(src), str(dst))
                else:
                    src.unlink()
        except OSError:
            pass


_ensure_dirs()
_migrate_legacy_state()


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


try:
    CONFIG = load_config()
except Exception as _e:
    try:
        ctypes.windll.user32.MessageBoxW(
            0,
            f"Failed to load config.json:\n{_e}",
            "Voice Hotkey - Config Error",
            0x10,  # MB_ICONERROR
        )
    except Exception:
        pass
    sys.exit(1)


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------
def _read_bot_token() -> str | None:
    # Archived 2026-09-03: the Discord relay existed to reach agents on a
    # different machine. The agents now run locally, so no token is loaded
    # and send_to_discord() is never called. secrets/.env may still contain
    # DISCORD_BOT_TOKEN — it is ignored (safe to delete that line).
    return None


BOT_TOKEN = _read_bot_token()  # None is OK — webhook may still work
DISCORD_API = "https://discord.com/api/v10"


def _load_webhook_url() -> str | None:
    webhook_file = APP_DIR / "webhooks.json"
    if not webhook_file.exists():
        return None
    try:
        data = json.loads(webhook_file.read_text(encoding="utf-8"))
        url = data.get("general")
        return url if isinstance(url, str) and url else None
    except (OSError, json.JSONDecodeError):
        return None


WEBHOOK_URL = None  # Discord relay archived 2026-09-03 — webhooks.json ignored (see _read_bot_token)


def get_openai_key() -> str:
    return get_secret("OPENAI_API_KEY") or ""


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def log_error(message: str, audio_path: str | Path | None = None) -> None:
    """Persist background-thread errors because tray toasts are easy to miss."""
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {message}"
    try:
        with ERROR_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    if audio_path:
        try:
            Path(audio_path).with_suffix(".error.txt").write_text(line + "\n", encoding="utf-8")
        except OSError:
            pass


# ---------------------------------------------------------------------------
# File rotation (cheap, on-write)
# ---------------------------------------------------------------------------
_KEEP_DAYS_RECORDINGS = 30
_KEEP_COUNT_PRD = 50


def rotate_recordings_if_needed() -> None:
    """Prune recordings older than KEEP_DAYS. Best-effort, never raises."""
    try:
        cutoff = time.time() - _KEEP_DAYS_RECORDINGS * 86400
        for p in RECORDINGS_DIR.iterdir():
            try:
                if p.is_file() and p.stat().st_mtime < cutoff:
                    p.unlink()
            except OSError:
                continue
    except OSError:
        pass


def rotate_prd_archive_if_needed() -> None:
    """Keep only the last N PRD archive files. Best-effort."""
    try:
        files = sorted(
            (p for p in PRD_ARCHIVE_DIR.glob("prd_*.txt")),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for old in files[_KEEP_COUNT_PRD:]:
            try: old.unlink()
            except OSError: pass
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Channel routing
# ---------------------------------------------------------------------------
def route_message(text: str) -> dict:
    """Return the matching rule dict, or default_channel if none match.

    First-match-wins. The config comment in config.json.template says so;
    keep this in sync if you change the rules engine.
    """
    text_lower = text.lower()
    for rule in CONFIG.get("routing_rules", []):
        for kw in rule.get("keywords", []):
            if kw.lower() in text_lower:
                return rule
    return CONFIG["default_channel"]


# ---------------------------------------------------------------------------
# Discord send
# ---------------------------------------------------------------------------
def discord_headers() -> dict:
    return {
        "Authorization": f"Bot {BOT_TOKEN or ''}",
        "User-Agent": "VoiceHotkey/1.1",
    }


def _format_memo(text: str) -> str:
    return f"**Voice memo** ({len(text.split())} words):\n> {text}"


def send_to_discord(channel_id: str, text: str, audio_path: str | None = None,
                    target_agent: str | None = None) -> bool:
    """Send a message (with optional audio attachment) to a Discord channel.

    Prefers webhook (so the message appears from 'VoxPRD Voice' instead of
    the bot itself, which makes downstream agents actually respond to it).
    Falls back to bot token if no webhook configured.

    `target_agent` is included in the Discord content so the receiving agent
    can route based on intent (bastion, screenplay, fleet, general, ...).
    """
    if not BOT_TOKEN and not WEBHOOK_URL:
        notify("No Discord credentials — copied to clipboard instead", "Voice Hotkey")
        try:
            _copy_to_clipboard(text)
        except Exception:
            pass
        return False

    formatted = _format_memo(text)
    if target_agent and target_agent != "general":
        formatted = f"@`{target_agent}` {formatted}"

    if WEBHOOK_URL:
        try:
            if audio_path and os.path.isfile(audio_path):
                with open(audio_path, "rb") as af:
                    files = {"file": ("voice_memo.wav", af, "audio/wav")}
                    data = {"content": formatted, "username": "VoxPRD Voice"}
                    resp = requests.post(WEBHOOK_URL, data=data, files=files, timeout=30)
            else:
                resp = requests.post(
                    WEBHOOK_URL,
                    json={"content": formatted, "username": "VoxPRD Voice"},
                    timeout=15,
                )
            resp.raise_for_status()
            return True
        except requests.RequestException as e:
            notify(f"Webhook error, falling back to bot: {e}", "Voice Hotkey")

    url = f"{DISCORD_API}/channels/{channel_id}/messages"
    payload = {"content": formatted}
    try:
        if audio_path and os.path.isfile(audio_path):
            with open(audio_path, "rb") as af:
                files = {"file": ("voice_memo.wav", af, "audio/wav")}
                data = {"payload_json": json.dumps(payload)}
                resp = requests.post(url, headers=discord_headers(), data=data, files=files, timeout=30)
        else:
            resp = requests.post(url, headers=discord_headers(), json=payload, timeout=15)
        resp.raise_for_status()
        return True
    except requests.RequestException as e:
        notify(f"Discord error: {e}", "Voice Hotkey Error")
        return False


# ---------------------------------------------------------------------------
# Hotkey parsing
# ---------------------------------------------------------------------------
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
WM_HOTKEY = 0x0312
_HOTKEY_ID = 1
_ABORT_HOTKEY_ID = 2

VK_BACKSPACE = 0x08
VK_TAB = 0x09
VK_RETURN = 0x0D
VK_PAUSE = 0x13
VK_CAPSLOCK = 0x14
VK_ESCAPE = 0x1B
VK_SPACE = 0x20
VK_PRIOR = 0x21  # page up
VK_NEXT = 0x22   # page down
VK_END = 0x23
VK_HOME = 0x24
VK_LEFT = 0x25
VK_UP = 0x26
VK_RIGHT = 0x27
VK_DOWN = 0x28
VK_INSERT = 0x2D
VK_DELETE = 0x2E
VK_NUMPAD0 = 0x60
VK_NUMPAD9 = 0x69
VK_SCROLL = 0x91
VK_F1 = 0x70


_SPECIAL_HOTKEYS = {
    "backspace": VK_BACKSPACE,
    "tab": VK_TAB,
    "enter": VK_RETURN, "return": VK_RETURN,
    "pause": VK_PAUSE, "break": VK_PAUSE,
    "caps_lock": VK_CAPSLOCK, "capslock": VK_CAPSLOCK,
    "escape": VK_ESCAPE, "esc": VK_ESCAPE,
    "space": VK_SPACE,
    "page_up": VK_PRIOR, "pageup": VK_PRIOR,
    "page_down": VK_NEXT, "pagedown": VK_NEXT,
    "end": VK_END, "home": VK_HOME,
    "left": VK_LEFT, "up": VK_UP, "right": VK_RIGHT, "down": VK_DOWN,
    "insert": VK_INSERT, "ins": VK_INSERT,
    "delete": VK_DELETE, "del": VK_DELETE,
    "scroll_lock": VK_SCROLL, "scrolllock": VK_SCROLL,
    "numpad0": VK_NUMPAD0, "numpad1": VK_NUMPAD0 + 1, "numpad2": VK_NUMPAD0 + 2,
    "numpad3": VK_NUMPAD0 + 3, "numpad4": VK_NUMPAD0 + 4, "numpad5": VK_NUMPAD0 + 5,
    "numpad6": VK_NUMPAD0 + 6, "numpad7": VK_NUMPAD0 + 7, "numpad8": VK_NUMPAD0 + 8,
    "numpad9": VK_NUMPAD9,
}


def parse_hotkey(hotkey_str: str) -> tuple[int, int]:
    """Convert a config hotkey string (e.g. 'ctrl+alt+v') to (modifiers, vk).

    Returns (0, 0) if the string is unparseable. Callers should treat that
    as a registration failure.
    """
    parts = [p.strip().lower() for p in hotkey_str.split("+")]
    mods = 0
    vk = 0
    for p in parts:
        if p in ("ctrl", "control"):
            mods |= MOD_CONTROL
        elif p == "alt":
            mods |= MOD_ALT
        elif p == "shift":
            mods |= MOD_SHIFT
        elif p in ("win", "super", "meta"):
            mods |= MOD_WIN
        elif p in _SPECIAL_HOTKEYS:
            vk = _SPECIAL_HOTKEYS[p]
        elif len(p) == 1:
            try:
                vk = ctypes.windll.user32.VkKeyScanW(ord(p.upper())) & 0xFF
            except Exception:
                vk = 0
        elif p.startswith("f") and p[1:].isdigit():
            vk = VK_F1 + int(p[1:]) - 1
        else:
            return 0, 0
    return mods, vk


def register_system_hotkey(mods: int, vk: int, hotkey_id: int = _HOTKEY_ID) -> bool:
    return bool(ctypes.windll.user32.RegisterHotKey(None, hotkey_id, mods, vk))


def unregister_system_hotkey(hotkey_id: int | None = None) -> None:
    if hotkey_id is None:
        ctypes.windll.user32.UnregisterHotKey(None, _HOTKEY_ID)
        ctypes.windll.user32.UnregisterHotKey(None, _ABORT_HOTKEY_ID)
    else:
        ctypes.windll.user32.UnregisterHotKey(None, hotkey_id)


# ---------------------------------------------------------------------------
# Hotkey normalization (keyboard combos + mouse buttons)
# ---------------------------------------------------------------------------
_MOUSE_BUTTON_NAMES = {
    "left": "Left Mouse Button",
    "middle": "Middle Mouse Button",
    "right": "Right Mouse Button",
    "x": "Mouse Button X1 (side/back)",
    "x2": "Mouse Button X2 (side/forward)",
}

# True while the hotkey picker is listening; triggers must stand by so the
# captured press doesn't also start a recording.
_picker_capturing = [False]

# True when the primary STT endpoint answered the last health probe.
_stt_healthy = [True]


def hotkey_is_mouse(hotkey_str: str) -> bool:
    return isinstance(hotkey_str, str) and hotkey_str.lower().startswith("mouse:")


def mouse_button_of(hotkey_str: str) -> str | None:
    if not hotkey_is_mouse(hotkey_str):
        return None
    btn = hotkey_str.split(":", 1)[1].strip().lower()
    return btn if btn in _MOUSE_BUTTON_NAMES else None


def canonical_hotkey(hotkey_str: str) -> str | None:
    """Normalize a captured hotkey to config format, or None if unusable.

    Accepts keyboard-library names ('page down', 'ctrl+alt+v') and bare
    mouse-button names ('middle', 'x2'). Rejects modifier-only combos —
    a hotkey with no trigger key would never fire.
    """
    s = hotkey_str.strip().lower()
    if s in _MOUSE_BUTTON_NAMES:
        return f"mouse:{s}"
    parts = []
    for p in s.split("+"):
        p = p.strip().replace(" ", "_")
        if p == "control":
            p = "ctrl"
        elif p in ("windows", "super", "meta"):
            p = "win"
        parts.append(p)
    if "win" in parts:
        return None  # OS-reserved: win+key combos open shell apps (win+a etc.)
    candidate = "+".join(parts)
    _, vk = parse_hotkey(candidate)
    return candidate if vk else None


def hotkey_display(hotkey_str: str) -> str:
    """Human label for a config hotkey string."""
    if hotkey_is_mouse(hotkey_str):
        btn = mouse_button_of(hotkey_str)
        if btn:
            return _MOUSE_BUTTON_NAMES[btn]
    return str(hotkey_str).replace("_", " ").upper()


def _discover_models(base_url: str, timeout: int = 6) -> list[str]:
    """GET {base}/v1/models on an OpenAI-compatible server → sorted model ids.

    Accepts bare host:port, http(s)://host:port, or a URL already ending
    in /v1 — all normalize the same way.
    """
    url = base_url.strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    if url.endswith("/v1"):
        url = url[:-3]
    resp = requests.get(f"{url}/v1/models", timeout=timeout)
    resp.raise_for_status()
    data = resp.json().get("data", [])
    return sorted(m.get("id", "") for m in data if m.get("id"))


def _discover_stt(base_url: str, timeout: int = 6) -> tuple[list[str], str]:
    """Probe a speech-to-text server. Returns (models, source).

    OpenAI-compatible servers (Speaches etc.) list models at /v1/models;
    plain faster-whisper wrappers often only serve /health with a JSON
    body containing the model name. Raises if neither answers.
    """
    url = base_url.strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    try:
        resp = requests.get(f"{url}/v1/models", timeout=timeout)
        resp.raise_for_status()
        data = resp.json().get("data", [])
        models = sorted(m.get("id", "") for m in data if m.get("id"))
        if models:
            return models, "models"
    except (requests.RequestException, ValueError):
        pass
    resp = requests.get(f"{url}/health", timeout=timeout)
    resp.raise_for_status()
    try:
        info = resp.json()
    except ValueError:
        return ["whisper-1"], "health"
    model = info.get("model") or info.get("id") or "whisper-1"
    return [str(model)], "health"


def _probe_stt_primary() -> bool:
    """True if the first whisper provider (when it's a fleet endpoint)
    answers /health or /v1/models. Cloud/local providers aren't monitored."""
    chain = CONFIG.get("whisper", {}).get("providers", [])
    if not chain:
        return True
    first = chain[0]
    if first.get("type") != "fleet" or not first.get("url"):
        return True
    base = first["url"].rstrip("/")
    for suffix in ("/v1/audio/transcriptions", "/audio/transcriptions"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    try:
        if requests.get(f"{base}/health", timeout=4).status_code < 500:
            return True
    except requests.RequestException:
        pass
    try:
        return requests.get(f"{base}/v1/models", timeout=4).status_code < 500
    except requests.RequestException:
        return False


# ---------------------------------------------------------------------------
# Notifications (custom overlay toast)
# ---------------------------------------------------------------------------
_notify_overlay: "NotificationOverlay | None" = None


class NotificationOverlay:
    def __init__(self):
        self._root: tk.Tk | None = None
        self._title_var: tk.StringVar | None = None
        self._msg_var: tk.StringVar | None = None
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run_tk, daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5)

    def _run_tk(self):
        self._root = tk.Tk()
        self._root.overrideredirect(True)
        self._root.attributes("-topmost", True)
        self._root.attributes("-alpha", 0.0)
        self._root.configure(bg="#1E1E2E")
        self._title_var = tk.StringVar()
        self._msg_var = tk.StringVar()
        frame = tk.Frame(self._root, bg="#1E1E2E", padx=12, pady=8)
        frame.pack()
        tk.Label(frame, textvariable=self._title_var,
                 font=("Segoe UI", 10, "bold"), fg="#CDD6F4", bg="#1E1E2E",
                 anchor="w").pack(fill="x")
        tk.Label(frame, textvariable=self._msg_var,
                 font=("Segoe UI", 9), fg="#A6ADC8", bg="#1E1E2E",
                 anchor="w", wraplength=280, justify="left").pack(fill="x")
        self._root.update_idletasks()
        self._root.withdraw()
        self._ready.set()
        self._root.mainloop()

    def show(self, title: str, message: str, timeout: int = 3):
        if not self._root:
            return
        self._root.after(0, lambda: self._do_show(title, message, timeout))

    def _do_show(self, title: str, message: str, timeout: int):
        if not self._root: return
        self._title_var.set(title)
        self._msg_var.set(message)
        self._root.update_idletasks()
        w = self._root.winfo_reqwidth()
        h = self._root.winfo_reqheight()
        sw = self._root.winfo_screenwidth()
        sh = self._root.winfo_screenheight()
        self._root.geometry(f"{w}x{h}+{sw - w - 16}+{sh - h - 48}")
        self._root.deiconify()
        self._root.lift()
        self._fade(0.0, 0.92, 8, timeout * 1000)

    def _fade(self, start: float, end: float, steps: int, hold_ms: int):
        if not self._root: return
        def step(i):
            if not self._root: return
            if i > steps:
                self._root.after(hold_ms, lambda: self._fade(0.92, 0.0, 8, 0))
                return
            alpha = start + (end - start) * (i / steps)
            self._root.attributes("-alpha", alpha)
            self._root.after(20, lambda: step(i + 1))
        if end == 0.0 and hold_ms == 0:
            def hide_step(i):
                if not self._root: return
                if i > steps:
                    self._root.withdraw()
                    self._root.attributes("-alpha", 0.0)
                    return
                alpha = start - start * (i / steps)
                self._root.attributes("-alpha", max(0, alpha))
                self._root.after(20, lambda: hide_step(i + 1))
            hide_step(0)
        else:
            step(0)


def notify(message: str, title: str = "Voice Hotkey", timeout: int = 3) -> None:
    global _notify_overlay
    if _notify_overlay is None:
        try:
            _notify_overlay = NotificationOverlay()
        except Exception:
            return
    try:
        _notify_overlay.show(title, message, timeout)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# STT-down warning (dismissible panel)
# ---------------------------------------------------------------------------
_stt_warning_overlay: "SttWarningOverlay | None" = None


class SttWarningOverlay:
    """Small dismissible 'STT is down' panel. Persistent root — withdrawn,
    never destroyed (same Tk rule as every other overlay)."""

    def __init__(self):
        self._root: tk.Tk | None = None
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run_tk, daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5)

    def _run_tk(self):
        self._root = tk.Tk()
        self._root.overrideredirect(True)
        self._root.attributes("-topmost", True)
        self._root.configure(bg="#7F1D1D")
        frame = tk.Frame(self._root, bg="#7F1D1D", padx=14, pady=10)
        frame.pack()
        tk.Label(frame, text="⚠  Speech-to-text unreachable",
                 font=("Segoe UI", 11, "bold"), fg="#FECACA",
                 bg="#7F1D1D", anchor="w").pack(fill="x")
        tk.Label(frame,
                 text="Please check your transcription configuration.\n"
                      "Tray → STT Endpoint & Model… to fix the endpoint.",
                 font=("Segoe UI", 9), fg="#FCA5A5", bg="#7F1D1D",
                 anchor="w", justify="left").pack(fill="x", pady=(2, 0))
        tk.Button(frame, text="✕  Close", font=("Segoe UI", 9),
                  fg="#FCA5A5", bg="#7F1D1D", activebackground="#991B1B",
                  relief="flat", padx=10, pady=2, command=self.hide,
                  ).pack(anchor="e", pady=(6, 0))
        self._root.update_idletasks()
        w = self._root.winfo_reqwidth()
        h = self._root.winfo_reqheight()
        sw = self._root.winfo_screenwidth()
        sh = self._root.winfo_screenheight()
        self._root.geometry(f"+{sw - w - 16}+{sh - h - 120}")
        self._root.withdraw()
        self._ready.set()
        self._root.mainloop()

    def show(self):
        if self._root:
            try:
                self._root.after(0, lambda: (self._root.deiconify(), self._root.lift()))
            except Exception:
                pass

    def hide(self):
        if self._root:
            try:
                self._root.after(0, self._root.withdraw)
            except Exception:
                pass


def _show_stt_warning() -> None:
    if _stt_warning_overlay is not None:
        _stt_warning_overlay.show()


# ---------------------------------------------------------------------------
# Send / Copy / PRD overlay
# ---------------------------------------------------------------------------
_send_copy_overlay: "SendCopyOverlay | None" = None


class SendCopyOverlay:
    """Interactive toast with Send / Copy / PRD buttons after transcription.

    `auto_send_timeout`: 0 = wait for click forever; >0 = auto-send after N
    seconds. The default in config is 0.
    """

    def __init__(self, auto_send_timeout: int = 0):
        self._root: tk.Tk | None = None
        self._ready = threading.Event()
        self._result: str | None = None
        self._result_event = threading.Event()
        self._preview_text: tk.Text | None = None
        self._channel_var: tk.StringVar | None = None
        self._countdown_var: tk.StringVar | None = None
        self._countdown_id: str | None = None
        self._auto_send_timeout = int(auto_send_timeout)
        self._prd_done = False
        self._prd_text = ""
        self._thread = threading.Thread(target=self._run_tk, daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5)

    def _run_tk(self):
        self._root = tk.Tk()
        self._root.overrideredirect(True)
        self._root.attributes("-topmost", True)
        self._root.attributes("-alpha", 0.0)
        self._root.configure(bg="#1E1E2E")

        self._frame = tk.Frame(self._root, bg="#1E1E2E", padx=14, pady=10)
        self._frame.pack()

        self._title_label = tk.Label(self._frame, text="Voice Memo Ready",
                 font=("Segoe UI", 11, "bold"), fg="#CDD6F4", bg="#1E1E2E",
                 anchor="w")
        self._title_label.pack(fill="x")
        self._channel_var = tk.StringVar(value="→ general")
        self._channel_label = tk.Label(self._frame, textvariable=self._channel_var,
                 font=("Segoe UI", 9), fg="#89B4FA", bg="#1E1E2E",
                 anchor="w")
        self._channel_label.pack(fill="x", pady=(2, 4))

        self._preview_text = tk.Text(
            self._frame, font=("Segoe UI", 9), fg="#A6ADC8", bg="#2A2A3E",
            wrap="word", height=4, width=40, relief="flat",
            insertbackground="#A6ADC8", selectbackground="#45475A",
            padx=4, pady=4,
        )
        self._preview_text.pack(fill="x", pady=(0, 8))
        # Read-only but allow Ctrl+C / Ctrl+A
        self._preview_text.bind(
            "<Key>",
            lambda e: "break" if e.keysym not in ("c", "C", "a", "A") or not (e.state & 0x4) else None,
        )

        self._btn_frame = tk.Frame(self._frame, bg="#1E1E2E")
        self._btn_frame.pack(fill="x")

        # Discord Send removed 2026-09-03 (relay archived — agents are local).
        tk.Button(
            self._btn_frame, text="📋 Copy", font=("Segoe UI", 10, "bold"),
            fg="#1E1E2E", bg="#89B4FA", activebackground="#B4D0FB",
            relief="flat", padx=16, pady=4,
            command=lambda: self._choose("copy"),
        ).pack(side="left", padx=(0, 8))

        tk.Button(
            self._btn_frame, text="📝 PRD", font=("Segoe UI", 10, "bold"),
            fg="#1E1E2E", bg="#A6E3A1", activebackground="#B8F0B0",
            relief="flat", padx=16, pady=4,
            command=lambda: self._choose("prd"),
        ).pack(side="left", padx=(0, 8))

        tk.Button(
            self._btn_frame, text="✕", font=("Segoe UI", 10),
            fg="#6C7086", bg="#1E1E2E", activebackground="#45475A",
            relief="flat", padx=8, pady=4,
            command=lambda: self._choose("dismiss"),
        ).pack(side="right")

        self._countdown_var = tk.StringVar(value="")
        self._countdown_label = tk.Label(self._btn_frame, textvariable=self._countdown_var,
                 font=("Segoe UI", 8), fg="#6C7086", bg="#1E1E2E",
                 anchor="e")
        self._countdown_label.pack(side="right")

        self._root.update_idletasks()
        self._root.withdraw()
        self._ready.set()
        self._root.mainloop()

    def _choose(self, choice: str):
        if self._prd_done:
            # Post-PRD mode: prompt() already returned; Copy acts on the
            # PRD text, ✕ closes. Anything else is ignored.
            if choice == "copy":
                try:
                    _copy_to_clipboard(self._prd_text)
                    notify("PRD copied to clipboard", "Voice Hotkey")
                except Exception as e:
                    notify(f"Copy failed: {e}", "Voice Hotkey Error")
            elif choice == "dismiss":
                self._hide()
            return
        if choice == "prd":
            # Stay visible; show generating state in the preview pane
            self._result = choice
            if self._countdown_id:
                self._root.after_cancel(self._countdown_id)
                self._countdown_id = None
            self._result_event.set()
            self._root.after(0, lambda: self._show_generating())
            return
        self._result = choice
        if self._countdown_id:
            self._root.after_cancel(self._countdown_id)
            self._countdown_id = None
        self._result_event.set()
        self._hide()

    def _show_generating(self):
        if not self._root: return
        self._preview_text.config(state="normal")
        self._preview_text.delete("1.0", "end")
        self._preview_text.insert(
            "1.0",
            "⏳ Generating PRD via local DeepSeek V4 Flash...\nThis may take a minute.",
        )
        self._preview_text.config(state="disabled")
        chain = CONFIG.get("prd", {}).get("providers", [])
        if chain:
            self._channel_var.set(f"→ {chain[0].get('name', 'local LLM')}")
        self._root.deiconify()
        self._root.lift()
        self._root.attributes("-alpha", 0.95)

    def set_prd_result(self, text: str, info_line: str = "") -> None:
        """PRD finished: tint the overlay light yellow and keep it open —
        a persistent visual 'done' cue. Copy then acts on the PRD text."""
        if not self._root:
            return
        self._root.after(0, lambda: self._show_prd_result(text, info_line))

    def _show_prd_result(self, text: str, info_line: str) -> None:
        if not self._root:
            return
        self._prd_done = True
        self._prd_text = text
        self._tint_done()
        if info_line:
            self._channel_var.set(info_line)
        self._preview_text.config(state="normal")
        self._preview_text.delete("1.0", "end")
        self._preview_text.insert("1.0", text)
        self._preview_text.config(state="disabled", height=14)
        self._root.update_idletasks()
        w = max(self._root.winfo_reqwidth(), 320)
        h = self._root.winfo_reqheight()
        sw = self._root.winfo_screenwidth()
        sh = self._root.winfo_screenheight()
        self._root.geometry(f"{w}x{h}+{sw - w - 16}+{sh - h - 48}")
        self._root.deiconify()
        self._root.lift()

    _DONE_BG = "#F2ECC3"   # very light yellow — "PRD complete"
    _DONE_FG = "#3B3626"

    def _tint_done(self) -> None:
        y, dark = self._DONE_BG, self._DONE_FG
        self._root.configure(bg=y)
        self._frame.configure(bg=y)
        self._btn_frame.configure(bg=y)
        self._title_label.configure(text="✅ PRD Ready", fg=dark, bg=y)
        self._channel_label.configure(fg="#1a4f9c", bg=y)
        self._countdown_label.configure(bg=y)

    def _reset_theme(self) -> None:
        b = "#1E1E2E"
        self._root.configure(bg=b)
        self._frame.configure(bg=b)
        self._btn_frame.configure(bg=b)
        self._title_label.configure(text="Voice Memo Ready", fg="#CDD6F4", bg=b)
        self._channel_label.configure(fg="#89B4FA", bg=b)
        self._countdown_label.configure(bg=b)
        self._prd_done = False

    def _hide(self):
        if not self._root: return
        try:
            self._root.after(0, lambda: (
                self._root.attributes("-alpha", 0.0),
                self._root.withdraw(),
            ))
        except Exception:
            pass

    def prompt(self, text: str, channel_name: str) -> str:
        """Show overlay; block until the user clicks a button (or auto-send)."""
        self._result = None
        self._result_event.clear()
        if not self._root:
            return "send"
        self._root.after(0, lambda: self._do_show(text, channel_name, self._auto_send_timeout))
        # Block until user clicks (or auto-copy fires)
        if self._auto_send_timeout > 0:
            self._result_event.wait(timeout=self._auto_send_timeout + 2)
            if self._result is None:
                self._result = "copy"
                self._hide()
        else:
            self._result_event.wait()
        return self._result or "copy"

    def _do_show(self, text: str, channel_name: str, timeout: int):
        if not self._root: return
        self._reset_theme()
        self._preview_text.config(state="normal")
        self._preview_text.delete("1.0", "end")
        self._preview_text.insert("1.0", text)
        self._preview_text.config(state="disabled")
        line_count = min(8, max(3, len(text) // 60 + 1))
        self._preview_text.config(height=line_count)
        self._channel_var.set(f"→ {channel_name}")
        self._root.update_idletasks()
        w = max(self._root.winfo_reqwidth(), 320)
        h = self._root.winfo_reqheight()
        sw = self._root.winfo_screenwidth()
        sh = self._root.winfo_screenheight()
        self._root.geometry(f"{w}x{h}+{sw - w - 16}+{sh - h - 48}")
        self._root.deiconify()
        self._root.lift()
        self._root.attributes("-alpha", 0.95)
        if timeout > 0:
            self._start_countdown(timeout)

    def _start_countdown(self, seconds: int):
        if not self._root: return
        if seconds <= 0:
            self._choose("copy")
            return
        self._countdown_var.set(f"auto-copy in {seconds}s")
        self._countdown_id = self._root.after(
            1000, lambda: self._start_countdown(seconds - 1)
        )


# ---------------------------------------------------------------------------
# Recording overlay (live waveform)
# ---------------------------------------------------------------------------
class RecordingOverlay:
    WAVEFORM_WIDTH = 280
    WAVEFORM_HEIGHT = 50
    BAR_WIDTH = 3
    BAR_GAP = 1
    BG_COLOR = "#1a1a2e"
    BAR_COLOR = "#e74c3c"
    BAR_COLOR_PROCESS = "#F59E0B"
    TEXT_COLOR = "#ffffff"

    def __init__(self):
        self._root: tk.Tk | None = None
        self._canvas: tk.Canvas | None = None
        self._label: tk.Label | None = None
        self._visible = False
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._amplitude_queue: queue.Queue = queue.Queue(maxsize=200)
        self._amplitudes: list[float] = []
        self._animating = False
        self._mode = "idle"
        self._start_overlay_thread()

    def _start_overlay_thread(self):
        self._thread = threading.Thread(target=self._run_tk, daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5)

    def _run_tk(self):
        self._root = tk.Tk()
        self._root.title("")
        self._root.overrideredirect(True)
        self._root.attributes("-topmost", True)
        self._root.attributes("-alpha", 0.92)
        self._root.configure(bg=self.BG_COLOR)
        self._label = tk.Label(
            self._root, text="  ● REC  ",
            font=("Segoe UI", 11, "bold"),
            fg=self.TEXT_COLOR, bg=self.BG_COLOR, padx=6, pady=2,
        )
        self._label.pack(fill="x")
        self._canvas = tk.Canvas(
            self._root, width=self.WAVEFORM_WIDTH, height=self.WAVEFORM_HEIGHT,
            bg=self.BG_COLOR, highlightthickness=0,
        )
        self._canvas.pack(padx=6, pady=(0, 6))
        self._max_bars = self.WAVEFORM_WIDTH // (self.BAR_WIDTH + self.BAR_GAP)
        self._amplitudes = [0.0] * self._max_bars
        self._root.update_idletasks()
        screen_w = self._root.winfo_screenwidth()
        win_w = self._root.winfo_reqwidth()
        self._root.geometry(f"+{screen_w - win_w - 20}+10")
        self._root.withdraw()
        self._ready.set()
        self._root.mainloop()

    def push_amplitude(self, rms: float):
        try:
            self._amplitude_queue.put_nowait(rms)
        except queue.Full:
            pass

    def _start_animation(self):
        if self._animating:
            return
        self._animating = True
        self._animate()

    def _stop_animation(self):
        self._animating = False

    def _animate(self):
        if not self._animating or not self._root:
            return
        while not self._amplitude_queue.empty():
            try:
                amp = self._amplitude_queue.get_nowait()
                self._amplitudes.append(amp)
                if len(self._amplitudes) > self._max_bars:
                    self._amplitudes = self._amplitudes[-self._max_bars:]
            except queue.Empty:
                break
        self._draw_waveform()
        if self._root:
            self._root.after(50, self._animate)

    def _draw_waveform(self):
        if not self._canvas:
            return
        self._canvas.delete("all")
        h = self.WAVEFORM_HEIGHT
        bar_color = self.BAR_COLOR if self._mode == "recording" else self.BAR_COLOR_PROCESS
        for i, amp in enumerate(self._amplitudes):
            bar_h = max(2, int(amp * h * 0.95))
            x = i * (self.BAR_WIDTH + self.BAR_GAP)
            y_top = (h - bar_h) // 2
            y_bot = y_top + bar_h
            self._canvas.create_rectangle(
                x, y_top, x + self.BAR_WIDTH, y_bot, fill=bar_color, outline="",
            )

    def show(self, text: str = "  ● REC  ", color: str = "#DC2626"):
        if self._root and self._label:
            try:
                self._root.after(0, lambda: self._do_show(text, color))
            except Exception:
                pass

    def _do_show(self, text: str, color: str):
        if not self._root or not self._label:
            return
        self._label.config(text=text)
        self._root.deiconify()
        self._root.lift()
        self._visible = True
        if "REC" in text:
            self._mode = "recording"
            self._amplitudes = [0.0] * self._max_bars
            self._start_animation()
        elif "Processing" in text:
            self._mode = "processing"
        else:
            self._mode = "idle"

    def hide(self):
        if self._root:
            try:
                self._root.after(0, self._do_hide)
            except Exception:
                pass

    def _do_hide(self):
        if not self._root: return
        self._stop_animation()
        self._root.withdraw()
        self._visible = False
        self._mode = "idle"

    def update_text(self, text: str, color: str = "#DC2626"):
        self.show(text, color)


# ---------------------------------------------------------------------------
# Tray icon
# ---------------------------------------------------------------------------
def make_icon(color: str = "gray") -> Image.Image:
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    if color == "green":
        fill, outline = (34, 197, 94), (22, 163, 74)
    elif color == "orange":
        fill, outline = (249, 115, 22), (234, 88, 12)
    elif color == "red":
        fill, outline = (220, 38, 38), (153, 27, 27)  # STT endpoint down
    else:
        fill, outline = (148, 163, 184), (100, 116, 139)
    draw.ellipse([4, 4, size - 4, size - 4], fill=fill, outline=outline, width=2)
    mic_color = (255, 255, 255)
    draw.rounded_rectangle([24, 12, 40, 34], radius=6, fill=mic_color)
    draw.arc([18, 22, 46, 44], start=0, end=180, fill=mic_color, width=3)
    draw.line([32, 44, 32, 52], fill=mic_color, width=3)
    draw.line([24, 52, 40, 52], fill=mic_color, width=3)
    return img


# ---------------------------------------------------------------------------
# Recorder
# ---------------------------------------------------------------------------
class Recorder:
    def __init__(self):
        self.is_recording = False
        self.audio_data: list[np.ndarray] = []
        self.stream = None
        self.lock = threading.RLock()  # toggle() re-enters via _start/_stop_recording
        self.sample_rate = CONFIG.get("sample_rate", 16000)
        self.channels = CONFIG.get("channels", 1)
        self._tray_icon: pystray.Icon | None = None
        self._overlay: RecordingOverlay | None = None
        self._rec_start_time: float = 0.0

    def set_tray(self, icon: pystray.Icon):
        self._tray_icon = icon

    def set_overlay(self, overlay: RecordingOverlay):
        self._overlay = overlay

    def update_icon(self, color: str):
        if self._tray_icon:
            # Idle-gray turns red while the primary STT endpoint is down;
            # recording/processing states always win.
            if color == "gray" and not _stt_healthy[0]:
                color = "red"
            try:
                self._tray_icon.icon = make_icon(color)
            except Exception:
                pass

    def toggle(self):
        with self.lock:
            if self.is_recording:
                self._stop_recording()
            else:
                self._start_recording()

    def abort(self):
        with self.lock:
            if not self.is_recording:
                return
            self.is_recording = False
            if self.stream:
                self.stream.stop()
                self.stream.close()
                self.stream = None
            self.audio_data = []
            self.update_icon("gray")
            if self._overlay:
                self._overlay.hide()
        _play_stop_tone()
        notify("Recording aborted", "Voice Hotkey", timeout=2)

    def _start_recording(self):
        # Guard against rapid double-press (start while already processing)
        with self.lock:
            if self.is_recording or self._tray_icon is None:
                return
            try:
                sd.query_devices(kind="input")
            except sd.PortAudioError:
                notify("No microphone found!", "Voice Hotkey Error")
                return
            self.audio_data = []
            self.is_recording = True
            self._rec_start_time = time.time()
            self.update_icon("green")
            _play_start_tone()
            if self._overlay:
                self._overlay.show("  ● REC  ", "#DC2626")
            notify("Recording...", "Voice Hotkey", timeout=2)

            def callback(indata, frames, time_info, status):
                # Surface xruns/overruns in the log instead of silently dropping them
                if status:
                    log_error(f"PortAudio status: {status}")
                self.audio_data.append(indata.copy())
                if self._overlay:
                    rms = float(np.sqrt(np.mean(indata ** 2)))
                    self._overlay.push_amplitude(min(1.0, rms * 10.0))

            try:
                self.stream = sd.InputStream(
                    samplerate=self.sample_rate,
                    channels=self.channels,
                    dtype="float32",
                    callback=callback,
                )
                self.stream.start()
            except Exception as e:
                self.is_recording = False
                self.update_icon("gray")
                notify(f"Mic error: {e}", "Voice Hotkey Error")

    def _stop_recording(self):
        with self.lock:
            if not self.is_recording:
                return
            self.is_recording = False
            duration = time.time() - self._rec_start_time
            _play_stop_tone()
            if self.stream:
                self.stream.stop()
                self.stream.close()
                self.stream = None
            if not self.audio_data:
                self.update_icon("gray")
                if self._overlay:
                    self._overlay.hide()
                notify("No audio captured", "Voice Hotkey")
                return
            self.update_icon("orange")
            if self._overlay:
                self._overlay.update_text(f"  ⏳ Processing {duration:.0f}s...  ", "#F59E0B")
            audio = np.concatenate(self.audio_data, axis=0)
            threading.Thread(target=self._process_audio, args=(audio,), daemon=True).start()

    def _process_audio(self, audio: np.ndarray):
        audio_path: Path | None = None
        try:
            rotate_recordings_if_needed()
            ts = time.strftime("%Y%m%d_%H%M%S")
            audio_path = RECORDINGS_DIR / f"voice_hotkey_{ts}.wav"
            if audio.ndim > 1:
                audio = audio[:, 0]
            sf.write(str(audio_path), audio, self.sample_rate)

            duration = len(audio) / self.sample_rate
            if duration < 0.5:
                notify("Recording too short", "Voice Hotkey")
                self.update_icon("gray")
                return

            chain = CONFIG.get("whisper", {}).get("providers", [])
            if not chain:
                # Back-compat: old config had whisper_model + openai_transcription_model
                chain = self._legacy_whisper_chain()

            result = transcribe_with_retry(
                str(audio_path), chain,
                log=lambda msg: log_error(msg, audio_path),
            )
            if not result or not result.text:
                log_error("All transcription providers failed", audio_path)
                notify("Transcription failed — check log", "Voice Hotkey Error")
                self.update_icon("gray")
                return

            text = result.text

            # Optional: put the transcription on the clipboard immediately,
            # before the Send/Copy/PRD overlay is shown (Copy still works).
            copied = False
            if CONFIG.get("auto_copy_to_clipboard", False):
                try:
                    _copy_to_clipboard(text)
                    copied = True
                except Exception as e:
                    log_error(f"Auto-copy to clipboard failed: {e}")

            notify(
                f"Transcribed via {result.provider} in {result.duration_s:.1f}s"
                + (" — on clipboard" if copied else ""),
                "Voice Hotkey",
                timeout=2,
            )

            try:
                LAST_TRANSCRIPTION_PATH.write_text(text, encoding="utf-8")
                audio_path.with_suffix(".txt").write_text(text, encoding="utf-8")
            except OSError as e:
                log_error(f"Failed to persist transcription: {e}", audio_path)

            if self._overlay:
                self._overlay.hide()

            self._handle_text_choice(text, audio_path=str(audio_path))

        except Exception as e:
            log_error(f"Processing failed: {e}", audio_path)
            notify(f"Error: {e}", "Voice Hotkey Error")
        finally:
            self.update_icon("gray")
            if self._overlay:
                self._overlay.hide()

    def _legacy_whisper_chain(self) -> list[dict]:
        """Build a chain from old config keys (whisper_model + openai_transcription_model)."""
        chain: list[dict] = []
        if get_openai_key():
            chain.append({
                "type": "openai",
                "name": "openai-cloud",
                "model": CONFIG.get("openai_transcription_model", "whisper-1"),
                "timeout": 60,
                "retries": 3,
            })
        chain.append({
            "type": "local",
            "name": "local-whisper",
            "model": CONFIG.get("whisper_model", "base"),
        })
        return chain

    def _handle_text_choice(self, text: str, audio_path: str | None = None) -> None:
        if not text or not text.strip():
            notify("No transcription text", "Voice Hotkey")
            return

        rule = route_message(text)
        channel_id = rule.get("channel_id", "")
        channel_name = rule.get("name", "general")
        target_agent = rule.get("target_agent", channel_name)

        if _send_copy_overlay is None:
            notify("UI not ready yet — try again in a moment", "Voice Hotkey")
            return

        choice = _send_copy_overlay.prompt(text, channel_name)

        if choice == "dismiss":
            return
        if choice == "copy":
            try:
                _copy_to_clipboard(text)
                notify(f"Copied! Backup: {LAST_TRANSCRIPTION_PATH.name}", "Voice Hotkey")
            except Exception as e:
                notify(f"Copy failed: {e}", "Voice Hotkey Error")
            return
        if choice == "prd":
            threading.Thread(
                target=self._run_prd_flow,
                args=(text, channel_id, channel_name, target_agent, audio_path),
                daemon=True,
            ).start()
            return

        # Default: copy (Discord send archived 2026-09-03 — agents are local)
        try:
            _copy_to_clipboard(text)
            notify(f"Copied! Backup: {LAST_TRANSCRIPTION_PATH.name}", "Voice Hotkey")
        except Exception as e:
            notify(f"Copy failed: {e}", "Voice Hotkey Error")

    def _run_prd_flow(self, text: str, channel_id: str, channel_name: str,
                      target_agent: str, audio_path: str | None) -> None:
        chain = [dict(c) for c in CONFIG.get("prd", {}).get("providers", [])]
        if not chain:
            notify("No PRD providers configured", "Voice Hotkey Error")
            return
        # User-edited prompt (tray → Edit PRD Prompt…) overrides the built-in.
        if CONFIG.get("prd_prompt"):
            for c in chain:
                c["system_prompt"] = CONFIG["prd_prompt"]
        prd_result = generate_prd(text, chain, log=lambda m: log_error(f"PRD: {m}", audio_path))
        if not prd_result or not prd_result.text:
            notify("PRD generation failed — all providers unreachable", "Voice Hotkey Error")
            if _send_copy_overlay is not None:
                _send_copy_overlay._hide()
            return
        prd_text = prd_result.text
        try:
            LAST_PRD_PATH.write_text(prd_text, encoding="utf-8")
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            (PRD_ARCHIVE_DIR / f"prd_{ts}.txt").write_text(prd_text, encoding="utf-8")
            rotate_prd_archive_if_needed()
        except OSError as e:
            log_error(f"Failed to persist PRD: {e}", audio_path)

        copied = False
        if CONFIG.get("prd_auto_copy_to_clipboard", True):
            try:
                _copy_to_clipboard(prd_text)
                copied = True
            except Exception as e:
                notify(f"PRD saved to {LAST_PRD_PATH.name} (clipboard failed: {e})", "Voice Hotkey")
        # Keep the overlay open, tinted light yellow — the visible "done"
        # cue. Copy on it now copies the PRD itself.
        if _send_copy_overlay is not None:
            _send_copy_overlay.set_prd_result(
                prd_text,
                f"via {prd_result.provider}, {prd_result.duration_s:.0f}s"
                + (" • on clipboard" if copied else ""),
            )
        else:
            notify(f"PRD ready (via {prd_result.provider}, {prd_result.duration_s:.0f}s)",
                   "Voice Hotkey", timeout=4)

        # If the PRD was for a Discord-routed topic, optionally post the markdown to the channel
        if CONFIG.get("prd_auto_post_to_channel", False):
            ok = send_to_discord(
                channel_id, prd_text,
                audio_path=None,
                target_agent=target_agent,
            )
            if ok:
                notify(f"PRD also posted to #{channel_name}", "Voice Hotkey")

    def _push_to_scaffolding_daemon(self, text: str, channel_id: str,
                                    channel_name: str, target_agent: str) -> None:
        """Best-effort direct push to the local Bastion daemon.

        Skipped if daemon is down; the daemon's own poll cycle will catch up.
        """
        url = CONFIG.get("scaffolding_daemon_url", "http://127.0.0.1:8097/api/voice-memo")
        try:
            requests.post(
                url,
                json={
                    "transcription": text,
                    "msg_id": "direct-push",
                    "channel_id": channel_id,
                    "channel_name": channel_name,
                    "target_agent": target_agent,
                },
                timeout=3,
            )
        except requests.RequestException as e:
            log_error(f"Scaffolding daemon push failed (will retry on poll): {e}")


# ---------------------------------------------------------------------------
# Audio cues
# ---------------------------------------------------------------------------
def _play_tone(freq: float = 800, duration: float = 0.15, volume: float = 0.3) -> None:
    try:
        sr = 44100
        t = np.linspace(0, duration, int(sr * duration), endpoint=False)
        tone = (volume * np.sin(2 * np.pi * freq * t)).astype(np.float32)
        fade = int(sr * 0.01)
        tone[:fade] *= np.linspace(0, 1, fade).astype(np.float32)
        tone[-fade:] *= np.linspace(1, 0, fade).astype(np.float32)
        sd.play(tone, sr, blocking=False)
    except Exception:
        pass


def _play_start_tone() -> None:
    threading.Thread(
        target=lambda: (_play_tone(600, 0.1, 0.25), time.sleep(0.05), _play_tone(900, 0.15, 0.3)),
        daemon=True,
    ).start()


def _play_stop_tone() -> None:
    threading.Thread(
        target=lambda: (_play_tone(900, 0.1, 0.25), time.sleep(0.05), _play_tone(500, 0.2, 0.3)),
        daemon=True,
    ).start()


# ---------------------------------------------------------------------------
# Clipboard
# ---------------------------------------------------------------------------
def _copy_to_clipboard(text: str) -> None:
    """Copy text to the Windows clipboard via Win32 directly, with a backup file."""
    try:
        LAST_TRANSCRIPTION_PATH.write_text(text, encoding="utf-8")
    except OSError:
        pass

    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002
    kernel32 = ctypes.windll.kernel32
    user32 = ctypes.windll.user32
    kernel32.GlobalAlloc.restype = ctypes.c_size_t
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_size_t]
    kernel32.GlobalUnlock.argtypes = [ctypes.c_size_t]
    kernel32.GlobalFree.argtypes = [ctypes.c_size_t]
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    user32.SetClipboardData.restype = ctypes.c_size_t

    text_bytes = text.encode("utf-16-le") + b"\x00\x00"
    buf_size = len(text_bytes)
    h_mem = kernel32.GlobalAlloc(GMEM_MOVEABLE, buf_size)
    if not h_mem:
        raise RuntimeError("GlobalAlloc failed")
    ptr = kernel32.GlobalLock(h_mem)
    if not ptr:
        kernel32.GlobalFree(h_mem)
        raise RuntimeError("GlobalLock failed")
    ctypes.memmove(ptr, text_bytes, buf_size)
    kernel32.GlobalUnlock(h_mem)

    recopy = bool(CONFIG.get("clipboard_recopy_after_500ms", True))
    for attempt in range(3):
        if user32.OpenClipboard(None):
            user32.EmptyClipboard()
            result = user32.SetClipboardData(CF_UNICODETEXT, h_mem)
            user32.CloseClipboard()
            if result:
                if recopy:
                    def _recopy():
                        time.sleep(0.5)
                        try:
                            _copy_to_clipboard_raw(text)
                        except Exception:
                            pass
                    threading.Thread(target=_recopy, daemon=True).start()
                return
            break
        time.sleep(0.05)
    kernel32.GlobalFree(h_mem)
    raise RuntimeError(f"Clipboard write failed. Text saved to: {LAST_TRANSCRIPTION_PATH}")


def _copy_to_clipboard_raw(text: str) -> None:
    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002
    kernel32 = ctypes.windll.kernel32
    user32 = ctypes.windll.user32
    kernel32.GlobalAlloc.restype = ctypes.c_size_t
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_size_t]
    kernel32.GlobalUnlock.argtypes = [ctypes.c_size_t]
    kernel32.GlobalFree.argtypes = [ctypes.c_size_t]
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    user32.SetClipboardData.restype = ctypes.c_size_t
    text_bytes = text.encode("utf-16-le") + b"\x00\x00"
    h_mem = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(text_bytes))
    if not h_mem:
        return
    ptr = kernel32.GlobalLock(h_mem)
    if not ptr:
        kernel32.GlobalFree(h_mem)
        return
    ctypes.memmove(ptr, text_bytes, len(text_bytes))
    kernel32.GlobalUnlock(h_mem)
    if user32.OpenClipboard(None):
        user32.EmptyClipboard()
        result = user32.SetClipboardData(CF_UNICODETEXT, h_mem)
        user32.CloseClipboard()
        if not result:
            kernel32.GlobalFree(h_mem)
    else:
        kernel32.GlobalFree(h_mem)


# ---------------------------------------------------------------------------
# Preload (warm local Whisper at startup)
# ---------------------------------------------------------------------------
def _preload_whisper() -> None:
    """Preload the local Whisper model so the first memo is instant.

    Skipped if a non-local provider is configured first in the chain (the
    cloud call is fast enough). For local-only or local-fallback setups,
    this saves 5-15s on the first recording.
    """
    chain = CONFIG.get("whisper", {}).get("providers", [])
    if not chain:
        return
    # If any non-local provider comes first, the local model is a fallback
    # and we don't need to warm it at startup.
    first = chain[0]
    if first.get("type") != "local":
        return
    try:
        import whisper  # noqa: F401  (import is the slow part; load_model runs)
        notify("Preloading local Whisper model...", "Voice Hotkey", timeout=2)
        whisper.load_model(first.get("model", "base"))
        notify("Whisper ready", "Voice Hotkey", timeout=2)
    except Exception as e:
        log_error(f"Whisper preload failed: {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    global _send_copy_overlay

    recorder = Recorder()
    overlay = RecordingOverlay()
    recorder.set_overlay(overlay)
    _send_copy_overlay = SendCopyOverlay(
        auto_send_timeout=int(CONFIG.get("auto_send_timeout", 0))
    )
    global _stt_warning_overlay
    _stt_warning_overlay = SttWarningOverlay()

    _hotkey_reload_event = threading.Event()

    def on_quit(icon, item):
        keyboard.unhook_all()
        icon.stop()

    def on_restart(icon, item):
        """Kill this process and let start-hidden.vbs respawn a fresh one.

        The VBS already has kill-then-launch logic, so we just kick it off
        and then stop the tray icon. ~1.5s later a new process is up.
        """
        try:
            notify("Restarting...", "Voice Hotkey", timeout=2)
        except Exception:
            pass
        # Fire the launcher detached. It will kill this process and spawn a
        # new one in the same VBS run.
        vbs = APP_DIR / "start-hidden.vbs"
        if vbs.exists():
            try:
                subprocess.Popen(
                    ["wscript.exe", str(vbs)],
                    creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
                    close_fds=True,
                )
            except Exception as e:
                log_error(f"Failed to spawn restart VBS: {e}")
        # Give the VBS a moment to start before we exit, so the kill happens
        # in the VBS rather than us killing ourselves prematurely.
        threading.Timer(0.4, lambda: (keyboard.unhook_all(), icon.stop())).start()

    def on_reload_config(icon, item):
        global CONFIG
        try:
            CONFIG = load_config()
            _hotkey_reload_event.set()
            notify("Config reloaded", "Voice Hotkey")
        except Exception as e:
            notify(f"Config error: {e}", "Voice Hotkey Error")

    def on_show_last(icon, item):
        text = _safe_read_text(LAST_TRANSCRIPTION_PATH)
        if not text.strip():
            notify("No saved transcription yet — record something first.", "Voice Hotkey")
            return
        threading.Thread(
            target=recorder._handle_text_choice,
            args=(text,),
            kwargs={"audio_path": None},
            daemon=True,
        ).start()

    def on_prd_from_last(icon, item):
        text = _safe_read_text(LAST_TRANSCRIPTION_PATH)
        if not text.strip():
            notify("No saved transcription to convert to PRD.", "Voice Hotkey")
            return
        threading.Thread(
            target=recorder._run_prd_flow,
            args=(text, "", "general", "general", None),
            daemon=True,
        ).start()

    def on_change_hotkey(icon, item):
        picker_open[0] = True

    # --- Hotkey picker: persistent overlay, opened on demand. Its Tk root
    # is withdrawn — never destroyed: tearing down a Tk root on a
    # background thread corrupts Tcl process-global state and hard-crashes
    # the next Tk call from any other overlay (the 2026-09-03 crash right
    # after saving a new hotkey). ---
    picker_open: list[bool] = [False]

    def _run_hotkey_picker() -> None:
        capture_queue: "queue.Queue[str]" = queue.Queue()
        state: dict = {"hotkey": None}

        # Hook-based capture (not keyboard.read_hotkey): no lingering
        # threads, and unhooked the moment listening stops.
        _kb_mods: set[str] = set()
        _MOD_TOKENS = {
            "ctrl": "ctrl", "left ctrl": "ctrl", "right ctrl": "ctrl",
            "alt": "alt", "left alt": "alt", "right alt": "alt",
            "shift": "shift", "left shift": "shift", "right shift": "shift",
            "windows": "win", "left windows": "win", "right windows": "win",
        }

        def _cap_key_evt(event) -> None:
            # Return None (falsy) or later handlers in the keyboard lib's
            # chain never see the event.
            if not _picker_capturing[0] or state["hotkey"]:
                return
            if event.event_type != "down":
                return
            name = (event.name or "").lower()
            if name in _MOD_TOKENS:
                _kb_mods.add(_MOD_TOKENS[name])
                return
            if name:
                combo = "+".join(sorted(_kb_mods) + [name])
                canon = canonical_hotkey(combo)
                if canon:
                    capture_queue.put(canon)
                _kb_mods.clear()

        def _cap_mouse(event) -> None:
            if (mouse is not None and isinstance(event, mouse.ButtonEvent)
                    and _picker_capturing[0]
                    and event.event_type in ("down", "double")
                    and event.button in _MOUSE_BUTTON_NAMES):
                capture_queue.put(f"mouse:{event.button}")

        def _stop_listening() -> None:
            _picker_capturing[0] = False
            try:
                keyboard.unhook(_cap_key_evt)
            except Exception:
                pass
            if mouse is not None:
                try:
                    mouse.unhook(_cap_mouse)
                except Exception:
                    pass

        def _listen() -> None:
            state["hotkey"] = None
            save_btn.config(state="disabled")
            status.config(
                text="Press any key combination,\nor click any mouse button…"
                     + ("" if mouse else "\n(mouse package missing)"),
                fg="#A6ADC8",
            )
            _picker_capturing[0] = True
            _kb_mods.clear()
            try:
                keyboard.hook(_cap_key_evt)
            except Exception:
                pass
            if mouse is not None:
                try:
                    mouse.hook(_cap_mouse)
                except Exception:
                    pass

        def _poll_queue() -> None:
            if picker_open[0]:
                picker_open[0] = False
                _listen()
                root.deiconify()
                root.lift()
            try:
                canon = capture_queue.get_nowait()
            except queue.Empty:
                root.after(50, _poll_queue)
                return
            if not state["hotkey"]:
                state["hotkey"] = canon
                status.config(text=f"Captured:  {hotkey_display(canon)}", fg="#A6E3A1")
                save_btn.config(state="normal")
            root.after(50, _poll_queue)

        def _save() -> None:
            hk = state["hotkey"]
            if not hk:
                return
            _stop_listening()
            root.withdraw()
            try:
                CONFIG["hotkey"] = hk
                CONFIG_PATH.write_text(
                    json.dumps(CONFIG, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
                _hotkey_reload_event.set()
                notify(f"Hotkey saved: {hotkey_display(hk)}", "Voice Hotkey")
            except OSError as e:
                log_error(f"Failed to save hotkey to config.json: {e}")
                notify(f"Save failed: {e}", "Voice Hotkey Error")

        def _close() -> None:
            _stop_listening()
            root.withdraw()

        root = tk.Tk()
        root.title("Voice Hotkey — Choose Hotkey")
        root.configure(bg="#1E1E2E")
        root.attributes("-topmost", True)
        root.resizable(False, False)
        frame = tk.Frame(root, bg="#1E1E2E", padx=16, pady=12)
        frame.pack()
        tk.Label(frame, text="Choose Hotkey", font=("Segoe UI", 11, "bold"),
                 fg="#CDD6F4", bg="#1E1E2E").pack(pady=(0, 4))
        status = tk.Label(frame, text="", font=("Segoe UI", 10),
                          fg="#A6ADC8", bg="#1E1E2E", justify="center")
        status.pack(pady=(0, 12))
        btns = tk.Frame(frame, bg="#1E1E2E")
        btns.pack()
        save_btn = tk.Button(
            btns, text="Save", font=("Segoe UI", 10, "bold"),
            fg="#1E1E2E", bg="#A6E3A1", activebackground="#B8F0B0",
            relief="flat", padx=16, pady=4, state="disabled", command=_save,
        )
        save_btn.pack(side="left", padx=(0, 8))
        tk.Button(
            btns, text="Clear", font=("Segoe UI", 10, "bold"),
            fg="#CDD6F4", bg="#45475A", activebackground="#585B70",
            relief="flat", padx=16, pady=4, command=_listen,
        ).pack(side="left", padx=(0, 8))
        tk.Button(
            btns, text="Cancel", font=("Segoe UI", 10, "bold"),
            fg="#CDD6F4", bg="#45475A", activebackground="#585B70",
            relief="flat", padx=16, pady=4, command=_close,
        ).pack(side="left")
        root.protocol("WM_DELETE_WINDOW", _close)
        root.withdraw()
        _poll_queue()
        root.mainloop()

    threading.Thread(target=_run_hotkey_picker, daemon=True).start()

    # --- PRD prompt editor + LLM endpoint picker (persistent overlays) ---
    def on_edit_prd_prompt(icon, item):
        prd_prompt_open[0] = True

    def on_llm_settings(icon, item):
        llm_open[0] = True

    prd_prompt_open: list[bool] = [False]
    llm_open: list[bool] = [False]
    llm_queue: "queue.Queue[tuple]" = queue.Queue()

    def _run_prd_prompt_editor() -> None:
        def _effective() -> str:
            return CONFIG.get("prd_prompt") or PRD_SYSTEM_PROMPT

        def _save() -> None:
            text = txt.get("1.0", "end-1c").strip()
            use_default = (not text) or (text == PRD_SYSTEM_PROMPT.strip())
            global CONFIG
            try:
                CONFIG = load_config()
                if use_default:
                    CONFIG.pop("prd_prompt", None)
                else:
                    CONFIG["prd_prompt"] = text
                CONFIG_PATH.write_text(
                    json.dumps(CONFIG, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
                notify("PRD prompt saved" + (" (using built-in default)" if use_default else ""),
                       "Voice Hotkey")
            except (OSError, ValueError) as e:
                log_error(f"Failed to save PRD prompt: {e}")
                notify(f"Save failed: {e}", "Voice Hotkey Error")
            root.withdraw()

        def _reset() -> None:
            txt.delete("1.0", "end")
            txt.insert("1.0", PRD_SYSTEM_PROMPT)

        def _close() -> None:
            root.withdraw()

        def _poll() -> None:
            if prd_prompt_open[0]:
                prd_prompt_open[0] = False
                txt.delete("1.0", "end")
                txt.insert("1.0", _effective())
                root.deiconify()
                root.lift()
            root.after(80, _poll)

        root = tk.Tk()
        root.title("Voice Hotkey — Edit PRD Prompt")
        root.configure(bg="#1E1E2E")
        root.attributes("-topmost", True)
        frame = tk.Frame(root, bg="#1E1E2E", padx=14, pady=10)
        frame.pack()
        tk.Label(frame, text="Edit PRD Prompt", font=("Segoe UI", 11, "bold"),
                 fg="#CDD6F4", bg="#1E1E2E").pack(anchor="w")
        tk.Label(frame, text="System prompt that shapes PRD output.\n"
                             "Clear the text (or Reset) to use the built-in default.",
                 font=("Segoe UI", 9), fg="#A6ADC8", bg="#1E1E2E",
                 justify="left").pack(anchor="w", pady=(2, 6))
        txt = tk.Text(frame, font=("Consolas", 9), fg="#A6ADC8", bg="#2A2A3E",
                      wrap="word", width=76, height=20, relief="flat",
                      insertbackground="#A6ADC8", padx=6, pady=6)
        scroll = tk.Scrollbar(frame, command=txt.yview)
        txt.configure(yscrollcommand=scroll.set)
        txt.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        btns = tk.Frame(root, bg="#1E1E2E", padx=14, pady=10)
        btns.pack(fill="x")
        tk.Button(btns, text="Save", font=("Segoe UI", 10, "bold"),
                  fg="#1E1E2E", bg="#A6E3A1", activebackground="#B8F0B0",
                  relief="flat", padx=16, pady=4, command=_save,
                  ).pack(side="left", padx=(0, 8))
        tk.Button(btns, text="Reset to Default", font=("Segoe UI", 10, "bold"),
                  fg="#CDD6F4", bg="#45475A", activebackground="#585B70",
                  relief="flat", padx=12, pady=4, command=_reset,
                  ).pack(side="left", padx=(0, 8))
        tk.Button(btns, text="Close", font=("Segoe UI", 10, "bold"),
                  fg="#CDD6F4", bg="#45475A", activebackground="#585B70",
                  relief="flat", padx=12, pady=4, command=_close,
                  ).pack(side="left")
        root.protocol("WM_DELETE_WINDOW", _close)
        root.withdraw()
        _poll()
        root.mainloop()

    def _run_llm_settings() -> None:
        def _current_base() -> str:
            try:
                url = (CONFIG.get("prd", {}).get("providers") or [{}])[0].get("url", "")
                url = url.rstrip("/")
                if url.endswith("/v1"):
                    url = url[:-3]
                return url
            except Exception:
                return ""

        def _discover() -> None:
            url = url_var.get().strip()
            if not url:
                return

            def work() -> None:
                try:
                    llm_queue.put(("ok", _discover_models(url)))
                except Exception as e:
                    llm_queue.put(("err", str(e)))

            status.config(text="Discovering…", fg="#F59E0B")
            threading.Thread(target=work, daemon=True).start()

        def _save() -> None:
            url = url_var.get().strip()
            model = model_var.get().strip()
            if not url or not model:
                status.config(text="Need both a URL and a model", fg="#F87171")
                return
            base = url.rstrip("/")
            if base.endswith("/v1"):
                base = base[:-3]
            if not base.startswith(("http://", "https://")):
                base = "http://" + base
            global CONFIG
            try:
                CONFIG = load_config()
                prd = CONFIG.setdefault("prd", {})
                providers = [p for p in prd.get("providers", [])
                             if p.get("url") != f"{base}/v1"]
                providers.insert(0, {
                    "type": "openai_compatible", "name": "custom-endpoint",
                    "url": f"{base}/v1", "model": model,
                    # Reasoning models spend much of the budget on invisible
                    # thinking; 16k leaves room for the visible document.
                    "timeout": 300, "max_tokens": 16384, "temperature": 0.3,
                })
                prd["providers"] = providers
                CONFIG_PATH.write_text(
                    json.dumps(CONFIG, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
                notify(f"PRD endpoint saved: {model}", "Voice Hotkey")
                root.withdraw()
            except (OSError, ValueError) as e:
                log_error(f"Failed to save LLM endpoint: {e}")
                notify(f"Save failed: {e}", "Voice Hotkey Error")

        def _close() -> None:
            root.withdraw()

        def _poll() -> None:
            if llm_open[0]:
                llm_open[0] = False
                url_var.set(_current_base())
                try:
                    first = (CONFIG.get("prd", {}).get("providers") or [{}])[0]
                    model_var.set(first.get("model", ""))
                except Exception:
                    model_var.set("")
                status.config(text="Enter an IP/host, then Discover.", fg="#A6ADC8")
                root.deiconify()
                root.lift()
            try:
                kind, payload = llm_queue.get_nowait()
            except queue.Empty:
                pass
            else:
                if kind == "ok" and payload:
                    model_combo["values"] = payload
                    if model_var.get() not in payload:
                        model_var.set(payload[0])
                    status.config(text=f"{len(payload)} models found", fg="#A6E3A1")
                elif kind == "ok":
                    status.config(text="Server returned no models", fg="#F87171")
                else:
                    status.config(text=f"Discovery failed: {payload[:80]}", fg="#F87171")
            root.after(80, _poll)

        root = tk.Tk()
        root.title("Voice Hotkey — LLM Endpoint & Model")
        root.configure(bg="#1E1E2E")
        root.attributes("-topmost", True)
        frame = tk.Frame(root, bg="#1E1E2E", padx=14, pady=10)
        frame.pack()
        tk.Label(frame, text="LLM Endpoint & Model", font=("Segoe UI", 11, "bold"),
                 fg="#CDD6F4", bg="#1E1E2E").pack(anchor="w")
        tk.Label(frame, text="OpenAI-compatible server used for PRD generation.\n"
                             "e.g. 192.168.1.100:8000 — Discover lists its models.",
                 font=("Segoe UI", 9), fg="#A6ADC8", bg="#1E1E2E",
                 justify="left").pack(anchor="w", pady=(2, 8))
        row = tk.Frame(frame, bg="#1E1E2E")
        row.pack(fill="x", pady=(0, 6))
        tk.Label(row, text="URL:", font=("Segoe UI", 9), fg="#A6ADC8",
                 bg="#1E1E2E").pack(side="left", padx=(0, 6))
        url_var = tk.StringVar()
        tk.Entry(row, textvariable=url_var, width=40, font=("Consolas", 9),
                 fg="#A6ADC8", bg="#2A2A3E", relief="flat",
                 insertbackground="#A6ADC8").pack(side="left", padx=(0, 8))
        tk.Button(row, text="Discover", font=("Segoe UI", 9, "bold"),
                  fg="#1E1E2E", bg="#89B4FA", activebackground="#B4D0FB",
                  relief="flat", padx=12, pady=2, command=_discover,
                  ).pack(side="left")
        row2 = tk.Frame(frame, bg="#1E1E2E")
        row2.pack(fill="x", pady=(0, 6))
        tk.Label(row2, text="Model:", font=("Segoe UI", 9), fg="#A6ADC8",
                 bg="#1E1E2E").pack(side="left", padx=(0, 6))
        model_var = tk.StringVar()
        model_combo = ttk.Combobox(row2, textvariable=model_var, state="readonly",
                                   width=38)
        model_combo.pack(side="left")
        status = tk.Label(frame, text="", font=("Segoe UI", 9), fg="#A6ADC8",
                          bg="#1E1E2E", anchor="w")
        status.pack(fill="x", pady=(2, 8))
        btns = tk.Frame(frame, bg="#1E1E2E")
        btns.pack(fill="x")
        tk.Button(btns, text="Save", font=("Segoe UI", 10, "bold"),
                  fg="#1E1E2E", bg="#A6E3A1", activebackground="#B8F0B0",
                  relief="flat", padx=16, pady=4, command=_save,
                  ).pack(side="left", padx=(0, 8))
        tk.Button(btns, text="Close", font=("Segoe UI", 10, "bold"),
                  fg="#CDD6F4", bg="#45475A", activebackground="#585B70",
                  relief="flat", padx=12, pady=4, command=_close,
                  ).pack(side="left")
        root.protocol("WM_DELETE_WINDOW", _close)
        root.withdraw()
        _poll()
        root.mainloop()

    threading.Thread(target=_run_prd_prompt_editor, daemon=True).start()
    threading.Thread(target=_run_llm_settings, daemon=True).start()

    # --- STT endpoint picker + health watch ---
    def on_stt_settings(icon, item):
        stt_open[0] = True

    stt_open: list[bool] = [False]
    stt_queue: "queue.Queue[tuple]" = queue.Queue()

    def _run_stt_settings() -> None:
        def _current_base() -> str:
            for p in CONFIG.get("whisper", {}).get("providers", []):
                if p.get("type") == "fleet" and p.get("url"):
                    u = p["url"].rstrip("/")
                    for suf in ("/v1/audio/transcriptions", "/audio/transcriptions"):
                        if u.endswith(suf):
                            return u[: -len(suf)]
                    return u
            return ""

        def _current_model() -> str:
            for p in CONFIG.get("whisper", {}).get("providers", []):
                if p.get("type") == "fleet":
                    return p.get("model", "whisper-1")
            return "whisper-1"

        def _discover() -> None:
            url = stt_url_var.get().strip()
            if not url:
                return

            def work() -> None:
                try:
                    models, source = _discover_stt(url)
                    stt_queue.put(("ok", models, source))
                except Exception as e:
                    stt_queue.put(("err", str(e), ""))

            stt_status.config(text="Discovering…", fg="#F59E0B")
            threading.Thread(target=work, daemon=True).start()

        def _save() -> None:
            url = stt_url_var.get().strip()
            model = stt_model_var.get().strip() or "whisper-1"
            if not url:
                stt_status.config(text="Need a URL", fg="#F87171")
                return
            base = url.rstrip("/")
            for suf in ("/v1/audio/transcriptions", "/audio/transcriptions"):
                if base.endswith(suf):
                    base = base[: -len(suf)]
            if not base.startswith(("http://", "https://")):
                base = "http://" + base
            global CONFIG
            try:
                CONFIG = load_config()
                wchain = CONFIG.setdefault("whisper", {}).setdefault("providers", [])
                full = f"{base}/v1/audio/transcriptions"
                wchain = [p for p in wchain if p.get("url") != full]
                wchain.insert(0, {
                    "type": "fleet", "name": "custom-stt",
                    "url": full, "model": model, "timeout": 60,
                })
                CONFIG["whisper"]["providers"] = wchain
                CONFIG_PATH.write_text(
                    json.dumps(CONFIG, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
                _stt_healthy[0] = _probe_stt_primary()
                recorder.update_icon("gray")
                notify(f"STT endpoint saved: {model}"
                       + ("" if _stt_healthy[0] else " — endpoint not responding!"),
                       "Voice Hotkey")
                root.withdraw()
            except (OSError, ValueError) as e:
                log_error(f"Failed to save STT endpoint: {e}")
                notify(f"Save failed: {e}", "Voice Hotkey Error")

        def _close() -> None:
            root.withdraw()

        def _poll() -> None:
            if stt_open[0]:
                stt_open[0] = False
                stt_url_var.set(_current_base())
                stt_model_var.set(_current_model())
                stt_status.config(text="Enter your STT server, then Discover.",
                                  fg="#A6ADC8")
                root.deiconify()
                root.lift()
            try:
                kind, payload, source = stt_queue.get_nowait()
            except queue.Empty:
                pass
            else:
                if kind == "ok" and payload:
                    stt_model_combo["values"] = payload
                    if stt_model_var.get() not in payload:
                        stt_model_var.set(payload[0])
                    how = "via /v1/models" if source == "models" else "via /health"
                    stt_status.config(text=f"{len(payload)} model(s) found {how}",
                                      fg="#A6E3A1")
                elif kind == "ok":
                    stt_status.config(text="Server returned no models", fg="#F87171")
                else:
                    stt_status.config(text=f"Discovery failed: {payload[:80]}", fg="#F87171")
            root.after(80, _poll)

        root = tk.Tk()
        root.title("Voice Hotkey — STT Endpoint & Model")
        root.configure(bg="#1E1E2E")
        root.attributes("-topmost", True)
        frame = tk.Frame(root, bg="#1E1E2E", padx=14, pady=10)
        frame.pack()
        tk.Label(frame, text="STT Endpoint & Model", font=("Segoe UI", 11, "bold"),
                 fg="#CDD6F4", bg="#1E1E2E").pack(anchor="w")
        tk.Label(frame, text="OpenAI-compatible transcription server (Speaches,\n"
                             "faster-whisper wrapper, …). Discover probes /v1/models\n"
                             "then /health.",
                 font=("Segoe UI", 9), fg="#A6ADC8", bg="#1E1E2E",
                 justify="left").pack(anchor="w", pady=(2, 8))
        row = tk.Frame(frame, bg="#1E1E2E")
        row.pack(fill="x", pady=(0, 6))
        tk.Label(row, text="URL:", font=("Segoe UI", 9), fg="#A6ADC8",
                 bg="#1E1E2E").pack(side="left", padx=(0, 6))
        stt_url_var = tk.StringVar()
        tk.Entry(row, textvariable=stt_url_var, width=40, font=("Consolas", 9),
                 fg="#A6ADC8", bg="#2A2A3E", relief="flat",
                 insertbackground="#A6ADC8").pack(side="left", padx=(0, 8))
        tk.Button(row, text="Discover", font=("Segoe UI", 9, "bold"),
                  fg="#1E1E2E", bg="#89B4FA", activebackground="#B4D0FB",
                  relief="flat", padx=12, pady=2, command=_discover,
                  ).pack(side="left")
        row2 = tk.Frame(frame, bg="#1E1E2E")
        row2.pack(fill="x", pady=(0, 6))
        tk.Label(row2, text="Model:", font=("Segoe UI", 9), fg="#A6ADC8",
                 bg="#1E1E2E").pack(side="left", padx=(0, 6))
        stt_model_var = tk.StringVar()
        stt_model_combo = ttk.Combobox(row2, textvariable=stt_model_var, width=38)
        stt_model_combo.pack(side="left")
        stt_status = tk.Label(frame, text="", font=("Segoe UI", 9), fg="#A6ADC8",
                              bg="#1E1E2E", anchor="w")
        stt_status.pack(fill="x", pady=(2, 8))
        btns = tk.Frame(frame, bg="#1E1E2E")
        btns.pack(fill="x")
        tk.Button(btns, text="Save", font=("Segoe UI", 10, "bold"),
                  fg="#1E1E2E", bg="#A6E3A1", activebackground="#B8F0B0",
                  relief="flat", padx=16, pady=4, command=_save,
                  ).pack(side="left", padx=(0, 8))
        tk.Button(btns, text="Close", font=("Segoe UI", 10, "bold"),
                  fg="#CDD6F4", bg="#45475A", activebackground="#585B70",
                  relief="flat", padx=12, pady=4, command=_close,
                  ).pack(side="left")
        root.protocol("WM_DELETE_WINDOW", _close)
        root.withdraw()
        _poll()
        root.mainloop()

    threading.Thread(target=_run_stt_settings, daemon=True).start()

    # Health watch: poll the primary STT endpoint; red icon + warning on death.
    _stt_last_state: list[bool] = [True]

    def _stt_health_loop() -> None:
        while True:
            healthy = _probe_stt_primary()
            _stt_healthy[0] = healthy
            if healthy != _stt_last_state[0]:
                _stt_last_state[0] = healthy
                if healthy:
                    notify("Speech-to-text endpoint is back online", "Voice Hotkey")
                else:
                    notify("Speech-to-text endpoint unreachable — check STT configuration",
                           "Voice Hotkey Error")
                    _show_stt_warning()
                recorder.update_icon("gray")
            time.sleep(45)

    threading.Thread(target=_stt_health_loop, daemon=True).start()

    hotkey_label = hotkey_display(CONFIG.get("hotkey", "ctrl+alt+v"))
    menu = pystray.Menu(
        pystray.MenuItem(f"Voice Hotkey ({hotkey_label})", None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Show Last Transcription", on_show_last, default=True),
        pystray.MenuItem("Last Transcription -> PRD", on_prd_from_last),
        pystray.MenuItem("Change Hotkey…", on_change_hotkey),
        pystray.MenuItem("Edit PRD Prompt…", on_edit_prd_prompt),
        pystray.MenuItem("STT Endpoint & Model…", on_stt_settings),
        pystray.MenuItem("LLM Endpoint & Model…", on_llm_settings),
        pystray.MenuItem("Reload Config", on_reload_config),
        pystray.MenuItem("Restart", on_restart),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", on_quit),
    )

    icon = pystray.Icon(
        name="voice_hotkey",
        icon=make_icon("gray"),
        title=f"Voice Hotkey — {hotkey_label} to record",
        menu=menu,
    )
    recorder.set_tray(icon)

    # --- Double-Escape abort (in addition to pause/break) ---
    _last_esc: list[float] = [0.0]

    def _on_escape(e):
        if not recorder.is_recording:
            return
        now = time.time()
        if now - _last_esc[0] < 0.6:
            recorder.abort()
            _last_esc[0] = 0.0
        else:
            _last_esc[0] = now

    try:
        keyboard.on_press_key("escape", _on_escape, suppress=False)
    except Exception as e:
        log_error(f"Escape hook failed: {e}")

    # Direct Win32 low-level keyboard hook. This is the most reliable way
    # to catch a global key on Windows: the OS calls our callback in a
    # dedicated thread on every key event, no message pumping required,
    # and nothing in the user-mode app layer can swallow it. This is what
    # the user reported as "pagedown worked before" — let's lock it in.
    WH_KEYBOARD_LL = 13
    WM_KEYDOWN = 0x0100
    WM_SYSKEYDOWN = 0x0104

    # Map the configured hotkey to (modifiers mask, vk). The keyboard lib
    # + Win32 paths are flaky on this box; the low-level hook is not.
    _hotkey_mods_mask, _hotkey_vk = 0, 0
    _abort_vk = 0
    try:
        _hk_str = CONFIG.get("hotkey", "ctrl+alt+v")
        # Mouse-button hotkeys ("mouse:middle") have no VK; the mouse hook owns them.
        _hotkey_mods_mask, _hotkey_vk = (0, 0) if hotkey_is_mouse(_hk_str) else parse_hotkey(_hk_str)
        # Abort: double-press of the main hotkey within 600ms.
        _abort_vk = 0
    except Exception:
        _hotkey_mods_mask, _hotkey_vk = 0, 0

    # Double-pagedown detection
    _last_hotkey_press: list[float] = [0.0]
    _DOUBLE_PRESS_WINDOW = 0.6

    # Must keep a reference to the WINFUNCTYPE; ctypes will GC it otherwise
    # and the callback will crash the host process.
    _HOOKPROC = ctypes.WINFUNCTYPE(
        ctypes.c_int, ctypes.c_int, ctypes.wintypes.WPARAM, ctypes.POINTER(ctypes.c_void_p)
    )

    def _dispatch_hotkey_press() -> None:
        """Shared trigger logic for keyboard and mouse hotkeys."""
        now = time.time()
        double = now - _last_hotkey_press[0] < _DOUBLE_PRESS_WINDOW
        _last_hotkey_press[0] = 0.0 if double else now
        # Must dispatch, not call recorder methods directly: Windows
        # silently removes low-level hooks whose callback exceeds ~300ms,
        # and mic stream setup can take longer than that.
        if not _stt_healthy[0]:
            _show_stt_warning()
        threading.Thread(
            target=recorder.abort if double else recorder.toggle,
            daemon=True,
        ).start()
        try:
            with open(STATE_DIR / "_hook_debug.log", "a", encoding="utf-8") as f:
                f.write(f"[{time.strftime('%H:%M:%S')}] {CONFIG.get('hotkey', '?')} -> "
                        f"{'abort' if double else 'toggle'}\n")
        except OSError:
            pass

    def _mods_active() -> bool:
        """True when the currently-held modifiers equal the hotkey's.

        The LL hook only sees the vk code, so without this a hotkey like
        win+a would fire on every bare 'a' keystroke.
        """
        gs = ctypes.windll.user32.GetAsyncKeyState
        expected = [
            (MOD_CONTROL, gs(0x11) & 0x8000),   # VK_CONTROL
            (MOD_ALT, gs(0x12) & 0x8000),       # VK_MENU
            (MOD_SHIFT, gs(0x10) & 0x8000),     # VK_SHIFT
            (MOD_WIN, (gs(0x5B) | gs(0x5C)) & 0x8000),  # LWIN/RWIN
        ]
        return all(held == bool(_hotkey_mods_mask & mod) for mod, held in expected)

    def _low_level_keyboard_proc(nCode, wParam, lParam):
        try:
            if nCode == 0 and wParam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                vk = ctypes.cast(lParam, ctypes.POINTER(ctypes.c_int))[0]
                if (vk == _hotkey_vk and _hotkey_vk != 0
                        and not _picker_capturing[0] and _mods_active()):
                    _dispatch_hotkey_press()
                    return 1
        except Exception as e:
            log_error(f"keyboard hook error: {e}")
        return ctypes.windll.user32.CallNextHookEx(0, nCode, wParam, lParam)

    _low_level_keyboard_proc_ref = _HOOKPROC(_low_level_keyboard_proc)
    _hook_handle = ctypes.windll.user32.SetWindowsHookExW(
        WH_KEYBOARD_LL,
        _low_level_keyboard_proc_ref,
        0,  # hMod must be 0 for low-level hooks
        0,  # dwThreadId 0 = hook all threads
    )
    if not _hook_handle:
        log_error(f"SetWindowsHookExW(WH_KEYBOARD_LL) failed: {ctypes.GetLastError()}")
        notify("Hotkey hook failed — check logs", "Voice Hotkey Error")
    else:
        # Inform the user (via a custom-overlay toast) what to press
        notify(
            f"Press {hotkey_label} to record — double-press to abort",
            "Voice Hotkey active",
            timeout=4,
        )

    # --- Mouse-button trigger (global hook via the `mouse` package) ---
    _mouse_btn: list[str | None] = [mouse_button_of(CONFIG.get("hotkey", ""))]
    _last_mouse_evt: list[float] = [0.0]

    def _mouse_proc(event):
        # mouse.hook delivers MoveEvent/WheelEvent too; only ButtonEvent
        # has event_type/button. A rapid second click arrives as 'double',
        # not 'down', so both count as a press (dedupe window in case a
        # platform emits both). The mouse package CANNOT suppress events,
        # and any truthy return would cut off handlers registered after
        # this one (e.g. the picker's capture hook) — always return None.
        if mouse is None or not isinstance(event, mouse.ButtonEvent):
            return None
        if event.event_type not in ("down", "double") or _picker_capturing[0]:
            return None
        if _mouse_btn[0] and event.button == _mouse_btn[0]:
            now = time.time()
            if now - _last_mouse_evt[0] < 0.1:
                return None
            _last_mouse_evt[0] = now
            _dispatch_hotkey_press()
        return None

    if mouse is not None:
        mouse.hook(_mouse_proc)
        if _mouse_btn[0]:
            notify("Mouse trigger active", "Voice Hotkey", timeout=2)
    elif _mouse_btn[0]:
        log_error("Hotkey is a mouse button but the `mouse` package is missing "
                  "(pip install mouse) — trigger will not fire")

    def _hotkey_poll() -> None:
        """Watch for config reload and update the active trigger targets."""
        while True:
            if _hotkey_reload_event.is_set():
                _hotkey_reload_event.clear()
                nonlocal _hotkey_vk, _hotkey_mods_mask
                try:
                    hk = CONFIG.get("hotkey", "ctrl+alt+v")
                    _hotkey_mods_mask, _hotkey_vk = (0, 0) if hotkey_is_mouse(hk) else parse_hotkey(hk)
                    _mouse_btn[0] = mouse_button_of(hk)
                except Exception:
                    _hotkey_mods_mask, _hotkey_vk = 0, 0
                    _mouse_btn[0] = None
            time.sleep(0.5)

    threading.Thread(target=_hotkey_poll, daemon=True).start()

    # Preload Whisper only if explicitly enabled or no fast provider first
    if CONFIG.get("preload_whisper", True):
        threading.Thread(target=_preload_whisper, daemon=True).start()

    notify(
        f"Press {hotkey_label} to record — pause/break aborts",
        "Voice Hotkey active",
        timeout=4,
    )

    try:
        icon.run()
    finally:
        unregister_system_hotkey()


def _safe_read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


if __name__ == "__main__":
    import traceback

    def _log_uncaught_thread(args) -> None:
        # threading.excepthook — pythonw has no stderr, so an unhandled
        # exception on any worker thread would otherwise vanish silently.
        try:
            log_error("FATAL (thread): " + "".join(
                traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)))
        except Exception:
            pass

    threading.excepthook = _log_uncaught_thread
    try:
        main()
    except Exception:
        log_error("FATAL (main): " + traceback.format_exc())
        raise

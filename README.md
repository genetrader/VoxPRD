# Voice Hotkey

Local-first dictation tool. Press a global hotkey (or mouse button),
talk — and the text lands wherever you want it: your clipboard, the field
you're typing in, or straight into another app (your AI chat, an editor,
anywhere) with Enter included. Or it becomes a structured PRD via your
own local LLM.

## Features

- **Global hotkey** with Win32 `RegisterHotKey` + `keyboard`-lib fallback.
  Works through Google Remote Desktop (`scroll_lock`, `pause`, `ctrl+space`
  pass the GRD filter; `ctrl+alt+v` does not).
- **Live waveform overlay** while recording.
- **Whisper transcription** via a configurable provider chain
  (fleet endpoint → OpenAI → local). Retry honors `Retry-After` and uses
  exponential backoff with jitter.
- **Copy / PRD choice overlay** after transcription (with
  `auto_send_timeout` > 0 it auto-copies instead of waiting for a click).
  When a PRD finishes, the overlay tints **light yellow** with "✅ PRD
  Ready" and stays open as a visual cue; its Copy button then copies the
  PRD itself. `prd_auto_copy_to_clipboard` (default true) toggles the
  automatic clipboard copy of finished PRDs.
- **Settings submenu** — one place for configuration: *Options…*
  (checkboxes for auto-copy, **auto-paste into the focused field** with an
  optional **Enter-after-paste** for chat boxes, PRD auto-copy, the memo
  popup's auto-close timeout — default 60s, 0 = keep open — and start/end
  **tone presets** with live preview: Classic, Chime, Warm Hum, Bloom,
  Zen Bell), *Change Hotkey…*, *STT/LLM Endpoint & Model…*, *Edit PRD
  Prompt…*, and *Reload Config*. The memo popup can safely close itself:
  the text is already on the clipboard and saved to
  `state/last_transcription.txt`; PRD generating/done states never
  auto-close.
- **Route before you speak** — bind hotkeys to destinations: one trigger
  pastes straight into your AI chat with Enter, another goes directly to
  PRD (no popup), each with a live "● REC →" tag. A router hotkey opens a
  small palette to pick. Or skip keys entirely: say the route's spoken
  alias as the first word — "zed: fix the login bug" — and it's routed,
  prefix stripped. "scratch that" discards.
- **Paste targets — dictate into another app while you keep working.**
  Whitelist the apps that should receive your transcriptions (Settings →
  *Paste Target…*). Keep typing in your editor, hit the hotkey, talk —
  and the text lands in your AI assistant's chat box and sends itself,
  then focus snaps back to where you were. Picks the first running app
  in priority order (its most-recently-used window when several are
  open), with per-app Enter control, and falls back to the focused field
  (with a toast) when none are running.
- **Editable PRD prompt** — tray → *Edit PRD Prompt…* edits the PRD system
  prompt; stored as `prd_prompt` in `config.json` (null = built-in default).
- **LLM endpoint + model picker** — tray → *LLM Endpoint & Model…*: type an
  IP:port, hit *Discover* (queries `/v1/models` on any OpenAI-compatible
  server), pick a model; saved as the first `prd` provider.
- **STT endpoint picker + health watch** — tray → *STT Endpoint & Model…*:
  same discovery for the transcription server (probes `/v1/models`, then
  `/health` for plain faster-whisper wrappers). A background poll turns
  the tray icon **red** when the primary STT endpoint stops answering,
  toasts on the change, and pressing the hotkey while it's down shows a
  dismissible "check your transcription configuration" panel.
- **PRD generation** via a configurable provider chain
  (DeepSeek V4 Flash local → GLM/GRM local → OpenAI). Detailed
  prompt in `prompts.py`.
- **Pause/break** aborts a recording. **Double-Escape** also works.
- **Hotkey picker** — tray menu → *Change Hotkey…*: press any key combo or
  click any mouse button (middle and side buttons included), then Save.
  Clear re-listens for a fresh choice; Cancel discards. Saved to
  `config.json` and applied live.
- **State split**: secrets in `secrets/`, runtime state in `state/`, source
  in the project root. No `last_*.txt` or `recordings/` clutter at the
  root.

## Layout

```
voice-hotkey/
├── voice_hotkey.py        # main app
├── prompts.py             # system prompts (PRD, routing)
├── providers.py           # Whisper + PRD provider chains
├── appsecrets.py             # tiny secrets loader
├── config.json            # active config (channel IDs, endpoints)
├── config.json.template   # reference config
├── .gitignore
├── tests/                 # pytest suite
├── secrets/.env           # gitignored; DISCORD_BOT_TOKEN, OPENAI_API_KEY
├── state/                 # gitignored; recordings, last text, logs
└── launchers/             # VBS + BAT to start, stop, install
```

## Setup

```bat
install.bat
```

Creates `.venv`, installs deps, and migrates a legacy `.env` into
`secrets/.env`. Then create `secrets\.env` with:

```ini
OPENAI_API_KEY=sk-...   # optional: cloud fallback for transcription/PRD
```

`DISCORD_BOT_TOKEN` is no longer used (the Discord relay and its routing
were removed entirely); delete it from `secrets/.env` if present.

Copy `config.json.template` to `config.json` and edit the endpoints.

## Run

```bat
start.bat                    :: visible console (good for debugging)
start-hidden.vbs             :: fully hidden, no console flash
```

Drop `Voice Hotkey - Launch.bat` (or a shortcut to `start-hidden.vbs`)
on your Desktop. For auto-start at login: `Win+R` → `shell:startup` →
paste a shortcut to `start-hidden.vbs`.

## Config

```jsonc
{
  "hotkey": "pagedown",                  // primary hotkey
  "abort_hotkey": "pause",               // abort hotkey (works through GRD)
  "auto_send_timeout": 0,                // 0 = wait for click; N = auto-copy after Ns
  "auto_copy_to_clipboard": true,        // transcription goes on the clipboard immediately
  "prd_prompt": null,                    // custom PRD system prompt (tray → Edit PRD Prompt…)
  "preload_whisper": true,               // warm local Whisper at startup

  "whisper": {
    "providers": [
      { "type": "fleet",  "name": "my-whisper-server", "url": "http://YOUR_WHISPER_HOST:8000/v1/audio/transcriptions", "timeout": 60 },
      { "type": "openai", "name": "openai",       "model": "whisper-1", "retries": 3 },
      { "type": "local",  "name": "local",        "model": "base" }
    ]
  },

  "prd": {
    "providers": [
      { "type": "openai_compatible", "name": "my-local-llm", "url": "http://YOUR_LLM_HOST:8000/v1", "model": "YOUR_MODEL", "max_tokens": 16384 },
      { "type": "openai",             "name": "openai",   "model": "gpt-4o" }
    ]
  }
}
```

## Adding a Whisper endpoint

Any OpenAI-compatible `/v1/audio/transcriptions` server works:

```jsonc
{ "type": "fleet", "name": "my-whisper", "url": "http://host:port/v1/audio/transcriptions", "model": "whisper-large-v3", "timeout": 60 }
```

If `url` is on a private network (RFC1918), no API key is required. The
chain tries providers in order, so put your preferred one first.

## Fresh install / sharing the app

Transcription runs on a provider chain (fleet endpoint → OpenAI cloud →
local Whisper). On a machine with no fleet GPU box:

1. Run `install.bat`, then set the **local** provider's model to the large
   distilled model for near-large accuracy at usable CPU speed:

   ```jsonc
   { "type": "local", "name": "local-whisper", "model": "large-v3-turbo" }
   ```

   Library: [openai/whisper](https://github.com/openai/whisper) —
   `large-v3-turbo` is the 8-decoder-layer distillation of large-v3.
2. Much faster if you have any CUDA box: serve
   [SYSTRAN/faster-whisper](https://github.com/SYSTRAN/faster-whisper)
   `large-v3-turbo` behind an OpenAI-compatible `/v1/audio/transcriptions`
   endpoint and add it as a `fleet` provider — recipe in
   `C:\coding\fleet-gpu3060.md` (the default config points at one already).
3. For PRD generation: tray → **LLM Endpoint & Model…**, type the box's
   `IP:port`, hit **Discover**, pick a model. Any OpenAI-compatible server
   works (llama.cpp server, vLLM, LM Studio, llama-serve…).

## Tests

```bash
.venv\Scripts\python.exe -m pytest tests\ -v
```

## Hotkey reference

| Key              | Notes                                  |
|------------------|----------------------------------------|
| `ctrl+space`     | Workable, may collide with IME on Win  |
| `scroll_lock`    | Passes Google Remote Desktop filter    |
| `pause` / `break`| Aborts recording (single press)        |
| `pagedown`       | Default in this project                |
| `f1`..`f12`      | F-keys generally work, no GRD issues   |
| `numpad0`..`9`   | Numpad keys                            |

Mouse buttons work as triggers too. In `config.json` set
`"hotkey": "mouse:middle"` (also `mouse:left`, `mouse:right`, `mouse:x` /
`mouse:x2` for the side buttons), or pick one live with **Change Hotkey…**
in the tray menu. The button keeps its normal behavior while assigned
(the mouse library can't swallow events), so a side button like
`mouse:x` is the least intrusive choice. A quick double-click aborts,
same as the keyboard. `win+…` combos are rejected as hotkeys —
they're reserved by the OS.

## Troubleshooting

- **No transcription in the log** — the error log is at
  `state/voice_hotkey_errors.log`. Per-recording errors are saved next to
  the WAV in `state/recordings/`.
- **429 spam** — your OpenAI key is being rate-limited. The chain will fall
  through to local Whisper automatically; check `state/voice_hotkey_errors.log`
  for retry counts.
- **Hotkey won't fire inside Google Remote Desktop** — use `scroll_lock`,
  `pause`, or `ctrl+space`. `ctrl+alt+v` is intercepted by GRD.

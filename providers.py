"""Whisper transcription + PRD generation provider chains.

Each chain is a list of provider configs from config.json. We try them in
order, with exponential backoff + Retry-After honoring for the cloud
providers. The first provider that returns a non-empty result wins.

Adding a new provider:
  - Whisper: extend `_transcribe_<type>`. Supported: fleet, openai, local.
  - PRD:     extend `_prd_<type>`.    Supported: openai_compatible, openai.

The fleet Whisper endpoint is just an OpenAI-compatible
/audio/transcriptions endpoint. Configure it like:
  { "type": "fleet", "url": "http://host:port/v1/audio/transcriptions",
    "model": "whisper-large-v3" }

The local PRD endpoint is an OpenAI-compatible /v1/chat/completions endpoint
(DeepSeek V4 Flash, GLM, GRM, etc.). The system prompt is owned by
prompts.py.
"""

from __future__ import annotations

import json
import random
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import requests

from prompts import PRD_SYSTEM_PROMPT
from appsecrets import get as get_secret


# ---------------------------------------------------------------------------
# Backoff
# ---------------------------------------------------------------------------
def compute_backoff(attempt: int, retry_after: float | None = None) -> float:
    """Sleep duration before next retry.

    Strategy:
      - If server returned a Retry-After header (any value, even negative
        or garbage), honor it by clamping to [0, 60s]. Negative / unparseable
        values are treated as "retry immediately" (0s), not as "ignore".
      - Otherwise exponential backoff with full jitter, capped at 30s.
        attempt=1 -> [0,1), attempt=2 -> [0,2), attempt=3 -> [0,4), ...
    """
    if retry_after is not None:
        return min(60.0, max(0.0, float(retry_after)))
    base = min(30.0, 2 ** (attempt - 1))
    return random.uniform(0.0, base)


def _parse_retry_after(resp: requests.Response) -> float | None:
    """Pull Retry-After from a response, in seconds. None if absent."""
    val = resp.headers.get("Retry-After")
    if not val:
        return None
    try:
        return float(val)
    except ValueError:
        # HTTP-date form is allowed by RFC 7231; skip for now.
        return None


# ---------------------------------------------------------------------------
# Whisper
# ---------------------------------------------------------------------------
@dataclass
class WhisperResult:
    text: str
    provider: str
    duration_s: float = 0.0


class _WhisperLocalCache:
    """Lazy + thread-safe loader for openai-whisper models."""
    _lock = threading.Lock()
    _model = None

    @classmethod
    def get(cls, model_name: str):
        if cls._model is None:
            with cls._lock:
                if cls._model is None:
                    import whisper  # imported here to avoid 5s cold start
                    cls._model = whisper.load_model(model_name)
        return cls._model


def _transcribe_local(audio_path: str, cfg: dict) -> str:
    model_name = cfg.get("model", "base")
    model = _WhisperLocalCache.get(model_name)
    result = model.transcribe(
        audio_path,
        language="en",
        fp16=False,  # fp32 for Windows stability
    )
    return (result.get("text") or "").strip()


def _transcribe_openai_compatible(
    audio_path: str,
    cfg: dict,
    *,
    openai_endpoint: str,
    api_key: str | None,
    auth_header_factory: Callable[[str], dict] | None = None,
) -> str:
    """Shared implementation for OpenAI and OpenAI-compatible /audio/transcriptions endpoints."""
    url = cfg.get("url") or openai_endpoint
    if not url:
        raise ValueError("No transcription URL configured")
    # Public internet endpoints require a real key; private-network
    # endpoints (RFC1918, loopback) generally don't.
    url_lc = url.lower()
    is_private = (
        url_lc.startswith("http://192.168.")
        or url_lc.startswith("http://10.")
        or url_lc.startswith("http://127.")
        or url_lc.startswith("http://localhost")
    )
    if not api_key and not is_private:
        raise ValueError(f"No API key for public endpoint {url}")

    timeout = float(cfg.get("timeout", 60))
    model = cfg.get("model", "whisper-1")
    headers = {"Authorization": f"Bearer {api_key or 'local'}"}
    if auth_header_factory:
        headers = auth_header_factory(api_key or "")

    with open(audio_path, "rb") as f:
        files = {"file": (Path(audio_path).name, f, "audio/wav")}
        data = {"model": model, "language": "en", "response_format": "text"}
        resp = requests.post(url, headers=headers, files=files, data=data, timeout=timeout)
    resp.raise_for_status()
    body = (resp.text or "").strip()
    # Many OpenAI-compatible servers ignore `response_format: text` and return
    # a JSON envelope `{"text": "..."}`. Handle both shapes.
    if body.startswith("{") and body.endswith("}"):
        try:
            payload = json.loads(body)
            if isinstance(payload, dict) and "text" in payload:
                return str(payload["text"]).strip()
        except (json.JSONDecodeError, ValueError):
            pass
    return body


def transcribe_with_retry(audio_path: str, chain: list[dict], *, log=None) -> WhisperResult | None:
    """Try each provider in `chain` until one returns text.

    For cloud providers, retries with Retry-After + exponential jitter.
    For local, single-shot (no retry — a local model rarely needs it).
    Returns None if everything fails.
    """
    started = time.time()
    for cfg in chain:
        ptype = cfg.get("type")
        name = cfg.get("name", ptype or "unknown")
        try:
            if ptype == "local":
                text = _transcribe_local(audio_path, cfg)
            elif ptype == "openai":
                api_key = get_secret("OPENAI_API_KEY")
                if not api_key:
                    if log: log(f"Skipping {name}: no OPENAI_API_KEY")
                    continue
                retries = int(cfg.get("retries", 3))
                last_err: Exception | None = None
                for attempt in range(1, retries + 1):
                    try:
                        text = _transcribe_openai_compatible(
                            audio_path, cfg,
                            openai_endpoint="https://api.openai.com/v1/audio/transcriptions",
                            api_key=api_key,
                        )
                        last_err = None
                        break
                    except requests.HTTPError as e:
                        last_err = e
                        if e.response is not None and e.response.status_code == 429:
                            sleep_for = compute_backoff(attempt, _parse_retry_after(e.response))
                            if log: log(f"{name} 429 — sleeping {sleep_for:.1f}s (attempt {attempt}/{retries})")
                            time.sleep(sleep_for)
                            continue
                        # Non-429 HTTP error — log and move on to next provider
                        if log: log(f"{name} HTTP {e.response.status_code if e.response else '?'}: {e}")
                        break
                    except requests.RequestException as e:
                        last_err = e
                        sleep_for = compute_backoff(attempt)
                        if log: log(f"{name} network error: {e} — sleeping {sleep_for:.1f}s (attempt {attempt}/{retries})")
                        time.sleep(sleep_for)
                        continue
                else:
                    text = ""
                if last_err and not text:
                    if log: log(f"{name} failed after {retries} attempts: {last_err}")
                    continue
            elif ptype == "fleet":
                # Generic OpenAI-compatible /audio/transcriptions endpoint
                api_key = get_secret("FLEET_API_KEY") or "local"
                retries = int(cfg.get("retries", 2))
                last_err: Exception | None = None
                for attempt in range(1, retries + 1):
                    try:
                        text = _transcribe_openai_compatible(
                            audio_path, cfg,
                            openai_endpoint="",  # not used; cfg["url"] is authoritative
                            api_key=api_key,
                        )
                        last_err = None
                        break
                    except requests.HTTPError as e:
                        last_err = e
                        if e.response is not None and e.response.status_code == 429:
                            sleep_for = compute_backoff(attempt, _parse_retry_after(e.response))
                            if log: log(f"{name} 429 — sleeping {sleep_for:.1f}s (attempt {attempt}/{retries})")
                            time.sleep(sleep_for)
                            continue
                        if log: log(f"{name} HTTP {e.response.status_code if e.response else '?'}: {e}")
                        break
                    except requests.RequestException as e:
                        last_err = e
                        sleep_for = compute_backoff(attempt)
                        if log: log(f"{name} network error: {e} — sleeping {sleep_for:.1f}s (attempt {attempt}/{retries})")
                        time.sleep(sleep_for)
                        continue
                else:
                    text = ""
                if last_err and not text:
                    if log: log(f"{name} failed after {retries} attempts: {last_err}")
                    continue
            else:
                if log: log(f"Unknown provider type {ptype!r} — skipping")
                continue

            if text:
                return WhisperResult(text=text, provider=name, duration_s=time.time() - started)
        except Exception as e:
            if log: log(f"{name} threw: {e}")
            continue

    return None


# ---------------------------------------------------------------------------
# PRD
# ---------------------------------------------------------------------------
@dataclass
class PrdResult:
    text: str
    provider: str
    duration_s: float = 0.0


def _prd_openai_compatible(transcription: str, cfg: dict) -> str:
    """Hit an OpenAI-compatible /v1/chat/completions endpoint with the PRD system prompt."""
    url = cfg["url"].rstrip("/")
    if not url.endswith("/chat/completions"):
        url = f"{url}/chat/completions"
    api_key = get_secret("OPENAI_API_KEY") if "openai" in cfg.get("name", "").lower() and "192" not in url and "127" not in url else get_secret("FLEET_API_KEY") or "local"
    model = cfg.get("model", "gpt-4o")
    timeout = float(cfg.get("timeout", 300))
    max_tokens = int(cfg.get("max_tokens", 16384))
    temperature = float(cfg.get("temperature", 0.3))
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": cfg.get("system_prompt") or PRD_SYSTEM_PROMPT},
            {"role": "user", "content": transcription},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def _prd_openai(transcription: str, cfg: dict) -> str:
    api_key = get_secret("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("OPENAI_API_KEY not set")
    url = "https://api.openai.com/v1/chat/completions"
    model = cfg.get("model", "gpt-4o")
    timeout = float(cfg.get("timeout", 300))
    max_tokens = int(cfg.get("max_tokens", 16384))
    temperature = float(cfg.get("temperature", 0.3))
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": cfg.get("system_prompt") or PRD_SYSTEM_PROMPT},
            {"role": "user", "content": transcription},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def generate_prd(transcription: str, chain: list[dict], *, log=None) -> PrdResult | None:
    """Try each PRD provider in `chain` until one returns text. Returns None on full failure."""
    started = time.time()
    for cfg in chain:
        ptype = cfg.get("type")
        name = cfg.get("name", ptype or "unknown")
        try:
            if ptype == "openai_compatible":
                text = _prd_openai_compatible(transcription, cfg)
            elif ptype == "openai":
                text = _prd_openai(transcription, cfg)
            else:
                if log: log(f"Unknown PRD provider type {ptype!r} — skipping")
                continue
            if text:
                return PrdResult(text=text, provider=name, duration_s=time.time() - started)
        except requests.HTTPError as e:
            if log: log(f"{name} HTTP {e.response.status_code if e.response else '?'}: {e}")
        except requests.RequestException as e:
            if log: log(f"{name} network: {e}")
        except Exception as e:
            if log: log(f"{name} threw: {e}")
    return None

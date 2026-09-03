"""Tests for the Whisper + PRD provider chains.

Strategy: monkeypatch `requests.post` to return canned responses. This lets
us verify the chain logic (ordering, fallbacks, retry/backoff behavior)
without hitting the network.
"""

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, mock_open, patch

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import providers
from providers import (
    PrdResult,
    WhisperResult,
    _parse_retry_after,
    compute_backoff,
    generate_prd,
    transcribe_with_retry,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def fake_audio_path(tmp_path):
    """A real file the open() call inside _transcribe_openai_compatible needs."""
    p = tmp_path / "audio.wav"
    p.write_bytes(b"RIFF\x00\x00\x00\x00WAVE")
    return str(p)


@pytest.fixture(autouse=True)
def _clear_secrets_cache():
    """The secrets loader caches os.environ on first call. Reset between tests."""
    import appsecrets as _secrets
    _secrets.reset_cache()
    yield
    _secrets.reset_cache()


# ---------------------------------------------------------------------------
# Backoff
# ---------------------------------------------------------------------------
def test_retry_after_honored_and_capped():
    # Server says 30s, we cap at 60
    assert compute_backoff(attempt=1, retry_after=30) == 30
    # Server says 999s, we cap at 60
    assert compute_backoff(attempt=1, retry_after=999) == 60
    # Server says -1 (garbage), we clamp to 0
    assert compute_backoff(attempt=1, retry_after=-1) == 0


def test_no_retry_after_uses_exponential_jitter():
    # Each call should produce a value in [0, 2^(attempt-1))
    for attempt in range(1, 6):
        for _ in range(50):
            v = compute_backoff(attempt=attempt)
            cap = 2 ** (attempt - 1)
            assert 0.0 <= v < cap


def test_parse_retry_after_seconds():
    resp = MagicMock()
    resp.headers = {"Retry-After": "42"}
    assert _parse_retry_after(resp) == 42.0


def test_parse_retry_after_absent():
    resp = MagicMock()
    resp.headers = {}
    assert _parse_retry_after(resp) is None


def test_parse_retry_after_garbage():
    resp = MagicMock()
    resp.headers = {"Retry-After": "not-a-number"}
    assert _parse_retry_after(resp) is None


# ---------------------------------------------------------------------------
# Whisper chain
# ---------------------------------------------------------------------------
def _mock_resp(status: int, text: str = "", headers: dict | None = None):
    r = MagicMock(spec=requests.Response)
    r.status_code = status
    r.text = text
    r.headers = headers or {}
    if status < 400:
        r.raise_for_status = MagicMock()
    else:
        r.raise_for_status = MagicMock(side_effect=requests.HTTPError(response=r))
    return r


def test_whisper_first_provider_succeeds(fake_audio_path):
    chain = [
        {"type": "fleet", "name": "primary", "url": "http://192.168.1.127:9000/v1/audio/transcriptions", "model": "whisper-1"},
        {"type": "local", "name": "fallback", "model": "base"},
    ]
    with patch("providers.requests.post", return_value=_mock_resp(200, "hello world")) as p:
        result = transcribe_with_retry(fake_audio_path, chain)
    assert result is not None
    assert result.text == "hello world"
    assert result.provider == "primary"
    assert p.call_count == 1


def test_whisper_falls_through_on_empty(fake_audio_path):
    chain = [
        {"type": "fleet", "name": "primary", "url": "http://192.168.1.127:9000/v1/audio/transcriptions", "model": "w"},
        {"type": "local", "name": "fallback", "model": "base"},
    ]
    with patch("providers.requests.post", return_value=_mock_resp(200, "")), \
         patch.object(providers, "_WhisperLocalCache") as local_cache:
        local_cache.get.return_value.transcribe.return_value = {"text": "from second"}
        result = transcribe_with_retry(fake_audio_path, chain)
    assert result is not None
    assert result.text == "from second"
    assert result.provider == "fallback"


def test_whisper_429_triggers_backoff_and_eventually_succeeds(monkeypatch, fake_audio_path):
    chain = [
        {"type": "openai", "name": "openai", "model": "whisper-1", "retries": 3},
    ]
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    sleeps: list[float] = []
    monkeypatch.setattr("providers.time.sleep", lambda s: sleeps.append(s))

    with patch("providers.requests.post", side_effect=[
        _mock_resp(429, headers={"Retry-After": "1"}),
        _mock_resp(200, "transcribed after retry"),
    ]):
        result = transcribe_with_retry(fake_audio_path, chain)
    assert result is not None
    assert result.text == "transcribed after retry"
    assert len(sleeps) >= 1
    # First sleep should be capped version of Retry-After
    assert sleeps[0] == 1.0


def test_whisper_429_exhausts_retries_and_returns_none(monkeypatch, fake_audio_path):
    chain = [
        {"type": "openai", "name": "openai", "model": "whisper-1", "retries": 2},
    ]
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr("providers.time.sleep", lambda s: None)

    with patch("providers.requests.post", side_effect=[
        _mock_resp(429, headers={"Retry-After": "0"}),
        _mock_resp(429, headers={"Retry-After": "0"}),
    ]):
        result = transcribe_with_retry(fake_audio_path, chain)
    assert result is None


def test_whisper_no_chain_returns_none(fake_audio_path):
    assert transcribe_with_retry(fake_audio_path, []) is None


def test_whisper_openai_skipped_without_key(monkeypatch, fake_audio_path, tmp_path):
    """When OPENAI_API_KEY is missing, the openai provider is skipped and the
    chain falls through to the next provider (fleet)."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    # Point secrets at an empty tmp dir so secrets/.env from the project can't
    # supply a real key.
    import appsecrets as _secrets
    monkeypatch.setattr(_secrets, "load_env", lambda app_dir=None: {})
    chain = [
        {"type": "openai", "name": "openai", "model": "whisper-1"},
        {"type": "fleet", "name": "fleet", "url": "http://192.168.1.127:9000/v1/audio/transcriptions"},
    ]
    with patch("providers.requests.post", return_value=_mock_resp(200, "ok")) as p:
        result = transcribe_with_retry(fake_audio_path, chain)
    assert result is not None
    assert result.provider == "fleet"
    # Only the fleet call happened
    assert p.call_count == 1


# ---------------------------------------------------------------------------
# PRD chain
# ---------------------------------------------------------------------------
def test_prd_first_provider_succeeds():
    chain = [
        {"type": "openai_compatible", "name": "deepseek", "url": "http://192.168.1.127:8888/v1", "model": "ds"},
    ]
    payload = {
        "choices": [{"message": {"content": "# PRD\n\nGood stuff."}}]
    }
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = payload

    with patch("providers.requests.post", return_value=resp) as p:
        result = generate_prd("voice text", chain)
    assert result is not None
    assert "Good stuff" in result.text
    assert result.provider == "deepseek"
    assert p.call_count == 1


def test_prd_falls_through_on_failure(monkeypatch, tmp_path):
    chain = [
        {"type": "openai_compatible", "name": "deepseek", "url": "http://192.168.1.127:8888/v1", "model": "ds"},
        {"type": "openai", "name": "openai", "model": "gpt-4o"},
    ]
    fail = MagicMock()
    fail.raise_for_status.side_effect = requests.HTTPError("boom")

    good_payload = {"choices": [{"message": {"content": "# Backup PRD"}}]}
    good = MagicMock()
    good.raise_for_status = MagicMock()
    good.json.return_value = good_payload

    monkeypatch.setenv("OPENAI_API_KEY", "test")

    with patch("providers.requests.post", side_effect=[fail, good]):
        result = generate_prd("voice text", chain)
    assert result is not None
    assert "Backup PRD" in result.text
    assert result.provider == "openai"


def test_prd_returns_none_when_all_fail():
    chain = [
        {"type": "openai_compatible", "name": "ds", "url": "http://192.168.1.127:8888/v1", "model": "ds"},
    ]
    fail = MagicMock()
    fail.raise_for_status.side_effect = requests.RequestException("network")
    with patch("providers.requests.post", return_value=fail):
        result = generate_prd("voice text", chain)
    assert result is None


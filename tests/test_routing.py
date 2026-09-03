"""Tests for the Discord channel routing logic.

Routing is first-match-wins over the order in config.json. The PRD generator
and Discord poster both rely on this — wrong routing = wrong agent gets
the voice memo.
"""

import sys
from pathlib import Path

# Allow `from providers import ...` to work when running pytest from the
# project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

# Import the live module under test. We patch CONFIG via monkeypatch.
import voice_hotkey


@pytest.fixture
def sample_config():
    return {
        "routing_rules": [
            {
                "name": "screenplay",
                "channel_id": "1",
                "target_agent": "screenplay",
                "keywords": ["screenplay", "script", "scene", "draft", "outline", "dialogue"],
            },
            {
                "name": "fleet",
                "channel_id": "2",
                "target_agent": "fleet",
                "keywords": ["fleet", "homelab", "tts box", "rvc box", "ssh", "worker"],
            },
            {
                "name": "coding",
                "channel_id": "3",
                "target_agent": "bastion",
                "keywords": ["bastion", "build", "fix", "deploy", "research"],
            },
        ],
        "default_channel": {
            "name": "general",
            "channel_id": "0",
            "target_agent": "general",
        },
    }


def test_screenplay_match(sample_config, monkeypatch):
    monkeypatch.setattr(voice_hotkey, "CONFIG", sample_config)
    rule = voice_hotkey.route_message("Let's work on the screenplay draft and outline.")
    assert rule["name"] == "screenplay"
    assert rule["target_agent"] == "screenplay"


def test_fleet_match(sample_config, monkeypatch):
    monkeypatch.setattr(voice_hotkey, "CONFIG", sample_config)
    rule = voice_hotkey.route_message("Check the homelab ssh — fleet ops need attention")
    assert rule["name"] == "fleet"
    assert rule["target_agent"] == "fleet"


def test_coding_match(sample_config, monkeypatch):
    monkeypatch.setattr(voice_hotkey, "CONFIG", sample_config)
    rule = voice_hotkey.route_message("Build a thing to fix the deployment pipeline")
    # First-match-wins: "build" / "fix" / "deploy" all match coding
    assert rule["name"] == "coding"
    assert rule["target_agent"] == "bastion"


def test_default_when_no_match(sample_config, monkeypatch):
    monkeypatch.setattr(voice_hotkey, "CONFIG", sample_config)
    rule = voice_hotkey.route_message("hello world this matches nothing")
    assert rule["name"] == "general"
    assert rule["target_agent"] == "general"


def test_first_match_wins(sample_config, monkeypatch):
    """A keyword that appears in multiple rules goes to whichever is first."""
    cfg = dict(sample_config)
    cfg["routing_rules"] = [
        {**sample_config["routing_rules"][2]},  # coding
        {**sample_config["routing_rules"][0], "keywords": ["build", "screenplay"]},
    ]
    monkeypatch.setattr(voice_hotkey, "CONFIG", cfg)
    rule = voice_hotkey.route_message("let's work on the screenplay and build it")
    assert rule["name"] == "coding"  # first match wins


def test_case_insensitive(sample_config, monkeypatch):
    monkeypatch.setattr(voice_hotkey, "CONFIG", sample_config)
    rule = voice_hotkey.route_message("GOT TO DEPLOY THIS NOW")
    assert rule["name"] == "coding"


def test_keyword_substring_match(sample_config, monkeypatch):
    """'tts box' should match 'the tts box is down' because of `in` check."""
    monkeypatch.setattr(voice_hotkey, "CONFIG", sample_config)
    rule = voice_hotkey.route_message("the tts box is down again")
    assert rule["name"] == "fleet"

import copy

import pytest
import yaml

from stream_clipper.config import ConfigError, load_config, parse_config

from .conftest import EXAMPLE_CONFIG


@pytest.fixture
def raw():
    return yaml.safe_load(EXAMPLE_CONFIG.read_text())


def test_example_config_is_valid():
    cfg = load_config(EXAMPLE_CONFIG)
    assert "kaicenat" in cfg.enabled_sources("live")
    assert cfg.sources["kaicenat"].resolved_chat_backend() == "twitch_irc"
    assert cfg.g.workers.cpu_workers == 3
    assert cfg.campaigns["Kai"].required_hashtags == ["#FILL_ME"]
    # placeholders are reported, not fatal
    assert any("FILL_ME" in w for w in cfg.warnings())


def test_defaults_minimal():
    cfg = parse_config({"campaigns": {"A": {"creator_name": "@a", "required_hashtags": ["a"]}},
                        "sources": {"s": {"mode": "vod", "platform": "local", "url": "/x.mp4", "campaigns": ["A"]}}})
    assert cfg.campaigns["A"].required_hashtags == ["#a"]  # '#' added
    assert cfg.sources["s"].clip.pre_seconds == 30
    assert cfg.sources["s"].clip.post_seconds == 35
    assert cfg.g.whisper.model == "medium"


def _err(raw) -> str:
    with pytest.raises(ConfigError) as e:
        parse_config(raw)
    return str(e.value)


def test_unknown_campaign_reference(raw):
    raw["sources"]["kaicenat"]["campaigns"] = ["Nope"]
    assert "unknown campaign 'Nope'" in _err(raw)


def test_typo_key_rejected(raw):
    raw["sources"]["kaicenat"]["spike"]["multipler"] = 2
    msg = _err(raw)
    assert "sources.kaicenat.spike.multipler" in msg


def test_placeholder_platform_requires_disabled(raw):
    raw["sources"]["lacy"].update(enabled=True, platform="FILL_ME", url="FILL_ME")
    assert "platform is FILL_ME" in _err(raw)


def test_stack_needs_crops(raw):
    raw["sources"]["kaicenat"]["layout"] = {"type": "stack"}
    assert "needs both facecam and gameplay" in _err(raw)


def test_campaign_length_range(raw):
    raw["campaigns"]["Kai"]["min_seconds"] = 100
    assert "min_seconds (100) > max_seconds (90)" in _err(raw)


def test_bad_regex(raw):
    raw["sources"]["kaicenat"]["spike"]["ignore_patterns"] = ["(unclosed"]
    assert "bad regex" in _err(raw)


def test_live_from_local_rejected(raw):
    raw["sources"]["kaicenat"]["platform"] = "local"
    assert "live mode supports" in _err(raw)


def test_notification_creds(raw):
    raw["global"]["notifications"]["backend"] = "telegram"
    assert "bot_token" in _err(raw)


def test_bad_time(raw):
    raw["global"]["notifications"]["daily_summary_time"] = "25:00"
    assert "HH:MM" in _err(raw)


def test_multiple_errors_listed(raw):
    r = copy.deepcopy(raw)
    r["global"]["workers"]["cpu_workers"] = 0
    r["sources"]["kaicenat"]["spike"]["multiplier"] = 0.5
    msg = _err(r)
    assert msg.count("\n  - ") >= 2


def test_bad_yaml(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("global: [unclosed")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_config(p)

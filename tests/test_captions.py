import pytest

from stream_clipper.captions import build_caption, caption_file_text, resolve_language, suggest_campaigns, validate
from stream_clipper.config import CampaignCfg

KAI = CampaignCfg(creator_name="@kaicenat", creator_aliases=["kai", "kai cenat"], required_hashtags=["#kaicenat", "ad"],
                  extra_hashtags=["#fyp"], notes="no logos", max_caption_chars=200)


def test_caption_contains_rules():
    text = build_caption("clip1", KAI, quote="NO WAY he did that!")
    assert validate(text, KAI) == []
    lines = text.splitlines()
    assert lines[0] and "“NO WAY he did that!”" in text
    assert "@kaicenat" in lines and lines[-1] == "#kaicenat #ad #fyp"


def test_caption_hook_override_and_deterministic():
    assert build_caption("x", KAI, hook="Kai went crazy").startswith("Kai went crazy")
    assert build_caption("a", KAI) == build_caption("a", KAI)
    assert "Kai Cenat" in build_caption("any", KAI, hook="{creator} lost it")


def test_caption_drops_quote_when_too_long():
    text = build_caption("c", KAI, quote="word " * 23)
    assert len(text) <= 200 and validate(text, KAI) == []


def test_validate_detects_problems():
    probs = validate("hello #kaicenatfan", KAI)
    assert "missing required hashtag #kaicenat" in probs  # prefix of another tag doesn't count
    assert any("creator mention" in p for p in probs)
    ph = CampaignCfg(creator_name="FILL_ME", required_hashtags=["#FILL_ME"])
    assert any("FILL_ME" in p for p in validate(build_caption("c", ph), ph))


def test_caption_file_flags_problems():
    out = caption_file_text("x", ["missing required hashtag #a"], KAI)
    assert out.startswith("!!! DO NOT POST AS IS")
    assert "Campaign notes: no logos" in out
    assert caption_file_text("x", [], KAI).startswith("x\n")


def test_suggest_campaigns():
    camps = {"Kai": KAI, "Jason": CampaignCfg(creator_name="@jasontheween", creator_aliases=["jason"])}
    assert suggest_campaigns(camps, ["Kai"], "yo JASON come here") == ["Jason"]
    assert suggest_campaigns(camps, ["Kai"], "jasonic stuff") == []


@pytest.mark.parametrize("source,detected,fallback,expected", [
    ("pl", "en", "en", "pl"),
    ("auto", "pl", "en", "pl"),
    ("auto", "de", "pl", "de"),
    ("auto", None, "sk", "sk"),
    ("PL_pl", "en", "en", "pl"),
    (None, "und", "auto", "en"),
])
def test_caption_language_follows_streamer_before_legacy_campaign_default(source, detected, fallback, expected):
    assert resolve_language(source, detected, fallback) == expected


def test_polish_caption_preserves_speech_mentions_and_hashtags():
    quote = "Nie wierzę że naprawdę to zrobiłeś!"
    text = build_caption("polish", KAI, quote=quote, language="pl")
    assert any(word in text.splitlines()[0] for word in ("Tego", "moment", "czekać"))
    assert quote in text and "@kaicenat" in text and "#kaicenat #ad #fyp" in text
    assert validate(text, KAI) == []
    assert build_caption("polish", KAI, quote=quote, language="pl") == text


def test_other_languages_never_fall_back_to_english_hook():
    text = build_caption("unsupported", KAI, quote="Dyma foment wych i'w gweld heddiw!", language="cy")
    assert text.startswith("🎬 Kai Cenat\n")
    assert "Dyma foment" in text


def test_custom_templates_are_preserved_for_non_english_stream():
    campaign = KAI.model_copy(update={"hook_templates": ["Własny tytuł: {creator}"]})
    assert build_caption("one", campaign, language="pl").startswith("Własny tytuł: Kai Cenat")


def test_caption_length_retry_keeps_selected_language():
    campaign = KAI.model_copy(update={"max_caption_chars": 75})
    text = build_caption("polish", campaign, quote="To jest bardzo długi cytat " * 3, language="pl")
    assert len(text) <= 75
    assert any(word in text.splitlines()[0] for word in ("Tego", "moment", "czekać"))

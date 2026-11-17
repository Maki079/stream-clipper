from stream_clipper.captions import build_caption, caption_file_text, suggest_campaigns, validate
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

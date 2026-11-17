import random

import pytest

from stream_clipper.config import CampaignCfg, ClipCfg, SpikeCfg
from stream_clipper.live.spikes import ChatMessage, Spike, SpikeDetector
from stream_clipper.live.window import ClipPlanner, clip_window, length_limits


def simulate(cfg: SpikeCfg, rate_fn, duration=900, seed=1, users=500, text_fn=None):
    """Poisson-ish chat: rate_fn(t) messages/second. Returns finished spikes."""
    rnd = random.Random(seed)
    det = SpikeDetector(cfg, started_at=0)
    spikes = []
    for sec in range(duration):
        n = rnd.poissonvariate(rate_fn(sec)) if hasattr(rnd, "poissonvariate") else _poisson(rnd, rate_fn(sec))
        for i in range(n):
            u = f"u{rnd.randrange(users)}"
            text = text_fn(sec, rnd) if text_fn else f"msg {rnd.randrange(10_000)}"
            det.add(ChatMessage(sec + i / max(n, 1), u, text))
        s = det.tick(sec + 1)
        if s:
            spikes.append(s)
    if (s := det.flush()):
        spikes.append(s)
    return spikes


def _poisson(rnd, lam):
    if lam <= 0:
        return 0
    if lam > 30:
        return max(0, int(rnd.gauss(lam, lam ** 0.5)))
    L, k, p = 2.718281828 ** -lam, 0, 1.0
    while True:
        p *= rnd.random()
        if p <= L:
            return k
        k += 1


def burst(base, peak, at, length=15):
    return lambda t: peak if at <= t < at + length else base


def test_flat_chat_no_spikes():
    assert simulate(SpikeCfg(), lambda t: 2.0) == []


def test_detects_single_burst():
    spikes = simulate(SpikeCfg(), burst(1.5, 12, 400))
    assert len(spikes) == 1
    s = spikes[0]
    assert 400 <= s.start <= 412
    assert s.end < 440
    assert s.peak_messages >= 60 and s.peak_z > 4
    assert s.strength > 0


def test_warmup_suppresses_early_burst():
    assert simulate(SpikeCfg(warmup_seconds=180), burst(1.5, 12, 60)) == []
    assert len(simulate(SpikeCfg(warmup_seconds=30), burst(1.5, 12, 60))) == 1


def test_absolute_minimum_for_quiet_chat():
    # 0.1 msg/s -> 1 msg/s is 10x but only ~10 messages/window < 15
    assert simulate(SpikeCfg(min_messages=15), burst(0.1, 1.0, 400)) == []
    assert len(simulate(SpikeCfg(min_messages=8), burst(0.1, 2.0, 400))) == 1


def test_big_chat_needs_big_minimum():
    # large chat: 40 msg/s baseline, wobble to 60 is noise; true moment 200/s
    rate = lambda t: 200 if 500 <= t < 515 else (60 if 300 <= t < 310 else 40)
    spikes = simulate(SpikeCfg(min_messages=1200), rate, users=50_000)
    assert len(spikes) == 1 and 500 <= spikes[0].start < 515


def test_baseline_excludes_spikes():
    # two identical bursts 120 s apart must both be detected (first must not inflate baseline)
    rate = lambda t: 12 if (400 <= t < 415 or 520 <= t < 535) else 1.5
    spikes = simulate(SpikeCfg(), rate)
    assert len(spikes) == 2


def test_ignore_patterns_and_bots():
    cfg = SpikeCfg(ignore_patterns=[r"(?i)hype train", r"^!\w+"])
    spam = lambda sec, rnd: "HYPE TRAIN LEVEL 3" if 400 <= sec < 415 else f"m{rnd.random()}"
    assert simulate(cfg, burst(1.5, 12, 400), text_fn=spam) == []
    det = SpikeDetector(SpikeCfg(), 0)
    assert not det.accepts(ChatMessage(0, "Nightbot", "hi"))


def test_repeated_lines_from_same_user_count_once():
    det = SpikeDetector(SpikeCfg(window_seconds=10), 0)
    for i in range(50):
        det.add(ChatMessage(1 + i * 0.1, "spammer", "LUL"))
    msgs, uniq, _ = det._measure(10)
    assert (msgs, uniq) == (1, 1)


def test_unique_metric():
    # one user flooding distinct messages: messages spike, unique chatters don't
    cfg = SpikeCfg(metric="unique", min_messages=5)
    det = SpikeDetector(cfg, 0)
    rnd = random.Random(0)
    for sec in range(600):
        for i in range(2):
            det.add(ChatMessage(sec + i * 0.5, f"u{rnd.randrange(300)}", f"x{rnd.random()}"))
        if 400 <= sec < 415:
            for i in range(30):
                det.add(ChatMessage(sec + i / 30, "flooder", f"y{rnd.random()}"))
        assert det.tick(sec + 1) is None


def test_long_spike_is_capped():
    spikes = simulate(SpikeCfg(), burst(1.5, 12, 300, length=400), duration=900)
    assert spikes and all(s.end - s.start <= 121 for s in spikes)


# ------------------------------------------------------------------ window

def spike(t, end=None, z=10):
    return Spike(t, end or t + 5, 100, 50, z, 5, 10)


def test_clip_window_default_65s():
    s, e = clip_window(1000, 1005, ClipCfg(), 0, 120)
    assert (s, e) == (1000 - 6 - 30, 1000 - 6 + 35)
    assert e - s == 65


def test_clip_window_campaign_minimum_extends_tail():
    s, e = clip_window(1000, 1005, ClipCfg(pre_seconds=20, post_seconds=20), 61, 120)
    assert s == 1000 - 6 - 20 and e - s == 61


def test_clip_window_long_spike_and_cap():
    s, e = clip_window(1000, 1100, ClipCfg(), 0, 120)
    assert e - s == 120
    s, e = clip_window(1000, 1040, ClipCfg(), 0, 120)
    assert e == 1040 - 6 + 10


def test_length_limits():
    camps = [CampaignCfg(creator_name="a", min_seconds=61, max_seconds=90),
             CampaignCfg(creator_name="b", min_seconds=70, max_seconds=180)]
    assert length_limits(ClipCfg(), camps) == (70, 90)
    assert length_limits(ClipCfg(), []) == (0, 120)
    weird = [CampaignCfg(creator_name="a", min_seconds=100, max_seconds=150)]
    assert length_limits(ClipCfg(max_seconds=90), weird) == (100, 100)


def test_planner_merges_close_spikes_up_to_max():
    p = ClipPlanner(ClipCfg(), 61, 120, cooldown=90)
    k, c = p.on_spike(spike(1000))
    assert k == "new" and c.end - c.start == 65
    k, c2 = p.on_spike(spike(1040))
    assert k == "extended" and c2 is c and c.end - c.start == 105
    k, _ = p.on_spike(spike(1080))  # would exceed 120 s and moment is past clip end -> cooldown
    assert k == "dropped"
    assert p.ready(c.end + 1) == []
    out = p.ready(c.end + 10)
    assert len(out) == 1 and len(out[0].spikes) == 2
    assert p.on_spike(spike(1100))[0] == "dropped"  # within cooldown of last clip
    k, c3 = p.on_spike(spike(1300))
    assert k == "new" and c3.start >= c.end


def test_planner_flush():
    p = ClipPlanner(ClipCfg(), 0, 120, cooldown=90)
    p.on_spike(spike(1000))
    assert len(p.flush()) == 1 and p.open is None


def test_threshold_formula_follows_average():
    det = SpikeDetector(SpikeCfg(min_messages=15, multiplier=2.0, z_threshold=3.0), 0)
    assert det.threshold(2, 1.4, 15) == 15                 # quiet chat: absolute floor wins
    assert det.threshold(150, 20, 15) == 300               # big chat: 2x average wins
    assert det.threshold(100, 40, 15) == 220               # noisy chat: avg + 3 std wins


def test_threshold_rises_with_growing_chat():
    # chat slowly grows 2 -> 8 msg/s (no spike), then a real burst at 30/s
    rate = lambda t: 30 if 700 <= t < 712 else 2 + 6 * min(t, 600) / 600
    spikes = simulate(SpikeCfg(), rate)
    assert len(spikes) == 1 and 700 <= spikes[0].start < 712


def test_reaction_chat_scores_higher_than_poll_spam():
    react = lambda sec, rnd: rnd.choice(["LOL", "LMAOOO", "???", "OOOO", "no way", "KEKW"]) if 400 <= sec < 415 else f"msg {rnd.random()}"
    poll = lambda sec, rnd: rnd.choice(["A", "B", "pick B", "team A"]) if 400 <= sec < 415 else f"msg {rnd.random()}"
    s_react = simulate(SpikeCfg(), burst(1.5, 12, 400), text_fn=react)[0]
    s_poll = simulate(SpikeCfg(), burst(1.5, 12, 400), text_fn=poll)[0]
    assert s_react.reaction_share > 0.8 and s_poll.reaction_share < 0.1
    assert s_react.strength > 2 * s_poll.strength


def test_default_ignore_patterns():
    det = SpikeDetector(SpikeCfg(), 0)
    for text in ["67", "5-13", "yes \\u034f", "RT", "!watchtime", "vape break", "BONG BREAK LOL"]:
        assert not det.accepts(ChatMessage(0, "u", text.encode().decode("unicode_escape"))), text
    for text in ["LOL", "W", "no way bro", "???", "OOOO"]:
        assert det.accepts(ChatMessage(0, "u", text)), text


def test_planner_quality_gate_and_hourly_budget():
    def sp(t, strength):
        s = Spike(t, t + 5, 100, 50, 10, 5, 10, [])
        s.reaction_share = 0.5
        object.__setattr__(s, "_force", strength)
        return s

    from stream_clipper.live import spikes as spk

    orig = spk.Spike.strength
    spk.Spike.strength = property(lambda self: getattr(self, "_force", 0))
    try:
        p = ClipPlanner(ClipCfg(), 61, 90, cooldown=90, min_strength=14, max_per_hour=2)
        assert p.on_spike(sp(1000, 10))[0] == "weak"
        assert p.on_spike(sp(1200, 15))[0] == "new"
        assert p.on_spike(sp(1400, 15))[0] == "new"
        assert p.on_spike(sp(1600, 15))[0] == "over_budget"   # 3rd in an hour
        assert p.on_spike(sp(1800, 30))[0] == "new"           # huge moment still passes
        assert p.on_spike(sp(1800 + 3700, 15))[0] == "new"    # budget refills after an hour
    finally:
        spk.Spike.strength = orig

import wave

import numpy as np
import pytest

from stream_clipper.config import SourceCfg
from stream_clipper.vod import audio, scoring, text
from stream_clipper.vod.analyze import select_windows

SR = 16000


def synth(path, seconds=600, loud=((200, 215), (420, 430)), seed=0):
    """Quiet speech-like noise with planted loud 'shouting' bursts."""
    rnd = np.random.default_rng(seed)
    t = np.arange(seconds * SR) / SR
    x = 0.02 * rnd.standard_normal(len(t)) * (0.6 + 0.4 * np.sin(2 * np.pi * 0.3 * t))
    for a, b in loud:
        sl = slice(a * SR, b * SR)
        x[sl] += 0.5 * np.sin(2 * np.pi * 220 * t[sl]) * (0.7 + 0.3 * np.sin(2 * np.pi * 5 * t[sl]))
    x = np.clip(x, -1, 1)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((x * 32767).astype(np.int16).tobytes())
    return path


def fake_segments(seconds=600, every=6.0):
    segs, t = [], 0.0
    while t + every <= seconds:
        words = [{"start": t + i, "end": t + i + 0.8, "word": f"w{i}"} for i in range(5)]
        segs.append({"start": t, "end": t + 5.0, "text": "just talking here about stuff", "words": words})
        t += every
    return segs


def test_audio_features_find_bursts(tmp_path):
    x, sr = audio.load_wav(synth(tmp_path / "a.wav"))
    f = audio.features(x, sr)
    e = f["audio_energy"]
    assert len(e) == 600
    assert e[205] > 10 and e[100] < 5
    assert f["shouting"][205] > 0 and f["shouting"][100] == 0
    assert f["laughter"][205] > f["laughter"][100]  # 5 Hz modulation looks laugh-like


def test_select_windows_hits_planted_moments(tmp_path):
    x, sr = audio.load_wav(synth(tmp_path / "a.wav"))
    segs = fake_segments()
    feats = audio.features(x, sr, text.speech_mask(segs, 600))
    feats.update(text.features(segs, 600, ["no way"]))
    src = SourceCfg(mode="vod", platform="local", url="x", vod={"top_n": 2})
    wins, score = select_windows(feats, segs, src, 61, 90)
    assert len(wins) == 2
    for w in wins:
        assert 61 <= w.length <= 90
    assert any(w.start <= 205 <= w.end for w in wins)
    assert any(w.start <= 425 <= w.end for w in wins)
    assert not (wins[0].start < wins[1].end and wins[1].start < wins[0].end)  # non-overlapping


def test_select_top_non_overlapping_and_n():
    score = np.zeros(1000)
    score[100:120] = 5
    score[500:520] = 4
    score[800:820] = 3
    wins = scoring.select_top(score, 61, 90, 10)
    assert len(wins) == 10
    for i, a in enumerate(wins):
        for b in wins[i + 1:]:
            assert a.end + 5 <= b.start or b.end + 5 <= a.start
    assert wins[0].start <= 100 and wins[0].end >= 120


def test_snap_to_sentences_keeps_limits():
    segs = [{"start": 98.0, "end": 103.0}, {"start": 103.5, "end": 160.2}, {"start": 161.0, "end": 170.0}]
    w = scoring.snap(scoring.Window(100, 165, 1), segs, 61, 90)
    assert (w.start, w.end) == (98.0, 160.2) or (w.start, w.end) == (103.5, 170.0) or (w.start, w.end) == (98.0, 170.0)
    assert 61 <= w.length <= 90
    tight = scoring.snap(scoring.Window(100, 161, 1), segs, 61, 61)
    assert tight.length == 61  # no boundary fits exactly -> unchanged


def test_text_features_and_quote():
    segs = [{"start": 0, "end": 3, "text": "NO WAY he did that!", "words": [
        {"start": 0, "end": 0.5, "word": "NO"}, {"start": 0.5, "end": 1, "word": "WAY"},
        {"start": 1, "end": 1.5, "word": "he"}, {"start": 1.5, "end": 2, "word": "did"}, {"start": 2, "end": 3, "word": "that!"}]},
        {"start": 5, "end": 8, "text": "ok so anyway we continue with the thing now",
         "words": [{"start": 5, "end": 6, "word": "ok"}]}]
    f = text.features(segs, 10, ["no way"])
    assert f["excitement"][0] > f["excitement"][5]
    assert text.quotable_line(segs, 0, 10, ["no way"]) == "NO WAY he did that!"


def test_robust_z_flat():
    assert np.all(scoring.robust_z(np.ones(10)) == 0)


@pytest.mark.ffmpeg
def test_scene_changes(tmp_path):
    import subprocess

    from stream_clipper.vod.scenes import density, scene_changes
    v = tmp_path / "v.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=red:s=320x240:d=3", "-f", "lavfi", "-i",
                    "color=blue:s=320x240:d=3", "-filter_complex", "[0][1]concat=n=2:v=1", "-c:v", "libx264",
                    "-preset", "ultrafast", str(v)], check=True)
    times = scene_changes(v)
    assert len(times) == 1 and 2.5 < times[0] < 3.5
    assert density(times, 6).sum() > 0

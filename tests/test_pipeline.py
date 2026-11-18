"""End-to-end dry runs with a stubbed Whisper (real ffmpeg)."""
import subprocess

import pytest
import yaml

from stream_clipper import dryrun
from stream_clipper.config import parse_config
from stream_clipper.media import whisper

from .conftest import EXAMPLE_CONFIG, ROOT


def fake_transcribe(ctx, audio, *, language="auto", model=None, task="transcribe"):
    from stream_clipper.media.render import probe
    dur = probe(audio)["duration"]
    segs, t = [], 0.5
    while t + 3 < dur:
        words = [{"start": t + i * 0.5, "end": t + i * 0.5 + 0.4, "word": w, "prob": 0.9}
                 for i, w in enumerate(["no", "way", "chat", "look", "at", "this!"])]
        segs.append({"start": t, "end": t + 3, "text": "no way chat look at this!", "words": words})
        t += 4
    return {"language": "en", "duration": dur, "segments": segs}


@pytest.fixture
def cfg(tmp_path):
    raw = yaml.safe_load(EXAMPLE_CONFIG.read_text())
    raw["global"]["output"]["preset"] = "ultrafast"
    raw["global"]["llm"]["enabled"] = False  # no Ollama in tests; LLM paths are stubbed explicitly
    raw["global"]["output"]["fonts_dir"] = str(ROOT / "assets" / "fonts")
    raw["global"]["models_dir"] = str(tmp_path / "models")
    for c in raw["campaigns"].values():
        c["required_hashtags"] = ["#test"]
        c["min_seconds"] = 20
        c["max_seconds"] = 40
        if c["creator_name"] == "FILL_ME":
            c["creator_name"] = "@core"
    raw["sources"]["kaicenat"]["clip"] = {"pre_seconds": 10, "post_seconds": 12, "chat_delay_seconds": 2}
    raw["sources"]["kaicenat"]["spike"] = {"min_messages": 15, "warmup_seconds": 30, "cooldown_seconds": 20}
    return parse_config(raw)


@pytest.fixture
def video(tmp_path):
    v = tmp_path / "in.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=150",
                    "-f", "lavfi", "-i", "sine=frequency=300:duration=150", "-shortest", "-c:v", "libx264",
                    "-preset", "ultrafast", "-c:a", "aac", str(v)], check=True)
    return v


@pytest.mark.ffmpeg
def test_dry_run_live(cfg, video, tmp_path, monkeypatch):
    monkeypatch.setattr(whisper, "transcribe", fake_transcribe)
    clips = dryrun.run(cfg, "kaicenat", video, outdir=tmp_path / "dry", simulate=[60, 120], threads=2)
    assert video.exists()  # the input must never be deleted
    assert len(clips) == 2
    for c in clips:
        assert c["status"] == "delivered", c["meta"].get("error")
        assert sorted(c["campaigns"]) == ["CORE", "Kai"]
        assert 20 <= c["meta"]["duration"] <= 40
    out = tmp_path / "dry" / "output"
    mp4s = sorted(out.rglob("*.mp4"))
    assert len(mp4s) == 4  # 2 clips x 2 campaigns
    txt = next(out.rglob("*.Kai.txt")).read_text()
    assert "@kaicenat" in txt and "#test" in txt and "DO NOT POST" not in txt
    side = next(out.rglob("*.json")).read_text()
    assert '"top_chat"' in side and '"spikes"' in side


@pytest.mark.ffmpeg
def test_dry_run_vod(cfg, video, tmp_path, monkeypatch):
    monkeypatch.setattr(whisper, "transcribe", fake_transcribe)
    raw = cfg.model_dump(by_alias=True)
    raw["sources"]["kai_youtube_vods"]["vod"]["top_n"] = 2
    cfg2 = parse_config(raw)
    clips = dryrun.run(cfg2, "kai_youtube_vods", video, outdir=tmp_path / "dryv", threads=2)
    assert len(clips) == 2
    assert all(c["status"] == "delivered" and c["mode"] == "vod" for c in clips)
    a, b = sorted(clips, key=lambda c: c["start_ts"])
    assert a["end_ts"] <= b["start_ts"]
    assert all(20 <= c["end_ts"] - c["start_ts"] <= 40 for c in clips)


@pytest.mark.ffmpeg
def test_cut_from_live_buffer(cfg, tmp_path):
    """Real .ts segments named like the recorder names them -> cut job -> raw.ts + offset."""
    import os
    import time as _t

    from stream_clipper.context import Context
    from stream_clipper.media.render import probe
    from stream_clipper.pipeline import handle_cut

    raw = cfg.model_dump(by_alias=True)
    raw["global"]["data_dir"] = str(tmp_path / "data")
    raw["global"]["buffer_dir"] = str(tmp_path / "buffer")
    ctx = Context.create(parse_config(raw))
    bdir = ctx.buffer_dir("kaicenat")
    t0 = int(_t.time()) - 200
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30:duration=60",
                    "-f", "lavfi", "-i", "sine=duration=60", "-shortest", "-c:v", "libx264", "-g", "30", "-preset",
                    "ultrafast", "-c:a", "aac", "-f", "segment", "-segment_time", "5", "-reset_timestamps", "1",
                    str(bdir / "tmp%03d.ts")], check=True)
    for i, f in enumerate(sorted(bdir.glob("tmp*.ts"))):
        dst = bdir / f"sess1_{t0 + i * 5}.ts"
        f.rename(dst)
        os.utime(dst, (t0 + i * 5 + 5, t0 + i * 5 + 5))
    ctx.store.add_clip("c1", "kaicenat", "live", t0 + 12, t0 + 42, 5.0, ["Kai"], {})
    handle_cut(ctx, {"clip_id": "c1"})
    clip = ctx.store.get_clip("c1")
    assert clip["status"] == "cut"
    assert clip["meta"]["offset"] == 2.0 and clip["meta"]["duration"] == 30.0
    assert probe(clip["meta"]["raw"])["duration"] >= 32
    assert ctx.queue.counts() == {"render": {"queued": 1}}


def test_deliver_then_telegram_job(cfg, tmp_path, monkeypatch):
    from stream_clipper import notify, pipeline
    from stream_clipper.context import Context

    raw = cfg.model_dump(by_alias=True)
    g = raw["global"]
    g["data_dir"], g["buffer_dir"] = str(tmp_path / "data"), str(tmp_path / "buffer")
    g["delivery"].update(backend="local", local_dir=str(tmp_path / "out"))
    g["notifications"] = {"backend": "telegram", "telegram": {"bot_token": "x"}}
    ctx = Context.create(parse_config(raw))
    work = ctx.work_dir("c1")
    (work / "c1.mp4").write_bytes(b"video")
    ctx.store.add_clip("c1", "kaicenat", "live", 0, 65, 5.0, ["Kai"],
                       {"video": str(work / "c1.mp4"), "duration": 65,
                        "captions": {"Kai": {"text": "hook @kaicenat #test", "problems": []}}}, status="rendered")
    pipeline.handle_deliver(ctx, {"clip_id": "c1"})
    assert work.exists()  # kept for the telegram job
    job = ctx.queue.claim(["telegram"], "t")
    sent = []
    monkeypatch.setattr(notify, "telegram_chat_id", lambda n, store=None: "42")
    monkeypatch.setattr(notify, "send_clip_telegram", lambda n, store, clip, video, chat: sent.append((clip["id"], video.name)))
    pipeline.handle_telegram(ctx, job.payload)
    assert sent == [("c1", "c1.mp4")]
    assert ctx.store.get_clip("c1")["notified"] == 1 and not work.exists()
    assert (tmp_path / "out" / "Kai").exists()


@pytest.mark.ffmpeg
def test_dry_run_live_with_llm(cfg, video, tmp_path, monkeypatch):
    """LLM skips the boring clip before rendering; the good one gets its cut moved + hook burned in."""
    from stream_clipper import pipeline
    from stream_clipper.vod import llm

    monkeypatch.setattr(whisper, "transcribe", fake_transcribe)
    seen = []

    def fake_judge(cfg_, segments, *, creator, moments, chat, total, min_len, max_len, default):
        seen.append((segments, moments, total, default, min_len, max_len))
        if len(seen) == 1:
            return llm.Verdict(3, default[0], default[1], "", "nothing happens", False)
        return llm.Verdict(8, default[0] + 3, default[1] + 3, "HE ACTUALLY DID IT", "big reaction", True)

    monkeypatch.setattr(pipeline.llm_mod, "judge_live", fake_judge)
    raw = cfg.model_dump(by_alias=True)
    raw["global"]["llm"]["enabled"] = True
    raw["global"]["llm"]["keep_strength"] = 1000  # simulated spikes are strong; let the LLM decide
    clips = dryrun.run(parse_config(raw), "kaicenat", video, outdir=tmp_path / "dry", simulate=[60, 120], threads=2)
    by = {c["status"]: c for c in clips}
    assert set(by) == {"skipped", "delivered"}, [c["meta"].get("error") for c in clips]
    assert by["skipped"]["meta"]["llm"]["reason"] == "nothing happens"
    good = by["delivered"]["meta"]
    assert good["hook"] == "HE ACTUALLY DID IT" and good["llm"]["trimmed"]
    assert abs(good["render_offset"] - (seen[1][3][0] + 3)) < 0.01
    assert all(seg["start"] >= 0 for seg in seen[1][0])  # transcript handed over in footage time
    ass = next((tmp_path / "dry").rglob("subs.ass"), None)
    if ass is not None:
        assert "HE ACTUALLY DID IT" in ass.read_text()
    assert len(sorted((tmp_path / "dry" / "output").rglob("*.mp4"))) == 2  # only the good clip, 2 campaigns


def test_llm_fit_window():
    from stream_clipper.vod.llm import fit_window

    kw = dict(total=110, min_len=61, max_len=90, moments=[50.0], default=(20.0, 85.0))
    assert fit_window(30, 95, **kw) == (30, 95, True)
    assert fit_window(30, 60, **kw) == (30, 91, True)            # too short: tail grows
    assert fit_window(0, 110, **kw)[1] - fit_window(0, 110, **kw)[0] == 90
    s, e, ok = fit_window(0, 110, **kw)
    assert ok and s <= 49 and e >= 52                             # chat moment kept inside
    assert fit_window(55, 116, **kw) == (40, 101, True)           # no setup before the moment: moved back
    assert fit_window(44, 105, **kw) == (40, 101, True)
    assert fit_window(60, 125, **(kw | {"total": 200})) == (40, 105, True)
    assert fit_window(0, 55, **(kw | {"total": 55})) == (20.0, 85.0, False)  # footage too short -> default
    assert fit_window("x", None, **kw) == (20.0, 85.0, False)
    assert fit_window(80, 30, **kw) == (20.0, 85.0, False)


def test_llm_judge_parses_and_fails_soft(monkeypatch):
    import json as _json

    from stream_clipper.config import LlmCfg
    from stream_clipper.vod import llm

    class R:
        def __init__(self, body):
            self.body = body

        def raise_for_status(self):
            pass

        def json(self):
            return {"response": self.body}

    sent = {}

    def post(url, timeout, json):
        sent.update(json)
        return R(_json.dumps({"event": True, "payoff": True, "standalone": True, "intensity": 3, "score": 7.5, "start": 12, "end": 80, "hook": '"#NO WAY HE WON"', "reason": "clutch"}))

    monkeypatch.setattr(llm.httpx, "post", post)
    segs = [{"start": 10.0, "end": 12.0, "text": "watch this"}]
    v = llm.judge_live(LlmCfg(enabled=True), segs, creator="@jynxzi", moments=[40.0],
                       chat=[{"text": "LMAO", "count": 30}], total=100, min_len=61, max_len=90, default=(20, 85))
    assert (v.score, v.start, v.end, v.hook, v.trimmed) == (8.1, 12, 80, "NO WAY HE WON", True)
    assert "[10.0-12.0] watch this" in sent["prompt"] and "LMAO (x30)" in sent["prompt"]

    def boom(*a, **k):
        raise llm.httpx.ConnectError("down")

    monkeypatch.setattr(llm.httpx, "post", boom)
    assert llm.judge_live(LlmCfg(enabled=True), segs, creator="", moments=[], chat=[], total=100,
                          min_len=61, max_len=90, default=(20, 85)) is None


def test_llm_combined_score():
    from stream_clipper.vod.llm import combined_score

    great = combined_score({"event": True, "payoff": True, "standalone": True, "intensity": 5, "score": 9})
    talk = combined_score({"event": False, "payoff": False, "standalone": True, "intensity": 2, "score": 7})
    meh = combined_score({"event": "true", "payoff": "no", "standalone": False, "intensity": 2, "score": 6})
    assert great >= 9 and talk <= 5 and talk < meh < 6


def test_llm_clean_hook():
    from stream_clipper.vod.llm import clean_hook

    assert clean_hook('"#DIES EVERY ROUND"') == "DIES EVERY ROUND"
    assert clean_hook("MISSION FAIL, CHAT REACTS") == "" and clean_hook("CREATOR'S SHOCK REACTION") == ""
    assert clean_hook(None) == "" and clean_hook("Streamer hasn't eaten all day!") == "Streamer hasn't eaten all day!"

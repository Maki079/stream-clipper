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

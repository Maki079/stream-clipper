import json
from types import SimpleNamespace

import numpy as np
import pytest

from stream_clipper import pipeline
from stream_clipper.config import LlmCfg, SourceCfg, parse_config
from stream_clipper.context import Context
from stream_clipper.vod import analyze, llm


def test_live_and_vod_prompts_request_streamer_language(monkeypatch):
    prompts = []

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"response": json.dumps({"score": 8, "event": True, "payoff": True,
                    "standalone": True, "intensity": 4, "start": 0, "end": 65,
                    "hook": "Nie uwierzysz w ten wynik"})}

    def post(url, **kwargs):
        prompts.append(kwargs["json"]["prompt"])
        return Response()

    monkeypatch.setattr(llm.httpx, "post", post)
    cfg = LlmCfg(enabled=True)
    assert llm.hook(cfg, "Co tu się właśnie wydarzyło?", "@streamer", language="pl") == "Nie uwierzysz w ten wynik"
    verdict = llm.judge_live(cfg, [{"start": 0, "end": 65, "text": "Co tu się wydarzyło?"}],
                             creator="@streamer", moments=[30], chat=[], total=65,
                             min_len=61, max_len=90, default=(0, 65), language="pl")
    assert verdict.hook == "Nie uwierzysz w ten wynik"
    assert len(prompts) == 2
    assert all("language code 'pl'" in prompt for prompt in prompts)
    assert all("English hook" not in prompt and "words, English" not in prompt for prompt in prompts)


def test_vod_ranking_passes_detected_language_to_ai(monkeypatch):
    languages = []
    monkeypatch.setattr(llm, "rate", lambda cfg, text, creator, *, language: languages.append(language) or (8, "Polski tytuł"))
    source = SourceCfg(mode="vod", platform="local", url="video.mp4", language="auto")
    source.vod.top_n = 1
    windows, _ = analyze.select_windows({"audio_energy": np.ones(70)},
        [{"start": 0, "end": 65, "text": "To jest przykładowy polski tekst!"}], source, 61, 65,
        LlmCfg(enabled=True), language="pl")
    assert languages and set(languages) == {"pl"}
    assert windows[0].hook == "Polski tytuł"


@pytest.mark.parametrize("source_language,detected", [("auto", "pl"), ("pl", "en")])
@pytest.mark.parametrize("ai_enabled,responding", [(False, False), (True, False), (True, True)])
def test_render_writes_polish_description_despite_legacy_english_campaign(
        tmp_path, monkeypatch, source_language, detected, ai_enabled, responding):
    cfg = parse_config({"global": {"data_dir": str(tmp_path / "data"), "llm": {"enabled": ai_enabled},
                                   "delivery": {"local_dir": str(tmp_path / "output")}},
        "campaigns": {"Campaign": {"creator_name": "@streamer", "required_hashtags": ["test"], "caption_language": "en"}},
        "sources": {"streamer": {"mode": "vod", "platform": "local", "url": "video.mp4", "language": source_language,
                                 "campaigns": ["Campaign"]}}})
    ctx = Context.create(cfg)
    try:
        raw = tmp_path / "raw.ts"
        raw.write_bytes(b"footage")
        ctx.store.add_clip("clip", "streamer", "vod", 0, 65, 8, ["Campaign"],
            {"raw": str(raw), "offset": 0, "duration": 65, "hook": "An old English title"}, status="cut")

        def audio(raw, output, *args):
            output.write_bytes(b"audio")
            return output

        monkeypatch.setattr(pipeline.media, "extract_audio", audio)
        monkeypatch.setattr(pipeline.whisper, "transcribe", lambda *args, **kw: {
            "language": detected, "segments": [{"start": 0, "end": 65,
            "text": "Nie wierzę że naprawdę to zrobiłeś!", "words": []}]})
        monkeypatch.setattr(pipeline.motion, "plan", lambda *args, **kwargs: ("", []))
        monkeypatch.setattr(pipeline.media, "render", lambda raw, out, **kw: out.write_bytes(b"video"))
        monkeypatch.setattr(pipeline.media, "probe", lambda *args: {"duration": 65})

        def unavailable(*args, **kwargs):
            if responding:
                assert "language code 'pl'" in kwargs["json"]["prompt"]
                return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {
                    "response": json.dumps({"score": 8, "hook": "Tego nie da się zapomnieć"})})
            raise llm.httpx.ConnectError("offline")

        monkeypatch.setattr(llm.httpx, "post", unavailable)
        pipeline.handle_render(ctx, {"clip_id": "clip"})
        clip = ctx.store.get_clip("clip")
        caption = clip["meta"]["captions"]["Campaign"]
        assert caption["language"] == "pl"
        assert "An old English title" not in caption["text"]
        assert "Nie wierzę że naprawdę to zrobiłeś!" in caption["text"]
        assert any(word in caption["text"].splitlines()[0] for word in ("Tego", "moment", "czekać"))
        assert "@streamer" in caption["text"] and "#test" in caption["text"]
        assert (ctx.work_dir("clip") / "clip.Campaign.txt").read_text(encoding="utf-8").startswith(caption["text"])
        pipeline.handle_deliver(ctx, {"clip_id": "clip"})
        delivered = next((tmp_path / "output").rglob("clip.Campaign.txt"))
        assert delivered.read_text(encoding="utf-8").startswith(caption["text"])
        sidecar = json.loads(next((tmp_path / "output").rglob("clip.json")).read_text(encoding="utf-8"))
        assert sidecar["captions"]["Campaign"]["language"] == "pl"
    finally:
        ctx.store.c.close()


def test_live_render_judge_uses_detected_language_over_campaign_default(monkeypatch):
    cfg = parse_config({"global": {"llm": {"enabled": True}},
        "campaigns": {"C": {"creator_name": "@streamer", "caption_language": "en"}},
        "sources": {"s": {"mode": "live", "platform": "twitch", "url": "streamer", "campaigns": ["C"]}}})

    def judge(cfg, segments, **kwargs):
        assert kwargs["language"] == "pl"
        return llm.Verdict(8, 0, 65, "Tego nikt się nie spodziewał", "moment", False)

    monkeypatch.setattr(llm, "judge_live", judge)
    clip = {"id": "one", "source": "s", "mode": "live", "campaigns": ["C"], "meta": {}}
    verdict = pipeline._judge(SimpleNamespace(cfg=cfg), clip, cfg.sources["s"],
                              {"language": "pl", "segments": []}, 0, 65, 65)
    assert verdict.hook == "Tego nikt się nie spodziewał"

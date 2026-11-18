import datetime as dt
from zoneinfo import ZoneInfo

from stream_clipper import delivery, notify
from stream_clipper.config import DeliveryCfg, NotificationsCfg


def test_local_delivery(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    files = {k: work / n for k, n in [("video", "c1.mp4"), ("caption", "c1.Kai.txt"), ("sidecar", "c1.json")]}
    for f in files.values():
        f.write_text("x")
    cfg = DeliveryCfg(backend="local", local_dir=str(tmp_path / "out"))
    loc, link, _ = delivery.deliver(cfg, files, "Kai", "2026-11-17")
    assert loc == str(tmp_path / "out" / "Kai" / "2026-11-17")
    assert sorted(p.name for p in (tmp_path / "out" / "Kai" / "2026-11-17").iterdir()) == ["c1.Kai.txt", "c1.json", "c1.mp4"]


def test_rclone_commands(monkeypatch, tmp_path):
    calls = []

    def fake(cfg, *args, timeout=0):
        calls.append(args)
        return '{"ID": "abc123"}' if args[0] == "lsjson" else ""

    monkeypatch.setattr(delivery, "_rclone", fake)
    cfg = DeliveryCfg(backend="rclone")
    files = {"video": tmp_path / "c.mp4", "caption": tmp_path / "c.Kai.txt", "sidecar": tmp_path / "c.json"}
    loc, link, rv = delivery.deliver(cfg, files, "Kai", "2026-11-17")
    assert loc == "gdrive:stream-clipper/Kai/2026-11-17"
    assert link == "https://drive.google.com/file/d/abc123/view"
    assert calls[0] == ("copyto", str(files["video"]), "gdrive:stream-clipper/Kai/2026-11-17/c.mp4")
    calls.clear()
    delivery.deliver(cfg, files, "CORE", "2026-11-17", first_remote_video=rv)
    assert calls[0] == ("copyto", rv, "gdrive:stream-clipper/CORE/2026-11-17/c.mp4")  # server-side copy


def _clip(i, score, status="delivered"):
    return {"id": f"c{i}", "score": score, "source": "kai", "status": status, "campaigns": ["Kai"],
            "meta": {"duration": 65, "deliveries": [{"campaign": "Kai", "location": "/out", "link": "L"}],
                     "captions": {"Kai": {"text": "hook\n@kai\n#x", "problems": ["missing"] if i == 1 else []}}}}


def test_messages():
    title, body = notify.new_clips_message([_clip(1, 3), _clip(2, 9), _clip(3, 5)], top_n=2)
    assert title == "3 new clip(s) ready"
    assert body.index("c2") < body.index("c3") and "c1" not in body and "1 more" in body
    t, b = notify.daily_summary([_clip(1, 3), _clip(2, 9, "failed")], failed_jobs=4)
    assert "Clips delivered: 1 / created: 2" in b and "Failed jobs (24h): 4" in b
    assert "needs fixing" in notify.format_clip(_clip(1, 3))


def test_summary_due():
    cfg = NotificationsCfg(daily_summary_time="20:00")
    tz = "Europe/Bratislava"
    at = lambda h, m: dt.datetime(2026, 11, 17, h, m, tzinfo=ZoneInfo(tz)).timestamp()
    assert notify.summary_due(cfg, tz, None, at(19, 59)) is None
    assert notify.summary_due(cfg, tz, None, at(20, 1)) == "2026-11-17"
    assert notify.summary_due(cfg, tz, "2026-11-17", at(23, 0)) is None


def test_send_none_backend():
    assert notify.send(NotificationsCfg(), "t", "b") is False


def test_telegram_chat_discovery(monkeypatch):
    from stream_clipper.config import TelegramCfg

    class KV(dict):
        def kv_get(self, k):
            return self.get(k)

        def kv_set(self, k, v):
            self[k] = v

    calls = []

    def fake(cfg, method, **kw):
        calls.append(method)
        return [{"message": {"chat": {"id": 42, "type": "private"}}}] if method == "getUpdates" else {}

    monkeypatch.setattr(notify, "tg_call", fake)
    cfg = NotificationsCfg(backend="telegram", telegram=TelegramCfg(bot_token="x"))
    store = KV()
    assert notify.telegram_chat_id(cfg, store) == "42" and store["telegram_chat_id"] == "42"
    assert notify.telegram_chat_id(cfg, store) == "42" and calls == ["getUpdates"]  # remembered
    assert notify.send(cfg, "t", "b", store) and calls[-1] == "sendMessage"


def test_clip_header_shows_chat_ratio():
    c = _clip(2, 7)
    c["meta"]["spikes"] = [{"peak_messages": 400, "baseline": 100}]
    assert "4.0x average" in notify.clip_header(c)


def test_clip_header_shows_llm_verdict():
    c = _clip(2, 7)
    c["meta"]["llm"] = {"score": 7.0, "reason": "rage <quit> after losing"}
    assert "🤖 7/10 · rage &lt;quit&gt; after losing" in notify.clip_header(c)

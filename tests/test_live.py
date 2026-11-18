import json
import os
import time

from stream_clipper.live import buffer
from stream_clipper.live.chat.kick import parse_event
from stream_clipper.live.chat.twitch import parse_line
from stream_clipper.live.chat.youtube import _continuation, extract_initial, parse_actions
from stream_clipper.live.resolve import kick_slug, twitch_channel, youtube_live_url
from stream_clipper.live.runner import top_chat
from stream_clipper.live.spikes import ChatMessage


def _seg(d, session, start, end):
    p = d / f"{session}_{int(start)}.ts"
    p.write_bytes(b"x")
    os.utime(p, (end, end))
    return p


def test_buffer_select_coverage_prune(tmp_path):
    for t in range(1000, 1100, 5):
        _seg(tmp_path, "aaaa", t, t + 5)
    segs = buffer.list_segments(tmp_path, now=2000)
    assert len(segs) == 20 and segs[0].start == 1000 and segs[0].end == 1005
    chosen = buffer.select(segs, 1012, 1040)
    assert chosen[0].start == 1010 and chosen[-1].start == 1035
    assert buffer.coverage(chosen, 1012, 1040)[0]
    ok, gaps = buffer.coverage(chosen, 1012, 1060)
    assert not ok and gaps[0][0] == 1040
    # prune: retention 30 s from now=1100 -> keep end >= 1070, but pinned from 1050 keeps more
    n = buffer.prune(tmp_path, 30, pinned_from=1050, now=1100)
    left = buffer.list_segments(tmp_path, now=2000)
    assert n > 0 and left[0].end >= 1040


def test_buffer_ignores_segment_being_written(tmp_path):
    _seg(tmp_path, "s", 1000, 1005)
    _seg(tmp_path, "s", 1005, time.time())
    assert len(buffer.list_segments(tmp_path)) == 1


def test_buffer_gap_from_reconnect(tmp_path):
    _seg(tmp_path, "a", 1000, 1005)
    _seg(tmp_path, "b", 1030, 1035)
    chosen = buffer.select(buffer.list_segments(tmp_path, now=2000), 1000, 1035)
    ok, gaps = buffer.coverage(chosen, 1000, 1035)
    assert not ok and gaps == [(1005, 1030)]


def test_twitch_irc_parse():
    line = "@badge-info=;display-name=CoolGuy;tmi-sent-ts=1700000000000 :coolguy!coolguy@coolguy.tmi.twitch.tv PRIVMSG #kaicenat :W KAI LMAO"
    m = parse_line(line, 5.0)
    assert m == ChatMessage(5.0, "CoolGuy", "W KAI LMAO")
    assert parse_line(":tmi.twitch.tv 001 justinfan1 :Welcome", 1) is None


def test_youtube_parsers():
    html = ('<script>var ytInitialData = {"contents":{"liveChatRenderer":{"continuations":[{"invalidationContinuationData":'
            '{"continuation":"ABC","timeoutMs":3000}}]}}};</script> "INNERTUBE_API_KEY":"KEY1" "INNERTUBE_CLIENT_VERSION":"2.1"')
    data, key, ver = extract_initial(html)
    assert (key, ver) == ("KEY1", "2.1")
    assert _continuation(data["contents"]["liveChatRenderer"]["continuations"]) == ("ABC", 3.0)
    actions = [{"addChatItemAction": {"item": {"liveChatTextMessageRenderer": {
        "message": {"runs": [{"text": "no way "}, {"emoji": {"shortcuts": [":fire:"]}}]},
        "authorName": {"simpleText": "@bob"}}}}}, {"markChatItemAsDeletedAction": {}}]
    assert parse_actions(actions, 1.0) == [ChatMessage(1.0, "@bob", "no way :fire:")]


def test_kick_parse():
    raw = json.dumps({"event": "App\\Events\\ChatMessageEvent",
                      "data": json.dumps({"content": "W", "sender": {"username": "x"}})})
    assert parse_event(raw, 2.0) == ChatMessage(2.0, "x", "W")
    assert parse_event(json.dumps({"event": "pusher:connection_established", "data": "{}"}), 1) is None


def test_resolvers():
    assert twitch_channel("https://www.twitch.tv/KaiCenat") == "kaicenat"
    assert kick_slug("https://kick.com/some-one") == "some-one"
    assert youtube_live_url("https://www.youtube.com/@x") == "https://www.youtube.com/@x/live"
    assert youtube_live_url("https://youtu.be/abcdefghijk") == "https://youtu.be/abcdefghijk"


def test_top_chat():
    ms = [ChatMessage(10, "a", "LMAO"), ChatMessage(11, "b", "lmao"), ChatMessage(12, "c", "W"), ChatMessage(99, "d", "x")]
    assert top_chat(ms, 0, 50) == [{"text": "LMAO", "count": 2}, {"text": "W", "count": 1}]


def _csv_session(d, session, lengths, arrival_lag=8.0, t0=1000.0, burst_every=3):
    """Segments with content lengths; files arrive in bursts (several share one mtime)."""
    rows, t = [], 0.0
    for i, ln in enumerate(lengths):
        name = f"{session}_{i:06d}.ts"
        (d / name).write_bytes(b"x")
        rows.append(f"{name},{t:.6f},{t + ln:.6f}")
        t += ln
        # burst: mtime rounded up to the next burst boundary
        k = (i // burst_every + 1) * burst_every
        burst_end = sum(lengths[:k])
        m = t0 + arrival_lag + min(burst_end, t + 6)
        os.utime(d / name, (m, m))
    (d / f"{session}.csv").write_text("\n".join(rows) + "\n")
    return t


def test_csv_buffer_is_gapless_despite_bursty_arrival(tmp_path):
    lengths = [6, 4] * 15
    total = _csv_session(tmp_path, "s1", lengths)
    (tmp_path / "s1_000030.ts").write_bytes(b"partial")  # being written, not in csv yet
    segs = buffer.list_segments(tmp_path, now=5000)
    assert len(segs) == 30
    # contiguous: every segment starts exactly where the previous one ended
    assert all(abs(b.start - a.end) < 1e-6 for a, b in zip(segs, segs[1:]))
    assert abs((segs[-1].end - segs[0].start) - total) < 1e-6
    ok, gaps = buffer.coverage(segs, segs[0].start, segs[-1].end)
    assert ok and gaps == []
    # anchor ~ arrival lag, frozen to disk once enough samples exist
    assert 1000 + 8 <= segs[0].start <= 1000 + 8 + 6
    assert (tmp_path / "s1.anchor").exists()


def test_csv_prune_cleans_bookkeeping(tmp_path):
    _csv_session(tmp_path, "old", [6, 4, 6, 4], t0=0)
    _csv_session(tmp_path, "new", [6, 4] * 6, t0=1000)
    buffer.prune(tmp_path, 60, None, now=1200, keep_session="new")
    assert not list(tmp_path.glob("old*"))
    assert (tmp_path / "new.csv").exists()


def test_recorder_restarts_when_timeline_drifts(tmp_path, monkeypatch):
    """Network stall: streamlink resumes at the live edge, the stream timeline lags wall-clock -> restart."""
    import asyncio

    from stream_clipper.live import recorder as rec_mod

    now = time.time()
    rows, t = [], 0.0
    for i in range(20):
        name = f"s1_{i:06d}.ts"
        (tmp_path / name).write_bytes(b"x")
        rows.append(f"{name},{t:.6f},{t + 5:.6f}")
        t += 5
        lag = 0 if i < 12 else 120  # after segment 12 the stream stalled for 2 minutes
        m = now - 100 + t + lag - 20 * 5 + 0.5
        os.utime(tmp_path / name, (m, m))
    (tmp_path / "s1.csv").write_text("\n".join(rows) + "\n")

    class Proc:
        returncode = None
        terminated = False

        def terminate(self):
            self.terminated = True

    r = rec_mod.Recorder("x", None, None, tmp_path)
    p = Proc()
    r._procs = [p]
    real_sleep = asyncio.sleep
    monkeypatch.setattr(rec_mod.asyncio, "sleep", lambda s: real_sleep(0))
    asyncio.run(asyncio.wait_for(r._watch_live("s1"), 5))
    assert p.terminated and r.live

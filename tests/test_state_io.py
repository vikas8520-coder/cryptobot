"""state_io is the file that exists because unclean writes corrupted state — so the
tests assert the guarantees it was written for: no temp file left behind on failure,
never a partially-written target, and a send that reports honest delivery booleans."""
import json
import os

import pytest

import state_io


class FakeResp:
    def __init__(self, status=200, body=None, json_ct=True):
        self.status_code = status
        self._body = {"ok": True} if body is None else body
        self.headers = {"content-type": "application/json"} if json_ct else {"content-type": "text/html"}

    def json(self):
        return self._body


@pytest.fixture
def posts(monkeypatch):
    """Capture every telegram POST instead of making one."""
    calls = []

    def fake_post(url, data=None, timeout=None, **kw):
        calls.append({"url": url, "data": data, "timeout": timeout})
        return calls_resp[0]

    calls_resp = [FakeResp()]
    monkeypatch.setattr(state_io.requests, "post", fake_post)
    return calls, calls_resp


# ------------------------------------------------------------------ save_json ----

def test_save_json_writes_and_removes_tmp(tmp_path):
    p = tmp_path / "x.json"
    assert state_io.save_json(str(p), {"a": 1}, indent=2) is True
    assert json.loads(p.read_text()) == {"a": 1}
    assert not (tmp_path / "x.json.tmp").exists()


def test_save_json_leaves_previous_content_intact_on_failure(tmp_path, capsys):
    """The whole point of tmp+replace: a failed write must not truncate the good file."""
    p = tmp_path / "x.json"
    p.write_text('{"good": true}')
    assert state_io.save_json(str(p), {1: object()}) is False   # not JSON-serializable
    assert json.loads(p.read_text()) == {"good": True}
    assert not (tmp_path / "x.json.tmp").exists()
    assert "save_json(x.json) failed" in capsys.readouterr().out


def test_save_json_returns_false_when_dir_missing(tmp_path):
    assert state_io.save_json(str(tmp_path / "nope" / "x.json"), {}) is False


# ------------------------------------------------------------------ save_text ----

def test_save_text_roundtrip(tmp_path):
    p = tmp_path / "a.txt"
    assert state_io.save_text(str(p), "hello\n") is True
    assert p.read_text() == "hello\n"


def test_save_text_failure_is_reported(tmp_path, capsys):
    assert state_io.save_text(str(tmp_path / "missing" / "a.txt"), "x") is False
    assert "save_text(a.txt) failed" in capsys.readouterr().out


# ----------------------------------------------------------------- append_feed ----

def test_append_feed_appends_one_json_line(tmp_path, monkeypatch):
    feed = tmp_path / "feed.jsonl"
    monkeypatch.setattr(state_io, "FEED", str(feed))
    state_io.append_feed("brake", "hello")
    state_io.append_feed("guardian", "world")
    lines = feed.read_text().splitlines()
    assert [json.loads(x)["source"] for x in lines] == ["brake", "guardian"]
    assert json.loads(lines[0])["text"] == "hello"
    assert json.loads(lines[0])["ts"].endswith("+00:00")


def test_append_feed_trims_when_over_max(tmp_path, monkeypatch):
    feed = tmp_path / "feed.jsonl"
    monkeypatch.setattr(state_io, "FEED", str(feed))
    monkeypatch.setattr(state_io, "FEED_MAX_BYTES", 200)
    monkeypatch.setattr(state_io, "FEED_KEEP_LINES", 3)
    for i in range(20):
        state_io.append_feed("brake", f"msg {i}")
    lines = feed.read_text().splitlines()
    assert len(lines) == 3
    assert json.loads(lines[-1])["text"] == "msg 19"        # newest survives


def test_append_feed_never_raises(monkeypatch, capsys):
    monkeypatch.setattr(state_io, "FEED", "/nonexistent-dir/feed.jsonl")
    state_io.append_feed("brake", "x")                       # must not propagate
    assert "append_feed failed" in capsys.readouterr().out


# --------------------------------------------------------------- verified_send ----

def test_verified_send_true_only_on_ok_true(posts):
    calls, _ = posts
    assert state_io.verified_send("http://api", "42", "hi") is True
    assert calls[0]["url"] == "http://api/sendMessage"
    assert calls[0]["data"] == {"chat_id": "42", "text": "hi"}


def test_verified_send_false_on_http_error(posts, capsys):
    calls, resp = posts
    resp[0] = FakeResp(status=429, body={"ok": False, "description": "Too Many Requests"})
    assert state_io.verified_send("http://api", "42", "hi") is False
    assert "http=429" in capsys.readouterr().out


def test_verified_send_false_when_ok_field_missing(posts):
    _, resp = posts
    resp[0] = FakeResp(status=200, body={})
    assert state_io.verified_send("http://api", "42", "hi") is False


def test_verified_send_false_on_non_json_body(posts):
    """A 200 with an HTML body (proxy/captive portal) is NOT a confirmed delivery."""
    _, resp = posts
    resp[0] = FakeResp(status=200, json_ct=False)
    assert state_io.verified_send("http://api", "42", "hi") is False


def test_verified_send_false_on_exception(monkeypatch, capsys):
    def boom(*a, **k):
        raise ConnectionError("offline")
    monkeypatch.setattr(state_io.requests, "post", boom)
    assert state_io.verified_send("http://api", "42", "hi") is False
    assert "ConnectionError" in capsys.readouterr().out


def test_verified_send_chunks_long_text(posts):
    calls, _ = posts
    text = "x" * (state_io.MAX_TG * 2 + 5)
    assert state_io.verified_send("http://api", "42", text) is True
    assert len(calls) == 3
    assert [len(c["data"]["text"]) for c in calls] == [state_io.MAX_TG, state_io.MAX_TG, 5]


def test_verified_send_empty_text_still_sends_one_chunk(posts):
    calls, _ = posts
    assert state_io.verified_send("http://api", "42", "") is True
    assert len(calls) == 1 and calls[0]["data"]["text"] == ""


def test_verified_send_stops_at_first_failed_chunk(posts):
    """No point burning rate limit on chunk 2 when chunk 1 never landed."""
    calls, resp = posts
    resp[0] = FakeResp(status=500, body={"ok": False})
    assert state_io.verified_send("http://api", "42", "y" * (state_io.MAX_TG + 10)) is False
    assert len(calls) == 1


def test_verified_send_feeds_only_delivered_alerts(tmp_path, monkeypatch, posts):
    feed = tmp_path / "feed.jsonl"
    monkeypatch.setattr(state_io, "FEED", str(feed))
    _, resp = posts

    state_io.verified_send("http://api", "42", "reply", feed_source=None)
    assert not feed.exists()                                  # command replies stay out

    state_io.verified_send("http://api", "42", "alert", feed_source="brake")
    assert json.loads(feed.read_text().splitlines()[0])["source"] == "brake"

    resp[0] = FakeResp(status=502, body={"ok": False})
    state_io.verified_send("http://api", "42", "lost", feed_source="brake")
    assert len(feed.read_text().splitlines()) == 1            # failed send not mirrored


def test_verified_send_passes_timeout_through(posts):
    calls, _ = posts
    state_io.verified_send("http://api", "42", "hi", timeout=3)
    assert calls[0]["timeout"] == 3


def test_feed_path_defaults_next_to_the_module():
    assert os.path.basename(state_io.FEED) == "activity_feed.jsonl"

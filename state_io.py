#!/usr/bin/env python3
"""
state_io.py — tiny shared helpers fixing the two failure classes the 2026-07-19
audit found everywhere:

  1. save_json / save_text: ATOMIC writes (temp file + os.replace, atomic on APFS).
     The old json.dump(obj, open(path, "w")) pattern truncates first — a kill
     mid-write (logout/shutdown SIGKILLs launchd agents) left corrupt JSON that
     every loader silently swallowed, un-tripping circuit breakers and
     re-baselining alert state.

  2. verified_send: a Telegram send that RETURNS whether delivery actually
     happened (HTTP 200 + {"ok": true}), splitting >4096-char messages into
     chunks. The old fire-and-forget senders swallowed every failure, so a
     dropped alert looked identical to "all clear".

  3. load_json (audit 2026-07-26): the mirror image of (1). Every module had its own
     `try: json.load(...) except: pass -> default` loader, so a truncated state file
     was INDISTINGUISHABLE from a first run: the guardian's breaker un-tripped, the
     brake re-baselined (losing queued flips), the memory wiped its track record.
     Missing file -> default (a real first run); unreadable file -> loud log, and
     StateCorrupt when the caller passes strict=True (state whose loss is not
     recoverable by re-deriving it next run).
"""
import json
import os
from datetime import datetime, timezone

import requests

MAX_TG = 4096
FEED = os.path.join(os.path.dirname(os.path.abspath(__file__)), "activity_feed.jsonl")
FEED_MAX_BYTES = 512 * 1024      # keep the feed bounded (dashboard reads the tail)
FEED_KEEP_LINES = 300


class StateCorrupt(Exception):
    """A state file exists but could not be parsed — never treat this as a first run."""


def load_json(path, default, strict=False):
    """Read JSON from path. A MISSING file returns default (genuine first run); an
    existing but unreadable one is always logged, and raises StateCorrupt if strict."""
    if not os.path.exists(path):
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        print(f"load_json({os.path.basename(path)}) UNREADABLE: {e}", flush=True)
        if strict:
            raise StateCorrupt(f"{path}: {e}") from e
        return default


def append_feed(source, text):
    """Append one delivered push-alert to the shared activity feed the dashboard mirrors.
    Every Telegram ALERT flows through verified_send, so tagging it here gives the desk
    automatic parity with Telegram — command replies (feed_source=None) are NOT logged."""
    try:
        entry = json.dumps({"ts": datetime.now(timezone.utc).isoformat(),
                            "source": source, "text": text})
        with open(FEED, "a") as f:
            f.write(entry + "\n")
        if os.path.getsize(FEED) > FEED_MAX_BYTES:          # trim to the last N lines
            lines = open(FEED).read().splitlines()[-FEED_KEEP_LINES:]
            save_text(FEED, "\n".join(lines) + "\n")
    except Exception as e:
        print(f"append_feed failed: {e}", flush=True)


def save_json(path, obj, indent=None):
    """Atomically write obj as JSON to path. Returns True on success."""
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(obj, f, indent=indent)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return True
    except Exception as e:
        print(f"save_json({os.path.basename(path)}) failed: {e}", flush=True)
        try:
            os.remove(tmp)
        except Exception:
            pass
        return False


def save_text(path, text):
    """Atomically write text to path. Returns True on success."""
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return True
    except Exception as e:
        print(f"save_text({os.path.basename(path)}) failed: {e}", flush=True)
        try:
            os.remove(tmp)
        except Exception:
            pass
        return False


def verified_send(api, chat_id, text, timeout=20, feed_source=None):
    """Send a Telegram message, chunked at 4096 chars. Returns True only if EVERY
    chunk got HTTP 200 + {"ok": true} back. api = 'https://api.telegram.org/bot<tok>'.
    If feed_source is set, a delivered message is also logged to the activity feed so
    the dashboard mirrors it (pass a source label for push ALERTS; leave None for
    command replies you don't want in the feed)."""
    chunks = [text[i:i + MAX_TG] for i in range(0, len(text), MAX_TG)] or [""]
    for chunk in chunks:
        try:
            r = requests.post(f"{api}/sendMessage",
                              data={"chat_id": chat_id, "text": chunk}, timeout=timeout)
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            if r.status_code != 200 or not body.get("ok"):
                print(f"telegram send NOT ok: http={r.status_code} "
                      f"desc={body.get('description', '?')}", flush=True)
                return False
        except Exception as e:
            print(f"telegram send error: {type(e).__name__}", flush=True)
            return False
    if feed_source:
        append_feed(feed_source, text)
    return True

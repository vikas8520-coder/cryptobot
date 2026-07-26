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

Plus the three boilerplate blocks every job used to re-type by hand (a copy in each
file meant a fix — e.g. a missing telegram.conf raising AttributeError at import —
had to be made 9 times):

  3. load_json:      exists -> try json.load -> except -> default.
  4. telegram_conf:  parse TG_TOKEN/TG_CHAT out of telegram.conf into (chat, api).
  5. acquire_lock:   flock a .<name>.lock so a manual run can't interleave with the
     scheduled one and clobber the pending-alert queue.
"""
import fcntl
import json
import os
import re
import sys
from datetime import datetime, timezone

import requests

MAX_TG = 4096
BASE = os.path.dirname(os.path.abspath(__file__))
TG_CONF = os.path.join(BASE, "telegram.conf")
FEED = os.path.join(os.path.dirname(os.path.abspath(__file__)), "activity_feed.jsonl")
FEED_MAX_BYTES = 512 * 1024      # keep the feed bounded (dashboard reads the tail)
FEED_KEEP_LINES = 300


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


def load_json(path, default=None):
    """Read JSON defensively: a missing file or corrupt/partial content yields default
    (never an exception) — every loader in the stack already wanted exactly this."""
    if os.path.exists(path):
        try:
            return json.load(open(path))
        except Exception:
            pass
    return default


def telegram_conf(path=TG_CONF):
    """(chat_id, api_base) parsed from telegram.conf. api_base = the /bot<token> prefix
    verified_send expects; the raw token is never returned so it can't get logged."""
    try:
        conf = open(path).read()
        tok = re.search(r'TG_TOKEN="([^"]+)"', conf).group(1)
        chat = str(re.search(r'TG_CHAT="([^"]+)"', conf).group(1))
    except Exception as e:
        raise RuntimeError(f"telegram.conf unreadable ({os.path.basename(path)}): {e}")
    return chat, f"https://api.telegram.org/bot{tok}"


def tg_token(api):
    """The bare token back out of an api base — only the redactor should need it."""
    return api.rsplit("/bot", 1)[-1]


def acquire_lock(name, log=print):
    """Serialize runs (manual + scheduled interleavings clobbered the pending queue).
    Returns the lock file handle — KEEP IT REFERENCED, a GC'd handle drops the flock —
    or exits 0 if another run already holds it."""
    fh = open(os.path.join(BASE, f".{name}.lock"), "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("another run already in progress — exiting quietly")
        sys.exit(0)
    return fh


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

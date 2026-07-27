"""Shared test setup.

Two constraints shape everything here:
  * NEVER touch the live Telegram token or a real exchange — every sender/fetcher is
    monkeypatched or replaced by a fake, and the suite runs fully offline.
  * NEVER write into the repo's real state files (brake_episodes.json, activity_feed.jsonl,
    *.log) — modules keep their paths in module-level constants, so tests repoint those
    constants at tmp_path instead.
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

# brake_alerts (and everything importing it) parses telegram.conf AT IMPORT TIME, i.e.
# during collection — before any fixture could run. On the live machine the real conf is
# there and this is a no-op; on a clean checkout / CI there is none (it is git-ignored),
# so drop a dummy with an obviously-fake token and delete it when the session ends. The
# token is never exercised: every send is monkeypatched.
_CONF = os.path.join(BASE, "telegram.conf")
_CONF_IS_OURS = not os.path.exists(_CONF)
if _CONF_IS_OURS:
    with open(_CONF, "w") as _f:
        _f.write('TG_TOKEN="0000000000:TEST-TOKEN-NOT-A-REAL-BOT"\nTG_CHAT="1"\n')


def pytest_sessionfinish(session, exitstatus):
    if _CONF_IS_OURS and os.path.exists(_CONF):
        os.remove(_CONF)

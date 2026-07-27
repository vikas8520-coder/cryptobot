# cryptobot — project context for Claude Code

Freqtrade multi-bot **paper-trading** system (all bots `dry_run: true` — no real
capital at risk). 5 bots (spot, futures, scalp, daytrade, brakedhold), each its own
config + strategy + SQLite DB + REST port, managed entirely by **launchd**
(`~/Library/LaunchAgents/com.vikas.*.plist`; retired jobs in `disabled_plists/`).
Ops layer: `dashboard.py` (:8090), `telegram_bot.py` (control + alerts),
`watchdog.py`, `portfolio_guard.py`. Python venv at `./.venv`.

## Coding conventions (match the existing voice)
- **Comments explain WHY and cite the incident** (often dated, e.g. "audit
  2026-07-19", "audit HIGH") — never restate what the code does.
- **Module docstring = design brief**: purpose + rule/priority order + the failure
  classes the file fixes.
- **Defensive I/O**: atomic writes via `state_io.save_json/save_text`
  (tmp + fsync + os.replace); wrap every external read in try/except → `None/{}/[]`,
  then `isinstance`-check at the call site (watch the bool-is-a-subclass-of-int trap);
  daemon main loops catch-all / print / sleep / continue — never die.
- **Honest failure reporting**: send Telegram via `state_io.verified_send` (returns a
  real delivery bool) — no fire-and-forget.
- Compact one-liner helpers; UPPER_CASE module config with inline unit comments;
  `# ---- SECTION ----` banners in long functions; plain stdlib + requests;
  4-space indent, ~100 col.
- **Single-writer / single-source-of-truth** for shared state files (e.g. only the
  guardian writes `guard_state.json`; `/reset` drops a one-shot flag file to avoid a
  read-modify-write race).

## Safety
- Never exercise the live Telegram token in tests — monkeypatch senders, simulate
  offline. `telegram.conf` / `local_secrets.py` / `config_*.secret.json` are secrets
  (git-ignored). Do not restart launchd jobs or deploy unless explicitly asked.

## Tests
`tests/` (pytest), fully offline — no exchange, no Telegram, no live state files.

```
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
.venv/bin/python -m pytest --cov --cov-report=term-missing
```

Covered: `state_io`, `logrotate`, `brake_memory`, `stats_lib`, `risk_lib`,
`brake_alerts`, `trendline_signal`. Conventions: repoint a module's path constants at
`tmp_path` (never write the real journal/state/logs), monkeypatch senders, replace the
exchange with a fake that replays canned OHLCV, and build synthetic price series whose
answer is known analytically. `tests/conftest.py` drops a dummy `telegram.conf` only if
one is missing (import-time parse) and removes it after. Modules that read
`local_secrets` / absolute `/Users/...` paths at import (guardian, notifier, digests)
are not importable in a test process — extract logic into a lib to make it testable.

## Delegating to Hermes (the other agent)
Hermes is a second AI agent on this machine with tools you don't have: **web
search/extract, browser automation, image generation, cron scheduling, and its own
skills + persistent memory**. When a task needs any of those (or you just want a
second model's take), delegate by shelling out:

```
hermes -z "<self-contained task — Hermes knows nothing about your conversation>"
```

`-z` runs Hermes headless (one-shot) and prints the result to stdout. Useful flags:
`-m MODEL --provider PROVIDER` (pin a model), `-t web` (restrict toolsets),
`--skills <name>` (preload a skill), `--yolo` (skip approval prompts for unattended
runs — use deliberately).

**Verify what Hermes returns — do not blind-trust it.** (In testing, a headless
Hermes on a lightweight model miscounted files as "0".) For anything where
correctness matters, tell Hermes to return a **verifiable artifact** (the actual
command output, file list, URL, or ID), then check it yourself before acting.

#!/usr/bin/env python3
"""
freqtrade_api.py — the one client for the bots' local REST APIs.

Every ops script talked to Freqtrade by hand-rolling the same three lines
(requests + HTTPBasicAuth("freqtrader", pw) + a bare try/except), once per file. Nine
copies meant nine chances to drift: the read timeout was 5s in the dashboard, 6s in
the notifier, 8s elsewhere, and only some copies swallowed the connection error a
restarting bot always throws.

Contract kept identical to the copies it replaces:
  get()  -> parsed JSON, or None when the bot is unreachable/garbling (callers
            already treat None as "offline")
  post() -> parsed JSON, or {"error": "..."} — a command must report why it failed,
            never look like it silently worked.
"""
import requests
from requests.auth import HTTPBasicAuth

USER = "freqtrader"                 # Freqtrade's api_server username in every config
GET_TIMEOUT = 8                     # reads are local-loopback; 8s is already generous
POST_TIMEOUT = 10                   # commands (forceexit/stop) can block on the exchange


def url(port, ep):
    return f"http://127.0.0.1:{port}/api/v1/{ep}"


def get(port, pw, ep, params=None, timeout=GET_TIMEOUT):
    """GET /api/v1/<ep>. None on any failure — a bot mid-restart is normal, not fatal."""
    try:
        return requests.get(url(port, ep), params=params,
                            auth=HTTPBasicAuth(USER, pw), timeout=timeout).json()
    except Exception:
        return None


def post(port, pw, ep, body=None, timeout=POST_TIMEOUT, log=False):
    """POST /api/v1/<ep>. {"error": ...} on failure so the caller can surface it."""
    try:
        return requests.post(url(port, ep), json=body or {},
                             auth=HTTPBasicAuth(USER, pw), timeout=timeout).json()
    except Exception as e:
        if log:
            print("post err", e, flush=True)
        return {"error": str(e)}

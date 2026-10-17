#!/usr/bin/env python3
"""Fail-fast YouTube OAuth token health check.

Runs as the FIRST step of every upload-touching workflow, BEFORE any
rendering/setup work. If the refresh token is dead the run exits here in
~20 seconds with a precise runbook, instead of burning 10-90 minutes of
rendering and THEN dying at the upload step.

Exit codes:
  0  token healthy (or transient OAuth endpoint error — pipeline continues
     and the upload step will surface any real problem)
  1  token dead / missing secrets (fail fast, with instructions)
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request

RUNBOOK = """
============================ YOUTUBE OAUTH RUNBOOK ============================
The channel's refresh token is EXPIRED or REVOKED. Google expires refresh
tokens 7 days after issuance when the OAuth consent screen is in TESTING
status. The video pipeline is healthy — only the upload credential died.

Permanent fix (do this ONCE):
  1. Google Cloud Console -> APIs & Services -> OAuth consent screen
     -> Audience (or "Publish App"): switch the app from Testing to
     In production. Production grants live ~6 months, not 7 days.
     (Unverified-app warning screens are fine for your own accounts.)

Then re-grant each dead channel (~2 min each):
  2. On a machine with python3:  python3 scripts/get_refresh_token.py
     Open the printed consent URL with the account that OWNS the channel,
     approve, then paste the redirected http://localhost:8765/?code=...
     URL back into the same terminal.
  3. Put the printed refresh token into the repo secret YT_REFRESH_TOKEN
     (GitHub repo -> Settings -> Secrets and variables -> Actions).
  4. Re-run the failed workflow (Actions -> re-run) or wait for the next
     scheduled slot.

The daily Health Guard workflow will open/reopen an alert issue in this
repo until posting resumes, then auto-close it.
===============================================================================
"""


def main() -> int:
    cid = os.environ.get("YT_CLIENT_ID", "").strip()
    csecret = os.environ.get("YT_CLIENT_SECRET", "").strip()
    rt = os.environ.get("YT_REFRESH_TOKEN", "").strip()

    if not cid or not csecret or not rt:
        print("AUTHCHECK FAIL: YT_CLIENT_ID / YT_CLIENT_SECRET / "
              "YT_REFRESH_TOKEN secrets are not all set on this repo.")
        print(RUNBOOK)
        return 1

    data = urllib.parse.urlencode({
        "client_id": cid,
        "client_secret": csecret,
        "refresh_token": rt,
        "grant_type": "refresh_token",
    }).encode()

    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            tok = json.loads(r.read().decode()).get("access_token", "")
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read().decode())
        except Exception:
            err = {}
        if err.get("error") == "invalid_grant":
            print("AUTHCHECK FAIL: refresh token EXPIRED or REVOKED "
                  "(Google error: invalid_grant).")
            print(RUNBOOK)
            return 1
        # Other HTTP errors (5xx, throttling): do not block the pipeline.
        print(f"AUTHCHECK WARN: token endpoint returned HTTP {e.code} "
              f"({err.get('error', '?')}) — continuing; the upload step "
              f"will retry and surface any real problem.")
        return 0
    except Exception as e:  # network / DNS / timeout
        print(f"AUTHCHECK WARN: could not reach the token endpoint ({e}) "
              f"— continuing; the upload step will retry.")
        return 0

    if not tok:
        print("AUTHCHECK WARN: empty access token — continuing.")
        return 0

    # Token alive — also prove WHICH channel it controls (catches
    # accidentally-crossed secrets between the three channels).
    req2 = urllib.request.Request(
        "https://www.googleapis.com/youtube/v3/channels?part=snippet&mine=true",
        headers={"Authorization": f"Bearer {tok}"})
    try:
        with urllib.request.urlopen(req2, timeout=30) as r:
            d = json.loads(r.read().decode())
        items = d.get("items", [])
        if items:
            print(f"AUTHCHECK OK: token live for channel "
                  f"'{items[0]['snippet']['title']}' ({items[0]['id']})")
        else:
            print("AUTHCHECK OK: token live (no channel items returned)")
    except Exception as e:
        print(f"AUTHCHECK OK: token live (channel lookup failed: {e})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

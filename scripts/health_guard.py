#!/usr/bin/env python3
"""Channel Health Guard — runs daily after the last posting slot.

Checks the channel's PUBLIC RSS feed (no credentials needed, so this works
even when every secret is dead). If the newest upload is older than
SILENT_HOURS, it diagnoses the YouTube OAuth token and opens (or updates)
an alert issue in this repo — the owner gets an email notification. When
posting resumes, the issue is auto-closed with a confirmation comment.

This script NEVER exits non-zero: a broken guard is worse than no guard.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

NOW = datetime.now(timezone.utc)

CHANNEL_ID = os.environ.get("CHANNEL_ID", "")
CHANNEL_HANDLE = os.environ.get("CHANNEL_HANDLE", "")
SILENT_HOURS = float(os.environ.get("SILENT_HOURS", "24"))
REPO = os.environ.get("GITHUB_REPOSITORY", "")
TOKEN = os.environ.get("GITHUB_TOKEN", "")
ALERT_TITLE = "CHANNEL ALERT — posting stopped (auto-opened by Health Guard)"

GH = "https://api.github.com"


def gh(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{GH}{path}", data=data, method=method,
        headers={"Authorization": f"Bearer {TOKEN}",
                 "Accept": "application/vnd.github+json",
                 "User-Agent": "health-guard",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode() if r.status != 204 else "{}"
            return r.status, json.loads(raw or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {}


def fetch_rss(channel_id: str) -> str | None:
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}",
                headers={"User-Agent": "Mozilla/5.0 (health-guard)"})
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:
            print(f"RSS attempt {attempt + 1}/3 failed: {e}")
            time.sleep(15)
    return None


def parse_latest(xml: str) -> tuple[str, str, datetime] | None:
    m = re.search(
        r"<entry>.*?<yt:videoId>([^<]+)</yt:videoId>.*?"
        r"<title>([^<]*)</title>.*?<published>([^<]+)</published>", xml, re.S)
    if not m:
        return None
    vid, title, pub = m.group(1), m.group(2), m.group(3)
    dt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
    return vid, title, dt


def diagnose_auth() -> str:
    cid = os.environ.get("YT_CLIENT_ID", "").strip()
    cs = os.environ.get("YT_CLIENT_SECRET", "").strip()
    rt = os.environ.get("YT_REFRESH_TOKEN", "").strip()
    if not cid or not cs or not rt:
        return ("YouTube OAuth secrets (YT_CLIENT_ID / YT_CLIENT_SECRET / "
                "YT_REFRESH_TOKEN) are NOT all set on this repo. Set them in "
                "Settings -> Secrets and variables -> Actions.")
    data = urllib.parse.urlencode({
        "client_id": cid, "client_secret": cs,
        "refresh_token": rt, "grant_type": "refresh_token"}).encode()
    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            json.loads(r.read().decode())
        return ("YouTube OAuth token is ALIVE — the outage is NOT the token. "
                "Check the latest run logs under the Actions tab for the "
                "real failure (e.g. Gemini storm, footage outage).")
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read().decode()).get("error", "?")
        except Exception:
            err = "?"
        if err == "invalid_grant":
            return ("DIAGNOSIS: YouTube OAuth refresh token is EXPIRED/REVOKED "
                    "(invalid_grant). This is the classic 7-day expiry when the "
                    "Google Cloud OAuth consent screen is in TESTING status.\n\n"
                    "PERMANENT FIX (once, 1 minute): Google Cloud Console -> "
                    "APIs & Services -> OAuth consent screen -> Audience -> "
                    "'Publish app' to production. Production tokens last "
                    "~6 months instead of 7 days.\n\n"
                    "THEN re-grant: run scripts/get_refresh_token.py in this "
                    "repo, open the consent URL with the account that owns the "
                    "channel, approve, paste back the redirected "
                    "http://localhost:8765/?code=... URL, and put the printed "
                    "refresh token into the YT_REFRESH_TOKEN secret. "
                    "The next scheduled slot resumes posting automatically.")
        return f"DIAGNOSIS: token endpoint returned HTTP {e.code} ({err})."
    except Exception as e:
        return f"DIAGNOSIS: token endpoint unreachable ({e}) — inconclusive."


def find_open_alert() -> dict | None:
    code, data = gh("GET", f"/repos/{REPO}/issues?state=open&per_page=100")
    # GET /issues returns a JSON ARRAY on success, a dict on error.
    if isinstance(data, list):
        issues = data
    elif isinstance(data, dict):
        issues = data.get("issues", [])
    else:
        issues = []
    for iss in issues:
        if isinstance(iss, dict) and iss.get("title") == ALERT_TITLE:
            return iss
    return None


def comment(issue_number: int, text: str):
    gh("POST", f"/repos/{REPO}/issues/{issue_number}/comments", {"body": text})


def close(issue_number: int):
    gh("PATCH", f"/repos/{REPO}/issues/{issue_number}", {"state": "closed"})


def ensure_label():
    gh("POST", f"/repos/{REPO}/labels",
       {"name": "channel-alert", "color": "d73a4a",
        "description": "Auto-opened when the channel stops posting"})


def main() -> int:
    print(f"Health Guard: @{CHANNEL_HANDLE} ({CHANNEL_ID}), "
          f"threshold {SILENT_HOURS}h, now {NOW.isoformat(timespec='seconds')}")

    xml = fetch_rss(CHANNEL_ID)
    if xml is None:
        print("Could not read the channel RSS feed after 3 attempts — "
              "NOT alerting (avoids false alarms). Will retry next run.")
        return 0
    latest = parse_latest(xml)
    if latest is None:
        print("RSS feed has no entries — new channel or parse failure; "
              "treating as silent.")
        vid, title, dt = "", "(no uploads found)", NOW
        silent = 1e9
    else:
        vid, title, dt = latest
        silent = (NOW - dt).total_seconds() / 3600

    print(f"Newest upload: '{title}' at {dt.isoformat(timespec='seconds')} "
          f"({silent:.1f}h ago)")

    if silent <= SILENT_HOURS:
        print("Channel is posting on schedule — OK.")
        open_alert = find_open_alert()
        if open_alert:
            comment(open_alert["number"],
                    f"✅ **Posting resumed.** Newest upload: “{title}” "
                    f"({dt.strftime('%Y-%m-%d %H:%M')} UTC, {silent:.1f}h ago). "
                    f"Auto-closing this alert.")
            close(open_alert["number"])
            print(f"Closed alert issue #{open_alert['number']}.")
        return 0

    # ----- channel is SILENT: diagnose + alert -----
    diag = diagnose_auth()
    body = (
        f"## 🚨 @{CHANNEL_HANDLE} has not posted for {silent:.0f} hours\n\n"
        f"| check | value |\n|---|---|\n"
        f"| newest upload | “{title}” |\n"
        f"| published | {dt.strftime('%Y-%m-%d %H:%M')} UTC |\n"
        f"| silent for | {silent:.1f} hours (threshold: {SILENT_HOURS:.0f}h) |\n"
        f"| link | https://youtu.be/{vid} |\n\n"
        f"### Diagnosis\n\n{diag}\n\n"
        f"### How to fix (if token expiry)\n"
        f"1. **Publish the OAuth app to production** (once, permanent fix): "
        f"Google Cloud Console → APIs & Services → OAuth consent screen → "
        f"Audience → *Publish app*.\n"
        f"2. **Re-grant the token**: run `python3 scripts/get_refresh_token.py` "
        f"in this repo, open the consent URL with the channel-owning account, "
        f"approve, paste back the redirected `http://localhost:8765/?code=...` "
        f"URL.\n"
        f"3. **Update the secret** `YT_REFRESH_TOKEN` (repo Settings → "
        f"Secrets and variables → Actions).\n"
        f"4. Wait for the next scheduled slot (or re-run the latest failed "
        f"workflow run).\n\n"
        f"This issue auto-closes when posting resumes. Check the Actions tab "
        f"for the exact run logs.\n\n"
        f"*Auto-opened by the Health Guard workflow at {NOW.isoformat(timespec='seconds')}*"
    )

    open_alert = find_open_alert()
    if open_alert:
        comment(open_alert["number"],
                f"Still silent: {silent:.1f}h since “{title}”. Re-checked at "
                f"{NOW.strftime('%Y-%m-%d %H:%M')} UTC.\n\n{diag}")
        print(f"Updated existing alert issue #{open_alert['number']}.")
    else:
        ensure_label()
        code, data = gh("POST", f"/repos/{REPO}/issues",
                        {"title": ALERT_TITLE, "body": body, "labels": ["channel-alert"]})
        if code in (200, 201):
            print(f"Opened alert issue #{data.get('number')}: {ALERT_TITLE}")
        else:
            # label may still not exist — retry without it
            code, data = gh("POST", f"/repos/{REPO}/issues",
                            {"title": ALERT_TITLE, "body": body})
            print(f"Opened alert issue without label: HTTP {code} "
                  f"#{data.get('number')}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        # The guard must never break the workflow — but if the guard ITSELF
        # crashes, open an issue about that so the breakage is visible.
        import traceback
        err = f"{type(e).__name__}: {e}\n```\n{traceback.format_exc()[:1500]}\n```"
        print("Health Guard crashed:", err)
        try:
            gh("POST", f"/repos/{REPO}/issues", {
                "title": "CHANNEL ALERT — Health Guard itself crashed (needs a code fix)",
                "body": f"The Health Guard script crashed and could NOT verify "
                        f"the channel this run. Please check the workflow log "
                        f"and fix scripts/health_guard.py.\n\n{err}"})
        except Exception:
            pass
        sys.exit(0)

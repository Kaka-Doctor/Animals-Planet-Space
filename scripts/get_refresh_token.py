"""Get the YouTube refresh token for the Animals Planet Space channel.

One-time OAuth consent for the account that OWNS the channel
https://www.youtube.com/@AnimalsPlanetSpace.

    python scripts/get_refresh_token.py

Then open the printed URL in a browser, sign in with the channel's Google
account, approve, and paste the FULL redirected URL (http://localhost:8765/?...)
back into the prompt. The script exchanges the code and prints the refresh
token — put it in the repo secret YT_REFRESH_TOKEN.
"""
from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

CLIENT_ID = ""      # filled from environment or argument
CLIENT_SECRET = ""
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPES = ("https://www.googleapis.com/auth/youtube.upload "
          "https://www.googleapis.com/auth/youtube.force-ssl")
REDIRECT_URI = "http://localhost:8765"  # NO trailing slash — must byte-match the OAuth client's registered Authorized Redirect URI


def _creds() -> tuple[str, str]:
    import os
    cid = os.environ.get("YT_CLIENT_ID", CLIENT_ID)
    sec = os.environ.get("YT_CLIENT_SECRET", CLIENT_SECRET)
    if not cid or not sec:
        print("Set YT_CLIENT_ID and YT_CLIENT_SECRET in the environment "
              "first (the same OAuth app the other channels use).")
        sys.exit(1)
    return cid, sec


def main() -> int:
    cid, sec = _creds()
    auth_url = ("https://accounts.google.com/o/oauth2/v2/auth?"
                + urllib.parse.urlencode({
                    "client_id": cid,
                    "redirect_uri": REDIRECT_URI,
                    "response_type": "code",
                    "scope": SCOPES,
                    "access_type": "offline",
                    "prompt": "consent select_account",
                }))
    print("\n1. Open this URL in a browser:\n")
    print(auth_url)
    print("\n2. Sign in with the Google account that OWNS the "
          "Animals Planet Space channel and approve.")
    redirected = input("\n3. Paste the FULL redirected URL "
                       "(http://localhost:8765/?...): ").strip()

    qs = urllib.parse.urlparse(redirected).query
    code = urllib.parse.parse_qs(qs).get("code", [""])[0]
    if not code:
        print("No authorization code found in that URL.")
        return 1

    data = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "client_id": cid,
        "client_secret": sec,
    }).encode()
    req = urllib.request.Request(
        TOKEN_URL, data=data, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            token = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        print(f"Exchange failed (HTTP {e.code}): {e.read().decode()[:300]}")
        return 1

    refresh = token.get("refresh_token")
    if not refresh:
        print("No refresh_token in the response — re-run and make sure to "
              "approve the offline-access prompt.")
        return 1
    print("\nREFRESH TOKEN (put this in the repo secret YT_REFRESH_TOKEN):\n")
    print(refresh)
    return 0


if __name__ == "__main__":
    sys.exit(main())

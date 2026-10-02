"""(Re-)apply the custom thumbnail to a video on the Animals Planet Space
channel.

Custom thumbnails require a verified channel (https://www.youtube.com/verify).
Until then YouTube returns 403 — re-run this script after verifying and the
epic thumbnail goes live.

Usage:
    python scripts/set_thumb.py                     # latest episode (state.json)
    VIDEO_ID=abc123 python scripts/set_thumb.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.config import Settings
from agent.youtube import set_thumbnail

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    settings = Settings.from_env()
    if not settings.has_youtube_credentials:
        print("YT_CLIENT_ID / YT_CLIENT_SECRET / YT_REFRESH_TOKEN not set")
        return 1

    video_id = os.environ.get("VIDEO_ID", "").strip()
    if not video_id:
        state = json.loads((ROOT / "state.json").read_text())
        last = state.get("last_video") or {}
        video_id = last.get("id", "")
    if not video_id:
        print("No VIDEO_ID given and state.json has no last_video")
        return 1

    thumb_env = os.environ.get("THUMB_FILE", "").strip()
    candidates = ([Path(thumb_env)] if thumb_env else [])
    if not candidates or not candidates[0].is_file():
        found = sorted((ROOT / "output").glob("animals_*_thumb.jpg"))
        if found:
            candidates = found

    from PIL import Image
    for src in candidates:
        if not src.is_file():
            continue
        jpg = src.with_suffix(".retrofit.jpg")
        Image.open(src).convert("RGB").save(jpg, "JPEG", quality=92)
        ok = set_thumbnail(video_id, jpg, settings)
        status = ("SET" if ok else
                  "NOT SET (403? verify the channel at "
                  "https://www.youtube.com/verify then re-run)")
        print(f"thumbnail {status} for {video_id} from {src}")
        return 0 if ok else 1

    print("No thumbnail file found — run the daily episode first.")
    return 1


if __name__ == "__main__":
    sys.exit(main())

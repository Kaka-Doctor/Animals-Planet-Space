"""Upload the finished video to YouTube using the Data API v3.

Uses only `requests` (no Google SDKs). Auth is a long-lived refresh token
obtained once with scripts/get_refresh_token.py — see README.md.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

import requests

log = logging.getLogger("youtube")

TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = ("https://www.googleapis.com/upload/youtube/v3/videos"
              "?uploadType=resumable&part=snippet,status")
THUMB_URL = "https://www.googleapis.com/upload/youtube/v3/thumbnails/set"
WATCH_URL = "https://youtu.be/{id}"
CHUNK = 8 * 1024 * 1024  # 8 MB resumable chunks


class YouTubeError(RuntimeError):
    pass


def _access_token(client_id: str, client_secret: str,
                  refresh_token: str) -> str:
    r = requests.post(TOKEN_URL, data={
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }, timeout=60)
    if r.status_code != 200:
        raise YouTubeError(
            f"Could not refresh access token (HTTP {r.status_code}): {r.text[:400]}. "
            "Your YT_REFRESH_TOKEN may be expired or revoked — regenerate it "
            "with scripts/get_refresh_token.py."
        )
    return r.json()["access_token"]


def upload_video(video_path: Path, title: str, description: str,
                 tags: list[str], settings) -> dict:
    token = _access_token(settings.yt_client_id,
                          settings.yt_client_secret,
                          settings.yt_refresh_token)
    size = video_path.stat().st_size

    metadata = {
        "snippet": {
            "title": title[:100],
            "description": description[:4900],
            "tags": [t for t in tags if t][:30],
            "categoryId": settings.yt_category_id,
        },
        "status": {
            "privacyStatus": settings.yt_privacy,
            "selfDeclaredMadeForKids": False,
            "embeddable": True,
        },
    }

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-Upload-Content-Type": "video/mp4",
        "X-Upload-Content-Length": str(size),
    }
    r = requests.post(UPLOAD_URL, headers=headers, json=metadata, timeout=120)
    if r.status_code != 200:
        raise YouTubeError(f"Upload init failed (HTTP {r.status_code}): {r.text[:500]}")
    upload_url = r.headers.get("Location")
    if not upload_url:
        raise YouTubeError("No resumable session URL returned")

    with open(video_path, "rb") as fh:
        offset = 0
        errors = 0
        max_retries = 10

        def _finalize() -> dict:
            put = requests.put(upload_url, headers={
                "Authorization": f"Bearer {token}",
                "Content-Range": f"bytes */{size}",
            }, timeout=120)
            if put.status_code in (200, 201):
                vid = put.json()["id"]
                log.info("Uploaded %s as %s", video_path.name, vid)
                return {"video_id": vid, "url": WATCH_URL.format(id=vid)}
            raise YouTubeError(
                f"Finalize failed (HTTP {put.status_code}): {put.text[:400]}")

        def _resync_offset() -> int:
            """Ask the session how many bytes it already has (after an error)."""
            try:
                probe = requests.put(upload_url, headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Range": f"bytes */{size}",
                }, timeout=120)
                if probe.status_code == 308:
                    rng = probe.headers.get("Range", "")
                    return int(rng.split("-")[-1]) + 1
            except (requests.RequestException, ValueError):
                pass
            return offset

        # NOTE: this must be a while-loop, NOT a for-loop over attempts:
        # successful 308 progress responses also consume loop iterations, so
        # a capped for-loop silently caps the video at attempts x CHUNK bytes
        # (a 71.7 MB episode died at chunk 5/9 with `for attempt in range(5)`).
        while True:
            if offset >= size:
                return _finalize()
            try:
                fh.seek(offset)
                data = fh.read(CHUNK)
                end = offset + len(data) - 1
                headers = {
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "video/mp4",
                    "Content-Range": f"bytes {offset}-{end}/{size}",
                    "Content-Length": str(len(data)),
                }
                put = requests.put(upload_url, headers=headers, data=data,
                                   timeout=600)
            except requests.RequestException as exc:
                errors += 1
                if errors > max_retries:
                    raise YouTubeError(
                        f"Upload failed after {max_retries} network errors: "
                        f"{exc}") from exc
                wait = min(5 * errors, 60)
                log.warning("Network error during upload (retry %d/%d, "
                            "waiting %ds): %s", errors, max_retries, wait, exc)
                time.sleep(wait)
                offset = _resync_offset()
                continue

            if put.status_code in (200, 201):
                vid = put.json()["id"]
                log.info("Uploaded %s as %s", video_path.name, vid)
                return {"video_id": vid, "url": WATCH_URL.format(id=vid)}
            if put.status_code == 308:
                rng = put.headers.get("Range", f"bytes=0-{offset}")
                try:
                    offset = int(rng.split("-")[-1]) + 1
                except ValueError:
                    offset = end + 1
                log.info("Resumable progress: %d/%d bytes", offset, size)
                continue
            if put.status_code >= 500:  # transient server error — retry
                errors += 1
                if errors > max_retries:
                    raise YouTubeError(
                        f"Upload failed after {max_retries} server errors "
                        f"(last HTTP {put.status_code}): {put.text[:400]}")
                wait = min(5 * errors, 60)
                log.warning("Server error HTTP %d during upload (retry %d/%d, "
                            "waiting %ds)", put.status_code, errors,
                            max_retries, wait)
                time.sleep(wait)
                offset = _resync_offset()
                continue
            raise YouTubeError(
                f"Chunk upload failed (HTTP {put.status_code}): {put.text[:400]}")


def set_thumbnail(video_id: str, thumbnail_path: Path, settings) -> bool:
    """Custom thumbnails can require a verified channel; failure is non-fatal."""
    try:
        token = _access_token(settings.yt_client_id,
                              settings.yt_client_secret,
                              settings.yt_refresh_token)
        data = thumbnail_path.read_bytes()
        r = requests.post(
            THUMB_URL, params={"videoId": video_id},
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "image/jpeg",
            }, data=data, timeout=120,
        )
        if r.status_code in (200, 201):
            log.info("Custom thumbnail set for %s", video_id)
            return True
        log.warning("Thumbnail not set (HTTP %d): %s — this is normal for "
                    "unverified channels; YouTube will use a frame instead.",
                    r.status_code, r.text[:200])
        return False
    except Exception as exc:  # noqa: BLE001
        log.warning("Thumbnail upload skipped: %s", exc)
        return False


def build_description(script_description: str,
                      animal: str,
                      angle_title: str,
                      section_chapters: list[tuple[str, float]],
                      settings,
                      footage_lines: list[str] | None = None) -> str:
    """Assemble the YouTube description: summary, chapters, footage
    attributions (CC-BY requirement), and the channel footer."""
    lines: list[str] = []
    if script_description:
        lines.append(script_description.strip())
        lines.append("")

    lines.append(f"ANIMAL: {animal} — today's angle: {angle_title}.")
    lines.append("All footage in this episode is REAL wildlife video, "
                 "reused legally under Creative Commons / public-domain "
                 "licenses from the sources listed below.")
    lines.append("")

    if section_chapters:
        lines.append("CHAPTERS")
        for name, t in section_chapters:
            mm, ss = divmod(int(t), 60)
            lines.append(f"{mm}:{ss:02d}  {name}")
        lines.append("")

    if footage_lines:
        lines.append("FOOTAGE SOURCES (used under their licenses — thank "
                     "you, filmmakers)")
        lines.extend(footage_lines)
        lines.append("")

    lines.append("TWO new wildlife documentaries every single day — "
                 "morning and evening. A different animal from a different "
                 "corner of the planet each time: Africa, Asia, the "
                 "Arctic, the oceans, the Americas, Australia and beyond.")
    lines.append("")
    lines.append(f"Subscribe: {settings.channel_url}")
    lines.append(f"{settings.channel_name} — the wild planet, explained fast.")
    lines.append("")
    lines.append("#wildlife #animals #nature #wildlifedocumentary "
                 "#AnimalsPlanetSpace")
    return "\n".join(lines)

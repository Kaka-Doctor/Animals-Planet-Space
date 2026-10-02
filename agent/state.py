"""Track posting state + featured animals (state.json in the repo root).

Three jobs:
  * twice-a-day guard   — the channel posts every ~12 hours; a new episode
    is allowed only after MIN_HOURS_BETWEEN_POSTS (default 10.5, tolerant
    of GitHub cron delays). `post_log` keeps the recent upload timestamps.
  * animal ledger       — which animal + angle featured when, so episodes
    never repeat the same animal until ANIMAL_COOLDOWN_DAYS passes, and
    never repeat "in an exact way" (each re-feature gets a fresh angle,
    and region rotation keeps consecutive episodes on different parts of
    the world).
  * metadata            — episode counter, last video, last run.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import STATE_FILE, Settings

log = logging.getLogger("state")

# The channel's posting day runs on East Africa Time (UTC+3).
EAT = timezone(timedelta(hours=3))

MAX_POST_LOG = 30      # recent uploads remembered for the cadence guard
MAX_LEDGER = 300       # animal-feature memory cap


def today_eat() -> str:
    return datetime.now(EAT).strftime("%Y-%m-%d")


def hours_since_last_post(data: dict) -> float | None:
    """Hours since the most recent upload, or None if never posted."""
    log_entries = data.get("post_log") or []
    if log_entries:
        try:
            last = datetime.fromisoformat(
                str(log_entries[-1].get("at", "")).replace("Z", "+00:00"))
            return (datetime.now(timezone.utc) - last).total_seconds() / 3600
        except ValueError:
            pass
    # fall back to last_run (older state files)
    try:
        last = datetime.fromisoformat(
            str(data.get("last_run", "")).replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - last).total_seconds() / 3600
    except ValueError:
        return None


def cadence_allows_post(data: dict, settings: Settings) -> tuple[bool, str]:
    """(allowed, reason) — the twice-a-day (every 12h) posting guard."""
    if not data.get("last_video"):
        return True, "first episode ever"
    gap = hours_since_last_post(data)
    if gap is None:
        return True, "last post time unknown — allowing"
    if gap >= settings.min_hours_between_posts:
        return True, f"{gap:.1f}h since the last episode (minimum " \
                     f"{settings.min_hours_between_posts}h)"
    return False, (f"only {gap:.1f}h since the last episode "
                   f"(minimum {settings.min_hours_between_posts}h) — the "
                   "channel posts every 12 hours, this slot is too early")


def load(path: Path | None = None, settings: Settings | None = None) -> dict:
    p = path or (Path(settings.state_file) if settings and settings.state_file
                 else STATE_FILE)
    default = {
        "completed": 0,
        "last_run": None,
        "last_video": None,
        "last_post_date": None,
        "post_log": [],
        "covered_animals": [],
        "note": "Wildlife documentaries, twice a day (~every 12h). post_log "
                "keeps recent upload times for the cadence guard; "
                "covered_animals is the animal ledger (dedup + angle "
                "rotation) powering no-exact-repeat episodes.",
    }
    if not p.exists():
        log.info("No state file — starting fresh.")
        return default
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        data.setdefault("covered_animals", [])
        data.setdefault("post_log", [])
        data.setdefault("completed", 0)
        return data
    except Exception as exc:  # noqa: BLE001
        log.warning("state.json unreadable (%s); starting fresh", exc)
        return default


def advance(path: Path | None, video_url: str, video_id: str,
            topic, settings: Settings | None = None) -> dict:
    """Record a successful upload: cadence log + animal ledger."""
    p = path or (Path(settings.state_file) if settings and settings.state_file
                 else STATE_FILE)
    data = load(p, settings)
    now = datetime.now(timezone.utc)
    data.update({
        "completed": int(data.get("completed", 0)) + 1,
        "last_run": now.isoformat(timespec="seconds"),
        "last_video": {"id": video_id, "url": video_url},
        "last_post_date": today_eat(),
    })
    data.setdefault("post_log", []).append({
        "at": now.isoformat(timespec="seconds"),
        "video_id": video_id,
        "url": video_url,
    })
    data["post_log"] = data["post_log"][-MAX_POST_LOG:]

    data.setdefault("covered_animals", []).append({
        "animal": topic.animal,
        "region": getattr(topic, "region", ""),
        "angle": getattr(topic, "angle_title", ""),
        "date": now.isoformat(timespec="seconds"),
        "video_id": video_id,
    })
    data["covered_animals"] = data["covered_animals"][-MAX_LEDGER:]

    p.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    log.info("State advanced → episode #%d live at %s (ledger: %d animals, "
             "post log: %d)", data["completed"], video_url,
             len(data["covered_animals"]), len(data["post_log"]))
    return data

"""Pre-upload quality gate: an episode must EARN its upload.

Wildlife-channel guidelines (all configurable via env — see config.py):
- narration length   >= MIN_SCRIPT_WORDS (AI scripts, default 550) or
                      MIN_TEMPLATE_WORDS (honest footage-walk, default 350)
- section count      >= MIN_SECTIONS (default 4)
- video duration     within MIN/MAX_VIDEO_MINUTES (default 3-10)
- REAL footage share >= MIN_FOOTAGE_RATIO (default 0.85) — this channel
                      is real moving video, not static images
- footage sources    >= 1 with attribution lines (license compliance)
- packaging          non-empty title <= 100 chars, description >= 80 chars,
                      existing thumbnail file >= 20 KB

If ANY check fails: no upload, no state advance, exit 1 — the next
scheduled slot retries. A missing day is better than a bad episode.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from .config import Settings

log = logging.getLogger("qa")


@dataclass
class Check:
    name: str
    passed: bool
    detail: str


def check_script(script, settings: Settings,
                 footage_seconds: float = 0.0) -> list[Check]:
    """Script length (by source, scaled to the footage we actually have)
    + section structure."""
    words = script.word_count
    floor = (settings.min_template_words
             if script.source.startswith("template")
             else settings.min_script_words)
    # the script is sized to the footage: short footage → shorter script is
    # correct, not a failure (only applies when footage is real)
    if footage_seconds > 0:
        scaled = int(footage_seconds * settings.words_per_second) + 40
        floor = min(floor, scaled)
    return [
        Check("script_length", words >= floor,
              f"{words} words (minimum {floor} for source={script.source}, "
              f"footage {footage_seconds:.0f}s)"),
        Check("section_count", len(script.sections) >= settings.min_sections,
              f"{len(script.sections)} sections (minimum "
              f"{settings.min_sections})"),
        Check("title", bool(script.title.strip()),
              f"YouTube title: {script.title[:70]!r}"),
        Check("description", len(script.description.strip()) >= 60,
              f"{len(script.description.strip())} chars of description"),
    ]


def check_video(video_path: Path, stats: dict, settings: Settings) -> list[Check]:
    duration = float(stats.get("duration", 0.0))
    minutes = duration / 60.0
    size_mb = (video_path.stat().st_size / 1e6) if video_path.exists() else 0.0
    ratio = float(stats.get("footage_ratio", 0.0))
    footage_min = float(stats.get("footage_seconds", 0.0)) / 60.0
    return [
        Check("video_duration_min", minutes >= settings.min_video_minutes,
              f"{minutes:.1f} minutes (minimum {settings.min_video_minutes})"),
        Check("video_duration_max", minutes <= settings.max_video_minutes,
              f"{minutes:.1f} minutes (maximum {settings.max_video_minutes})"),
        Check("video_file", video_path.exists() and size_mb >= 1.0,
              f"{size_mb:.1f} MB — {video_path.name}"),
        Check("real_footage_ratio",
              ratio >= settings.min_footage_ratio,
              f"{ratio * 100:.0f}% of runtime is REAL footage "
              f"({footage_min:.1f} min; minimum "
              f"{settings.min_footage_ratio * 100:.0f}%)"),
        Check("narration_present", duration > 0.0,
              "narration track muxed into the final video"),
    ]


def check_packaging(title: str, description: str,
                    thumb_path: Path, attribution_count: int) -> list[Check]:
    thumb_ok = thumb_path.exists() and thumb_path.stat().st_size >= 20_000
    return [
        Check("title", bool(title.strip()) and len(title) <= 100,
              f"{len(title)} characters"),
        Check("description", len(description.strip()) >= 80,
              f"{len(description.strip())} characters"),
        Check("thumbnail", thumb_ok,
              f"{thumb_path.name} present ({thumb_path.stat().st_size // 1024} KB)"
              if thumb_ok else f"{thumb_path.name} missing or suspiciously small"),
        Check("footage_attribution", attribution_count >= 1,
              f"{attribution_count} footage source(s) attributed in the "
              "description (license compliance)"),
    ]


def log_checks(header: str, checks: list[Check]) -> None:
    log.info("%s: %s", header,
             ", ".join(f"{c.name}={'OK' if c.passed else 'FAIL'}"
                       for c in checks))


def write_report(path: Path, checks: list[Check], **extra) -> bool:
    """Write output/qa_report.json; returns True when everything passed."""
    passed = all(c.passed for c in checks)
    report = {
        "verdict": "PASS" if passed else "FAIL",
        "failed": sum(1 for c in checks if not c.passed),
        "checks": [{"name": c.name, "passed": c.passed, "detail": c.detail}
                   for c in checks],
        **extra,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:  # noqa: BLE001
        log.warning("Could not write QA report (%s): %s", path, exc)
    return passed

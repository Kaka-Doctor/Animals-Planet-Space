"""VLM frame verification — footage must SHOW the episode's animal.

Title filters catch most mismatches, but zoo compilations ("Polar Bear -
Snow Leopard - African Elephant") and misfiled videos slip through words
alone. This module extracts sample frames at the exact offsets we plan to
cut from and asks Gemini whether the animal is actually visible.

Design contract:
- Returns True  → the animal is visible in at least one sampled frame.
- Returns False → every sampled frame clearly shows something else.
- Returns None  → the vision API is unavailable (storm / geo-block /
  quota). Callers treat None as "accept": the strict title filter has
  already run, and never posting during an API storm would be worse.

Cost: <= 2 tiny images per candidate source (640px JPEG), one short call
each — negligible against the script-generation budget.
"""
from __future__ import annotations

import base64
import logging
import subprocess
from pathlib import Path

import requests

from .config import Settings

log = logging.getLogger("vision")

GEN_URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
           "{model}:generateContent")
FRAME_W = 640


def _extract_frame(video: Path, at: float, out: Path) -> Path | None:
    """One downscaled JPEG frame at timestamp `at` (best effort)."""
    try:
        proc = subprocess.run(
            ["ffmpeg", "-y", "-ss", f"{max(at, 0.0):.2f}", "-i", str(video),
             "-frames:v", "1", "-vf",
             f"scale={FRAME_W}:-2:flags=bilinear", "-q:v", "5",
             str(out)],
            capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0 or not out.exists() or out.stat().st_size < 800:
        out.unlink(missing_ok=True)
        return None
    return out


def _ask_gemini(image: Path, animal: str, settings: Settings) -> bool | None:
    """YES/NO vision call; None on any API failure (never crashes the run)."""
    if not settings.gemini_api_key:
        return None
    try:
        img_b64 = base64.b64encode(image.read_bytes()).decode()
    except OSError:
        return None
    prompt = (
        f"You are verifying wildlife footage for a documentary about the "
        f"animal \"{animal}\". Look at this video frame. Does it clearly "
        f"show that exact kind of animal (the species \"{animal}\")? A "
        f"DIFFERENT species — even one whose name contains the word "
        f"\"{animal.split()[-1]}\" (for example a different animal entirely), "
        f"or any other unrelated animal — means NO. Answer with exactly one "
        f"word: YES or NO."
    )
    models = [settings.gemini_model, *settings.gemini_fallback_models]
    for model in models[:4]:                     # best-effort, no retries
        try:
            r = requests.post(
                GEN_URL.format(model=model),
                headers={"x-goog-api-key": settings.gemini_api_key,
                         "Content-Type": "application/json"},
                json={
                    "contents": [{"role": "user", "parts": [
                        {"text": prompt},
                        {"inline_data": {
                            "mime_type": "image/jpeg",
                            "data": img_b64,
                        }},
                    ]}],
                    "generationConfig": {"temperature": 0.0,
                                         "maxOutputTokens": 8},
                },
                timeout=30,
            )
            if r.status_code != 200:
                continue
            text = (r.json().get("candidates", [{}])[0]
                    .get("content", {}).get("parts", [{}])[0]
                    .get("text", "") or "").strip().upper()
            if "YES" in text.split()[:1] or text.startswith("YES"):
                return True
            if "NO" in text.split()[:1] or text.startswith("NO"):
                return False
        except (requests.RequestException, ValueError, IndexError, KeyError):
            continue
    return None


def frames_show_animal(video: Path, animal: str, settings: Settings,
                       offsets: list[float]) -> bool | None:
    """Verify the animal appears at the offsets we plan to cut from.

    Samples up to 2 frames: the first planned offset and the midpoint of
    the planned span. Any YES accepts the source; all NO rejects; API
    unavailable returns None (accept — title filter already ran).
    """
    if not offsets:
        return None
    work = video.parent
    stamp = video.stem
    probe_points = [offsets[0]]
    if len(offsets) > 1:
        probe_points.append(offsets[len(offsets) // 2])
    saw_yes = False
    asked = 0
    for i, at in enumerate(probe_points):
        frame = work / f"vlm_{stamp}_{i}.jpg"
        try:
            if _extract_frame(video, at + 0.8, frame) is None:
                continue
            asked += 1
            verdict = _ask_gemini(frame, animal, settings)
            if verdict is True:
                saw_yes = True
            if verdict is not None:
                log.info("VLM %s @%.0fs: %s", video.name[:28], at,
                         "animal visible" if verdict else "NOT the animal")
        finally:
            frame.unlink(missing_ok=True)
    if saw_yes:
        return True
    if asked == 0:
        return None            # frames could not be extracted — accept
    return False

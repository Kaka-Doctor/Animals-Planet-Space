"""Central configuration for the Animals Planet Space video agent.

Everything is driven by environment variables with sensible defaults so the
same code runs locally (for testing) and inside GitHub Actions.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FONTS_DIR = ROOT / "fonts"
OUTPUT_DIR = ROOT / "output"
WORK_DIR = OUTPUT_DIR / "work"
STATE_FILE = ROOT / "state.json"
FOOTAGE_DIR = OUTPUT_DIR / "footage"


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _get(key: str, default: str = "") -> str:
    """Env lookup where an unset OR empty value falls back to the default."""
    value = os.environ.get(key)
    return value if value not in (None, "") else default


@dataclass
class Settings:
    # --- Content -----------------------------------------------------------
    target_minutes: float = 5.0        # episode length the script aims for
    min_footage_minutes: float = 3.0   # below this we refuse to build
    max_footage_minutes: float = 8.0
    voice: str = "en-US-AndrewNeural"  # energetic male documentary voice
    tts_rate: str = "+6%"
    # words per second of narration (edge-tts at this rate averages ~2.3 w/s;
    # 2.1 is the conservative planning figure so narration fits the footage)
    words_per_second: float = 2.8
    animal_cooldown_days: int = 60     # same animal only re-featured after N days

    # --- Posting cadence ---------------------------------------------------
    min_hours_between_posts: float = 10.5  # 2 slots/day, 12h apart, tolerant
                                           # of GitHub cron delays

    # --- Real footage sourcing (all license-clean) -------------------------
    enable_yt_footage: bool = True     # YouTube Creative-Commons (CC-BY)
    enable_commons: bool = True        # Wikimedia Commons (CC/PD)
    enable_archive: bool = True        # Internet Archive (PD/CC)
    max_sources: int = 10              # distinct source videos per episode
    max_segments: int = 12             # extracted segments per episode
    segment_max_seconds: float = 70.0  # per-segment cap (variety)
    title_card_seconds: float = 3.2    # branded opener (branding only)
    outro_card_seconds: float = 2.6    # branded closer

    # --- Branding ----------------------------------------------------------
    channel_name: str = "Animals Planet Space"
    channel_handle: str = "@AnimalsPlanetSpace"
    channel_url: str = "https://www.youtube.com/@AnimalsPlanetSpace"

    # --- AI script writer ----------------------------------------------------
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.7-flash"
    gemini_fallback_models: list[str] = field(
        default_factory=lambda: ["gemini-3.8-flash", "gemini-flash-latest",
                                 "gemini-3.6-flash", "gemini-3.5-flash",
                                 "gemini-3.1-flash-lite"])
    # Storm resilience: total script-generation rounds + wait between them.
    script_retry_rounds: int = 3
    storm_wait_seconds: int = 900

    # --- YouTube -------------------------------------------------------------
    yt_client_id: str = ""
    yt_client_secret: str = ""
    yt_refresh_token: str = ""
    yt_privacy: str = "public"
    yt_category_id: str = "15"  # Pets & Animals

    # --- Quality gate (pre-upload checks) --------------------------------------
    min_script_words: int = 550      # AI-written documentary scripts
    min_template_words: int = 350    # footage-walk fallback (no invented facts)
    min_sections: int = 4
    min_video_minutes: float = 3.0
    max_video_minutes: float = 10.0
    min_footage_ratio: float = 0.85  # share of runtime that is REAL footage
    force_upload: bool = False

    # --- Video ---------------------------------------------------------------
    width: int = 1280
    height: int = 720
    fps: int = 30

    # --- Run overrides (also settable via CLI) -------------------------------
    dry_run: bool = False
    keep_state: bool = False
    state_file: str = ""

    extra_tags: list[str] = field(default_factory=list)

    @classmethod
    def from_env(cls) -> "Settings":
        get = _get
        return cls(
            target_minutes=float(get("TARGET_MINUTES", "5.0") or 5.0),
            min_footage_minutes=float(get("MIN_FOOTAGE_MINUTES", "3.0") or 3.0),
            max_footage_minutes=float(get("MAX_FOOTAGE_MINUTES", "8.0") or 8.0),
            voice=get("VOICE", "en-US-AndrewNeural"),
            tts_rate=get("TTS_RATE", "+6%"),
            words_per_second=float(get("WORDS_PER_SECOND", "2.8") or 2.8),
            animal_cooldown_days=int(get("ANIMAL_COOLDOWN_DAYS", "60") or 60),
            min_hours_between_posts=float(
                get("MIN_HOURS_BETWEEN_POSTS", "10.5") or 10.5),
            enable_yt_footage=_bool(get("ENABLE_YT_FOOTAGE"), True),
            enable_commons=_bool(get("ENABLE_COMMONS"), True),
            enable_archive=_bool(get("ENABLE_ARCHIVE"), True),
            max_sources=int(get("MAX_SOURCES", "10") or 10),
            max_segments=int(get("MAX_SEGMENTS", "12") or 12),
            segment_max_seconds=float(get("SEGMENT_MAX_SECONDS", "70") or 70),
            title_card_seconds=float(get("TITLE_CARD_SECONDS", "3.2") or 3.2),
            outro_card_seconds=float(get("OUTRO_CARD_SECONDS", "2.6") or 2.6),
            channel_name=get("CHANNEL_NAME", "Animals Planet Space"),
            channel_handle=get("CHANNEL_HANDLE", "@AnimalsPlanetSpace"),
            channel_url=get("CHANNEL_URL",
                            "https://www.youtube.com/@AnimalsPlanetSpace"),
            gemini_api_key=get("GEMINI_API_KEY", ""),
            gemini_model=get("GEMINI_MODEL", "gemini-3.7-flash"),
            gemini_fallback_models=[
                m.strip()
                for m in get("GEMINI_FALLBACK_MODELS",
                             "gemini-3.8-flash,gemini-flash-latest,"
                             "gemini-3.6-flash,gemini-3.5-flash,"
                             "gemini-3.1-flash-lite").split(",")
                if m.strip()
            ],
            script_retry_rounds=int(get("SCRIPT_RETRY_ROUNDS", "3") or 3),
            storm_wait_seconds=int(get("STORM_WAIT_SECONDS", "900") or 900),
            yt_client_id=get("YT_CLIENT_ID", ""),
            yt_client_secret=get("YT_CLIENT_SECRET", ""),
            yt_refresh_token=get("YT_REFRESH_TOKEN", ""),
            yt_privacy=get("YT_PRIVACY", "public"),
            yt_category_id=get("YT_CATEGORY_ID", "15"),
            min_script_words=int(get("MIN_SCRIPT_WORDS", "550") or 550),
            min_template_words=int(get("MIN_TEMPLATE_WORDS", "350") or 350),
            min_sections=int(get("MIN_SECTIONS", "4") or 4),
            min_video_minutes=float(get("MIN_VIDEO_MINUTES", "3.0") or 3.0),
            max_video_minutes=float(get("MAX_VIDEO_MINUTES", "10.0") or 10.0),
            min_footage_ratio=float(get("MIN_FOOTAGE_RATIO", "0.85") or 0.85),
            force_upload=_bool(get("FORCE_UPLOAD"), False),
            dry_run=_bool(get("DRY_RUN"), False),
            keep_state=_bool(get("KEEP_STATE"), False),
            state_file=get("STATE_FILE", ""),
            extra_tags=[
                t.strip()
                for t in get("EXTRA_TAGS", "").split(",")
                if t.strip()
            ],
        )

    @property
    def has_youtube_credentials(self) -> bool:
        return bool(
            self.yt_client_id and self.yt_client_secret and self.yt_refresh_token
        )

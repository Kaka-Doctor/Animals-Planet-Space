"""Channel-performance scoring — which animals earn the most views/likes.

Channel brief: "the most liked/watched videos are the ones that you
should be creating more than others." Before every episode we pull the
public statistics of the videos THIS channel has already uploaded
(YouTube Data API videos.list with the channel's own upload OAuth token,
part=statistics,snippet), aggregate them per animal, and hand the scores
to the topic picker as weighted-random weights.

Score per animal = mean over its episodes of (views + LIKE_WEIGHT ×
likes). Likes count far more than raw views — a like is an active vote.

This never blocks an episode: any failure returns empty scores and the
picker falls back to uniform rotation.
"""
from __future__ import annotations

import logging
from statistics import fmean

import requests

from .config import Settings

log = logging.getLogger("popularity")

YT_VIDEOS = "https://www.googleapis.com/youtube/v3/videos"
LIKE_WEIGHT = 25          # one like is worth 25 views
TIMEOUT = 30
BATCH = 50                # videos.list accepts up to 50 ids per call


def _statistics(video_ids: list[str],
                settings: Settings) -> dict[str, dict]:
    """{video_id: {"views": int, "likes": int, "title": str}} for our
    uploads. Best effort — HTTP/auth failures return {}."""
    if not video_ids or not settings.has_youtube_credentials:
        return {}
    try:
        from .youtube import _access_token
        token = _access_token(settings.yt_client_id,
                              settings.yt_client_secret,
                              settings.yt_refresh_token)
    except Exception as exc:  # noqa: BLE001
        log.info("popularity: auth unavailable (%s)", exc)
        return {}

    out: dict[str, dict] = {}
    for i in range(0, len(video_ids), BATCH):
        chunk = video_ids[i:i + BATCH]
        try:
            r = requests.get(YT_VIDEOS, params={
                "part": "statistics,snippet",
                "id": ",".join(chunk),
            }, headers={"Authorization": f"Bearer {token}"},
                timeout=TIMEOUT)
            if r.status_code != 200:
                log.info("popularity: videos.list HTTP %d: %s",
                         r.status_code, r.text[:160])
                continue
            for item in r.json().get("items", []):
                st = item.get("statistics", {})
                out[item.get("id", "")] = {
                    "views": int(st.get("viewCount", 0) or 0),
                    "likes": int(st.get("likeCount", 0) or 0),
                    "title": (item.get("snippet", {})
                              .get("title", "")),
                }
        except (requests.RequestException, ValueError) as exc:
            log.info("popularity: videos.list error: %s", exc)
    return out


def channel_performance(state: dict, settings: Settings
                        ) -> tuple[dict[str, float], dict[str, list[str]]]:
    """(scores, past_titles) from the ledger of uploaded episodes.

    scores:    {animal_lower: mean(views + LIKE_WEIGHT×likes) per episode}
    past_titles: {animal_lower: [up to 4 recent episode titles]} — fed to
    the script prompt so repeat episodes get distinctly new titles.
    """
    entries = [e for e in (state.get("covered_animals") or [])
               if e.get("video_id") and e.get("animal")]
    if not entries:
        return {}, {}

    stats = _statistics([e["video_id"] for e in entries], settings)
    if not stats:
        return {}, {}

    per_animal: dict[str, list[float]] = {}
    titles: dict[str, list[str]] = {}
    for e in entries:
        key = str(e["animal"]).strip().lower()
        st = stats.get(e["video_id"])
        if st is None:
            continue
        per_animal.setdefault(key, []).append(
            st["views"] + LIKE_WEIGHT * st["likes"])
        t = st.get("title", "")
        if t:
            titles.setdefault(key, []).append(t)

    scores = {k: fmean(v) for k, v in per_animal.items() if v}
    titles = {k: v[-4:] for k, v in titles.items()}
    if scores:
        top = ", ".join(f"{k} {v:,.0f}"
                        for k, v in sorted(scores.items(),
                                           key=lambda kv: -kv[1])[:5])
        log.info("CHANNEL POPULARITY (views+%d×likes, per episode): %s",
                 LIKE_WEIGHT, top)
    return scores, titles

"""Real animal footage from license-clean sources — the MAIN visual track.

The episode is built from real, moving wildlife video (never static image
slides). Three sources, all legal for reuse:

  1. YouTube Creative-Commons videos — search.list with
     license=creativeCommons, ORDERED BY VIEW COUNT ("many views" per the
     channel brief), downloaded with yt-dlp at <=720p. Reused under CC-BY
     with attribution (the license YouTube's Creative Commons option grants).
  2. Wikimedia Commons videos (CC0 / CC-BY / CC-BY-SA / public domain) —
     a huge trove of real wildlife footage.
  3. Internet Archive movies (public domain / Creative Commons) — classic
     wildlife films, rich source of long footage.

From each source video we extract up to a few SEGMENTS at varied offsets
(intros skipped), each capped at SEGMENT_MAX_SECONDS for visual variety.
Every source actually used is attributed in the YouTube description —
that satisfies the CC-BY attribution requirement.

Failures at any level are skipped gracefully; we keep collecting until the
footage timeline reaches the target duration (or the sources run dry).
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import requests

from .animals import EpisodeTopic
from .config import Settings
from .video import probe_duration

log = logging.getLogger("footage")

# Wikimedia (and friends) require a descriptive UA with contact info —
# generic browser UAs get 403 on the API. This one is verified working.
UA = ("AnimalsPlanetSpace-video-agent/1.0 "
      "(https://github.com/Kaka-Doctor/Animals-Planet-Space; "
      "automated wildlife channel; contact: actions@users.noreply.github.com) "
      "requests")
TIMEOUT = 30
MAX_DOWNLOAD_MB = 250          # allow longer wildlife films
MIN_SOURCE_SECONDS = 5.0       # shorter sources are useless
YT_SEARCH = "https://www.googleapis.com/youtube/v3/search"
YT_VIDEOS = "https://www.googleapis.com/youtube/v3/videos"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
ARCHIVE_SEARCH = "https://archive.org/advancedsearch.php"
ARCHIVE_META = "https://archive.org/metadata/{id}"


def _animal_words(animal: str) -> set[str]:
    """Distinctive words of the animal name (for relevance checks)."""
    stop = {"the", "of", "a", "and", "master", "cousins", "dodo", "s"}
    return {w for w in re.findall(r"[a-z]{3,}", animal.lower())
            if w not in stop}


@dataclass
class FootageSegment:
    """One extracted piece of a source video, placed on the timeline."""
    path: Path
    start: float          # offset inside the source
    seconds: float        # extraction length
    source_index: int     # which FootageSource this came from


@dataclass
class FootageSource:
    path: Path
    provider: str         # "youtube" | "commons" | "archive"
    title: str
    url: str              # human-facing source page (for attribution)
    license: str
    channel: str = ""     # uploader / author
    duration: float = 0.0
    views: int = 0
    segments: list[FootageSegment] = field(default_factory=list)

    def attribution_line(self) -> str:
        who = f" by {self.channel}" if self.channel else ""
        segs = ", ".join(f"{s.start:.0f}s+{s.seconds:.0f}s"
                         for s in self.segments)
        return (f"• “{self.title[:70]}”{who} — {self.url} "
                f"({self.license}); segments used: {segs}. Reused under its "
                f"license with attribution.")

    @property
    def used_seconds(self) -> float:
        return sum(s.seconds for s in self.segments)


def _segment_offsets(total: float, seg_len: float,
                     max_segments: int) -> list[float]:
    """Offsets across a source where a FULL segment fits: skip the intro
    (first 5%), keep a safety margin at the end. The count is how many
    non-overlapping seg_len blocks fit in the usable span."""
    lo = total * 0.05
    usable_end = total * 0.97 - 0.3
    span = max(usable_end - lo, 0.0)
    n = max(1, min(max_segments, int(span // max(seg_len, 1.0)) or 1))
    if n == 1:
        return [lo]
    step = (span - seg_len) / (n - 1)
    return [lo + i * step for i in range(n)]


# ---------------------------------------------------------------------------
# YouTube Creative Commons (ordered by view count)
# ---------------------------------------------------------------------------

def _yt_search(query: str, settings: Settings,
               duration: str = "medium") -> list[dict]:
    """CC-licensed YouTube videos for a query, relevance-ranked."""
    if not settings.has_youtube_credentials:
        return []
    try:
        from .youtube import _access_token
        token = _access_token(settings.yt_client_id,
                              settings.yt_client_secret,
                              settings.yt_refresh_token)
    except Exception as exc:  # noqa: BLE001
        log.info("YouTube search auth failed: %s", exc)
        return []
    try:
        r = requests.get(YT_SEARCH, params={
            "part": "snippet", "q": query, "type": "video",
            "license": "creativeCommons", "maxResults": 8,
            "videoDuration": duration,      # short<4m, medium 4-20m, long>20m
            "videoEmbeddable": "true",
            "relevanceLanguage": "en", "safeSearch": "strict",
            "order": "viewCount",           # "many views" per the brief
        }, headers={"Authorization": f"Bearer {token}"}, timeout=TIMEOUT)
        if r.status_code != 200:
            log.info("YouTube search HTTP %d: %s", r.status_code,
                     r.text[:200])
            return []
        return r.json().get("items", [])
    except requests.RequestException as exc:
        log.info("YouTube search error: %s", exc)
        return []


def _yt_views(video_ids: list[str], settings: Settings) -> dict[str, int]:
    """View counts for candidate videos (one videos.list call)."""
    if not video_ids or not settings.has_youtube_credentials:
        return {}
    try:
        from .youtube import _access_token
        token = _access_token(settings.yt_client_id,
                              settings.yt_client_secret,
                              settings.yt_refresh_token)
        r = requests.get(YT_VIDEOS, params={
            "part": "statistics", "id": ",".join(video_ids[:50]),
        }, headers={"Authorization": f"Bearer {token}"}, timeout=TIMEOUT)
        if r.status_code != 200:
            return {}
        return {it["id"]: int(it.get("statistics", {}).get("viewCount", 0))
                for it in r.json().get("items", [])}
    except Exception:  # noqa: BLE001
        return {}


def _yt_download(video_id: str, out_path: Path) -> Path | None:
    """Download a CC video at <=720p with yt-dlp (best effort)."""
    yt_dlp = shutil.which("yt-dlp")
    if not yt_dlp:
        log.info("yt-dlp not installed — YouTube footage unavailable")
        return None
    cmd = [
        yt_dlp, "--quiet", "--no-warnings", "--no-playlist",
        "--no-progress", "--retries", "2",
        "-f", "best[height<=720][ext=mp4]/best[height<=720]/best",
        "-o", str(out_path),
        f"https://www.youtube.com/watch?v={video_id}",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=420)
    except subprocess.TimeoutExpired:
        log.info("yt-dlp timed out on %s", video_id)
        return None
    if proc.returncode != 0 or not out_path.exists():
        tail = (proc.stderr or "")[-160:].replace("\n", " ")
        log.info("yt-dlp failed on %s: %s", video_id, tail)
        return None
    if out_path.stat().st_size > MAX_DOWNLOAD_MB * 1e6:
        out_path.unlink(missing_ok=True)
        return None
    return out_path


def _find_yt_source(topic: EpisodeTopic, settings: Settings,
                    work_dir: Path, exclude: set[str]) -> FootageSource | None:
    for duration in ("medium", "short", "long"):
        for query in (f"{topic.animal} wildlife",
                      f"{topic.animal} {topic.hints}", topic.animal):
            items = _yt_search(query, settings, duration)
            if not items:
                log.info("YouTube CC search %r (%s): 0 items",
                         query, duration)
                continue
            cands = []
            for item in items:
                vid = item["id"].get("videoId", "")
                snip = item.get("snippet", {})
                title = snip.get("title", "")
                if not vid or not title:
                    continue
                page = f"https://www.youtube.com/watch?v={vid}"
                if page in exclude:
                    continue
                # relevance: the animal's distinctive words must appear in
                # the title (stops unrelated CC footage sneaking in)
                words = _animal_words(topic.animal)
                title_words = set(re.findall(r"[a-z]+", title.lower()))
                if words and not (words & title_words):
                    continue
                cands.append((vid, title, snip.get("channelTitle", "")))
            log.info("YouTube CC search %r (%s): %d items, %d relevant",
                     query, duration, len(items), len(cands))
            if not cands:
                continue
            views = _yt_views([c[0] for c in cands], settings)
            cands.sort(key=lambda c: -views.get(c[0], 0))
            for vid, title, channel in cands[:4]:
                page = f"https://www.youtube.com/watch?v={vid}"
                out_path = work_dir / f"yt_{vid}.mp4"
                if not out_path.exists():
                    if _yt_download(vid, out_path) is None:
                        continue
                try:
                    dur = probe_duration(out_path)
                except Exception:  # noqa: BLE001
                    out_path.unlink(missing_ok=True)
                    continue
                if dur < MIN_SOURCE_SECONDS:
                    out_path.unlink(missing_ok=True)
                    continue
                return FootageSource(
                    path=out_path, provider="youtube", title=title,
                    url=page,
                    license="CC-BY 3.0 via YouTube's Creative Commons option",
                    channel=channel, duration=dur,
                    views=views.get(vid, 0))
    return None


# ---------------------------------------------------------------------------
# Wikimedia Commons
# ---------------------------------------------------------------------------

def _commons_search(query: str) -> list[dict]:
    try:
        r = requests.get(COMMONS_API, params={
            "action": "query", "format": "json", "list": "search",
            "srnamespace": 6, "srlimit": 20,
            "srsearch": f"{query} filetype:video",
        }, headers={"User-Agent": UA}, timeout=TIMEOUT)
        if r.status_code != 200:
            return []
        hits = r.json().get("query", {}).get("search", [])
    except requests.RequestException:
        return []

    out = []
    for h in hits:
        title = h.get("title", "")          # "File:Foo.webm"
        if not title.startswith("File:"):
            continue
        try:
            r2 = requests.get(COMMONS_API, params={
                "action": "query", "format": "json", "titles": title,
                "prop": "imageinfo",
                "iiprop": "url|mime|size|extmetadata|duration",
            }, headers={"User-Agent": UA}, timeout=TIMEOUT)
            pages = r2.json().get("query", {}).get("pages", {})
            info = next(iter(pages.values())).get("imageinfo", [{}])[0]
        except (requests.RequestException, StopIteration, IndexError, KeyError):
            continue
        if not str(info.get("mime", "")).startswith("video/"):
            continue
        meta = info.get("extmetadata", {})
        license_ = (meta.get("LicenseShortName", {}) or {}).get("value", "")
        author = (meta.get("Artist", {}) or {}).get("value", "")
        author = re.sub(r"<[^>]+>", "", author or "").strip()[:60]
        out.append({
            "title": title[len("File:"):],
            "url": info.get("url", ""),
            "page": f"https://commons.wikimedia.org/wiki/{title.replace(' ', '_')}",
            "license": license_ or "see file page",
            "author": author,
            "duration": float(info.get("duration") or 0.0),
            "size": int(info.get("size") or 0),
        })
    return out


def _url_ext(url: str, default: str = ".webm") -> str:
    from urllib.parse import urlparse
    path = urlparse(url).path
    suffix = Path(path).suffix.lower()
    return suffix if suffix in {".webm", ".ogv", ".mp4", ".mov", ".mkv",
                                ".m4v"} else default


def _find_commons_source(topic: EpisodeTopic, settings: Settings,
                         work_dir: Path,
                         exclude: set[str]) -> FootageSource | None:
    for query in (f"{topic.animal}", f"{topic.hints}",
                  f"{topic.animal} behavior", f"{topic.animal} zoo",
                  "wildlife animals nature"):
        for cand in _commons_search(query):
            url = cand["url"]
            if not url or cand["page"] in exclude:
                continue
            dur = cand["duration"]
            if dur < MIN_SOURCE_SECONDS or dur > 1800:
                continue
            if cand["size"] > MAX_DOWNLOAD_MB * 1e6:
                continue
            # relevance (except for the generic wildlife fallback query)
            if not query.startswith("wildlife"):
                words = _animal_words(topic.animal)
                title_words = set(re.findall(r"[a-z]+",
                                             cand["title"].lower()))
                if words and not (words & title_words):
                    continue
            ext = _url_ext(url)
            out_path = work_dir / f"commons_{abs(hash(url)) % 10_000:04d}{ext}"
            if not out_path.exists():
                try:
                    r = requests.get(url, headers={"User-Agent": UA},
                                     timeout=180, stream=True)
                    if r.status_code != 200:
                        continue
                    size = int(r.headers.get("content-length") or 0)
                    if size > MAX_DOWNLOAD_MB * 1e6:
                        continue
                    with open(out_path, "wb") as fh:
                        for chunk in r.iter_content(64 * 1024):
                            fh.write(chunk)
                except requests.RequestException:
                    continue
            if out_path.stat().st_size > MAX_DOWNLOAD_MB * 1e6:
                out_path.unlink(missing_ok=True)
                continue
            try:
                dur = probe_duration(out_path)
            except Exception:  # noqa: BLE001
                out_path.unlink(missing_ok=True)
                continue
            if dur < MIN_SOURCE_SECONDS:
                out_path.unlink(missing_ok=True)
                continue
            return FootageSource(
                path=out_path, provider="commons",
                title=cand["title"], url=cand["page"],
                license=f"Wikimedia Commons ({cand['license']})",
                channel=cand["author"], duration=dur)
    return None


# ---------------------------------------------------------------------------
# Internet Archive
# ---------------------------------------------------------------------------

def _archive_search(query: str) -> list[dict]:
    try:
        r = requests.get(ARCHIVE_SEARCH, params={
            "q": f"({query}) AND mediatype:(movies)",
            "fl[]": ["identifier", "title", "licenseurl", "downloads"],
            "rows": 12, "page": 1, "output": "json",
            "sort[]": "downloads desc",
        }, headers={"User-Agent": UA}, timeout=TIMEOUT)
        if r.status_code != 200:
            return []
        return r.json().get("response", {}).get("docs", [])
    except (requests.RequestException, ValueError):
        return []


def _license_from_url(url: str) -> str:
    m = re.search(r"licenses/([a-z-]+)/([0-9.]+)", url or "", re.I)
    if m:
        kind, ver = m.group(1).upper(), m.group(2)
        return f"CC {'-'.join(kind.split('-')[:2])} {ver}"
    if "publicdomain" in (url or "").lower():
        return "Public domain"
    return "Internet Archive item (see item page for rights)"


def _find_archive_source(topic: EpisodeTopic, settings: Settings,
                         work_dir: Path,
                         exclude: set[str]) -> FootageSource | None:
    for query in (f"{topic.animal} wildlife", f"{topic.animal} animals",
                  f"{topic.hints} animals", "wildlife animals nature"):
        for doc in _archive_search(query):
            ident = doc.get("identifier", "")
            if not ident or f"archive.org/details/{ident}" in exclude:
                continue
            if not query.startswith("wildlife"):
                words = _animal_words(topic.animal)
                title_words = set(re.findall(r"[a-z]+",
                                             str(doc.get("title", "")).lower()))
                if words and not (words & title_words):
                    continue
            try:
                meta = requests.get(ARCHIVE_META.format(id=ident),
                                    headers={"User-Agent": UA},
                                    timeout=TIMEOUT).json()
            except (requests.RequestException, ValueError):
                continue
            files = meta.get("files", [])
            vids = [f for f in files
                    if str(f.get("name", "")).lower().endswith(
                        (".mp4", ".ogv", ".webm"))
                    and f.get("size")
                    and 500_000 <= int(f["size"]) <= MAX_DOWNLOAD_MB * 1e6]
            if not vids:
                continue
            vids.sort(key=lambda f: (0 if str(f.get("name", "")).endswith(".mp4")
                                     else 1, int(f.get("size", 0))))
            fname = vids[0]["name"]
            durl = f"https://archive.org/download/{ident}/{fname}"
            out_path = work_dir / f"ia_{ident[:40]}{_url_ext(durl, '.mp4')}"
            if not out_path.exists():
                try:
                    rr = requests.get(durl, headers={"User-Agent": UA},
                                      timeout=240, stream=True)
                    if rr.status_code != 200:
                        continue
                    with open(out_path, "wb") as fh:
                        for chunk in rr.iter_content(64 * 1024):
                            fh.write(chunk)
                except requests.RequestException:
                    continue
            if out_path.stat().st_size > MAX_DOWNLOAD_MB * 1e6:
                out_path.unlink(missing_ok=True)
                continue
            try:
                dur = probe_duration(out_path)
            except Exception:  # noqa: BLE001
                out_path.unlink(missing_ok=True)
                continue
            if dur < MIN_SOURCE_SECONDS:
                out_path.unlink(missing_ok=True)
                continue
            license_ = _license_from_url(
                doc.get("licenseurl", "") or
                str(meta.get("metadata", {}).get("rights", "")))
            return FootageSource(
                path=out_path, provider="archive",
                title=str(doc.get("title", ident))[:80],
                url=f"https://archive.org/details/{ident}",
                license=license_, duration=dur,
                views=int(doc.get("downloads", 0) or 0))
    return None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def collect_footage(topic: EpisodeTopic, settings: Settings,
                    work_dir: Path, needed_seconds: float) -> list[FootageSource]:
    """Collect real footage until the timeline reaches needed_seconds.

    Returns the list of sources with their segments assigned. Never raises —
    a short return list simply fails the footage quality gate later.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    sources: list[FootageSource] = []
    exclude: set[str] = set()
    total = 0.0

    log.info("FOOTAGE HUNT: %s — need %.0fs of real video",
             topic.animal, needed_seconds)
    while total < needed_seconds and len(sources) < settings.max_sources:
        src = None
        # alternate providers so a single blocked source never dominates
        for finder in (_find_yt_source if settings.enable_yt_footage
                       else None,
                       _find_commons_source if settings.enable_commons
                       else None,
                       _find_archive_source if settings.enable_archive
                       else None):
            if finder is None:
                continue
            src = None
            try:
                src = finder(topic, settings, work_dir, exclude)
            except Exception as exc:  # noqa: BLE001
                log.info("footage finder %s failed: %s",
                         finder.__name__, exc)
            if src is not None:
                break
        if src is None:
            if sources:
                break           # keep what we have; QA gate decides
            log.error("No usable footage found for %s from any source.",
                      topic.animal)
            return []

        exclude.add(src.url)
        remaining = needed_seconds - total
        seg_len = settings.segment_max_seconds
        n_max = settings.max_segments - sum(len(s.segments) for s in sources)
        if n_max <= 0:
            break
        usable_end = src.duration * 0.97 - 0.3
        offsets = _segment_offsets(src.duration, seg_len,
                                   min(4, n_max))
        for off in offsets:
            # honest accounting: only time that actually exists past the
            # offset counts (an offset near the end yields almost nothing)
            avail = usable_end - off
            take = min(seg_len, max(4.0, remaining), avail)
            if take < 4.0:
                continue
            src.segments.append(FootageSegment(
                path=src.path, start=off, seconds=take,
                source_index=len(sources)))
            total += take
            remaining -= take
        sources.append(src)
        log.info("footage source %d/%d: %s “%s” (%.0fs, %d views) → %d "
                 "segments, %.0fs total so far",
                 len(sources), settings.max_sources, src.provider,
                 src.title[:44], src.duration, src.views,
                 len(src.segments), total)

    log.info("FOOTAGE READY: %d sources, %d segments, %.0fs of real video "
             "(needed %.0fs)", len(sources),
             sum(len(s.segments) for s in sources), total, needed_seconds)
    return sources


def attribution_lines(sources: list[FootageSource]) -> list[str]:
    """Description block attributing every source actually used."""
    return [s.attribution_line() for s in sources if s.segments]

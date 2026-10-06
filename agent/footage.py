"""Real animal footage from license-clean sources — the MAIN visual track.

The channel is dedicated to the BIG 15 (Big Five + ten more giants),
and every episode is built from real, moving wildlife video of exactly
that animal (never static slides). Three sources, all legal for reuse:

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

Repeats stay fresh: `used_before` (state.json → used_sources) blocks every
source URL already featured in a past episode of this animal, so a repeat
episode is always cut from DIFFERENT videos.
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import requests

from .animals import ALIASES, CATALOG, EpisodeTopic, SCI_ALIASES
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


# ---------------------------------------------------------------------------
# Relevance: the footage must be ABOUT the episode's BIG-15 animal
# ---------------------------------------------------------------------------

# Light stemming so plurals match ("Leopards of Kruger" must count as
# "leopard"). ONLY a trailing "s" is stripped — an "es"/"ies" rule
# would wreck words like "whales"/"giraffes"/"crocodiles"; their genuine
# irregulars live in the map below instead.
_IRREGULAR = {"wolves": "wolf", "mice": "mouse", "geese": "goose",
              "moose": "moose", "fish": "fish", "sheep": "sheep",
              "deer": "deer", "foxes": "fox", "oxen": "ox",
              "tigress": "tiger", "tigresses": "tiger",
              "lioness": "lion", "lionesses": "lion",
              "leopardess": "leopard", "leopardesses": "leopard",
              "cubs": "cub", "buffaloes": "buffalo",
              "grizzlies": "grizzly", "lynxes": "lynx",
              "hippos": "hippo", "rhinos": "rhino", "crocs": "croc"}


def _stem(w: str) -> str:
    w = w.lower()
    if w in _IRREGULAR:
        return _IRREGULAR[w]
    if w.endswith("s") and len(w) - 1 >= 3:
        return w[:-1]
    return w


def _alias_sets(animal: str) -> list[tuple[str, ...]]:
    """Alias token sets: a title matches the animal when ANY alias appears
    (single word anywhere, multi-word as an adjacent phrase). "rhino"
    counts for Rhinoceros, "brown bear"/"kodiak" for Grizzly Bear,
    "killer whale" for Orca, "panthera pardus" for Leopard."""
    out: list[tuple[str, ...]] = []
    for alias in (*ALIASES.get(animal, (animal,)),
                  *SCI_ALIASES.get(animal, ())):
        toks = tuple(_stem(w) for w in
                     re.findall(r"[a-z]{3,}", alias.lower()))
        if toks:
            out.append(toks)
    return out


def _common_words(animal: str) -> set[str]:
    """COMMON-name alias words only (no scientific names) — vehicle-style
    patterns ("Leopard 2A7", "Tiger 131") only ever use common names, so
    the numbered-name check must not burn sci-word zoo files like
    "Panthera leo 01"."""
    out: set[str] = set()
    for alias in ALIASES.get(animal, (animal,)):
        out |= {_stem(w) for w in
                re.findall(r"[a-z]{3,}", alias.lower())}
    return out


def _title_tokens(title: str) -> list[str]:
    return [_stem(w) for w in re.findall(r"[a-z]{3,}", (title or "").lower())]


def _find_phrase(seq: list[str], words: tuple[str, ...]) -> int | None:
    """Index of the FIRST occurrence of the alias in the token sequence.
    Single word: anywhere. Multi-word: adjacent (order-insensitive)."""
    n = len(words)
    if n == 1:
        try:
            return seq.index(words[0])
        except ValueError:
            return None
    wset = set(words)
    for i in range(len(seq) - n + 1):
        if set(seq[i:i + n]) == wset:
            return i
    return None


# Confuser pairs — a DIFFERENTLY-NAMED species built from one of our
# words ("leopard seal", "tiger shark", "wolf spider", "lion fish",
# "elephant shrew"...). Only rejects when the second word is not part
# of the animal's own aliases.
_CONFUSERS = {"tortoise", "seal", "shark", "gecko", "frog", "toad",
              "moth", "butterfly", "eagle", "ray", "snake", "lizard",
              "cat", "dog", "danio", "cichlid", "wrasse", "puffer",
              "bass", "trout", "pigeon", "dove", "finch", "parrot",
              "warbler", "babbler", "octopus", "squid", "crab",
              "spider", "wasp", "beetle", "mantis", "cricket",
              "scorpion", "turtle", "viper", "cobra", "snail",
              "slug", "orchid", "lily", "urchin", "fish", "fly",
              "shrimp", "eel", "heron", "shrew"}

# LOOKALIKE species — closely related but DIFFERENT species whose names
# contain one of our words. ALWAYS rejected, regardless of position: a
# "snow leopard" video is not a Leopard video; "water buffalo" is not a
# Cape Buffalo; "asian elephant" is not the African Elephant.
_LOOKALIKE: list[tuple[str, ...]] = [
    ("snow", "leopard"), ("clouded", "leopard"), ("leopard", "seal"),
    ("leopard", "cat"), ("leopard", "tortoise"), ("leopard", "gecko"),
    ("leopard", "shark"), ("sea", "leopard"),
    ("asian", "elephant"), ("indian", "elephant"),
    ("forest", "elephant"), ("pygmy", "elephant"),
    ("water", "buffalo"), ("american", "buffalo"),
    ("pygmy", "hippo"),
    ("american", "crocodile"), ("saltwater", "crocodile"),
    ("american", "alligator"),
    ("black", "bear"), ("sun", "bear"), ("sloth", "bear"),
    ("spectacled", "bear"), ("moon", "bear"),
    ("sea", "lion"), ("mountain", "lion"),
    ("bullfrog",), ("catfish",),
]

# OTHER animals — not in the Big-15 catalog and never an episode topic.
# CO-SUBJECT species (other predators, apes, birds, reptiles...): both
# the subject-first rule AND the menagerie rule apply — two or more
# distinct co-subjects named means a sanctuary reel / zoo mix. Entries
# that are a subset of the topic animal's own alias tokens never reject
# (the "bear" in "polar bear", the "whale" in "killer whale").
_OTHER_ANIMALS: list[tuple[str, ...]] = [
    ("hyena",), ("baboon",), ("monkey",), ("chimpanzee",),
    ("chimp",), ("orangutan",), ("meerkat",), ("mongoose",),
    ("camel",), ("goose",), ("horse",), ("donkey",),
    ("chicken",), ("duck",), ("dog",), ("cat",), ("fox",),
    ("coyote",), ("jackal",), ("walrus",), ("otter",),
    ("beaver",), ("raccoon",), ("skunk",), ("porcupine",),
    ("squirrel",), ("kangaroo",), ("koala",), ("panda",),
    ("peacock",), ("ostrich",), ("emu",), ("vulture",),
    ("eagle",), ("owl",), ("parrot",), ("dolphin",), ("shark",),
    ("whale",), ("turtle",), ("tortoise",), ("snake",),
    ("python",), ("cobra",), ("lizard",), ("iguana",),
    ("alligator",), ("caiman",), ("gharial",), ("jaguar",),
    ("puma",), ("cougar",), ("lynx",), ("bobcat",),
    ("wild", "dog"), ("dingo",), ("lemur",), ("sloth",),
    ("bear",), ("bison",),
]

# Natural PREY species: hunting footage legitimately names the prey
# ("Lion hunts zebra", "Orca hunting sea lions"), so these only trigger
# the subject-first rule — never the menagerie rule.
_PREY_ANIMALS: list[tuple[str, ...]] = [
    ("zebra",), ("wildebeest",), ("gazelle",), ("antelope",),
    ("impala",), ("kudu",), ("warthog",), ("seal",),
    ("sea", "lion"), ("penguin",), ("goat",), ("sheep",),
    ("cow",), ("cattle",), ("pig",), ("boar",), ("deer",),
    ("elk",), ("moose",), ("rabbit",),
]

# Machines named after animals (Yakovlev "Yak-130" jets, "Leopard 2A7"
# tanks, "Tiger Moth" planes, sports teams, dishes...) — never real
# footage of the animal. Either a vehicle/event keyword anywhere, or the
# "<animal>-<number>" vehicle naming pattern, rejects the source.
_MACHINE_WORDS = {"aircraft", "airplane", "aeroplane", "flugzeug",
                  "avion", "airliner", "jet", "fighter", "bomber",
                  "helicopter", "heli", "panzer", "missile",
                  "rocket", "locomotive", "tramway", "airbase",
                  "squadron", "aerobatics", "starvorgang", "yakovlev",
                  "airshow", "taxiing", "luftwaffe", "wehrmacht",
                  "kampfjet", "tank", "golf", "nfl", "football",
                  "soccer", "hockey", "stadium", "quarterback",
                  "recipe", "cooking", "wings", "bbq", "dance",
                  "dragon", "plunge", "trailer", "gameplay",
                  "minecraft", "robot", "review", "unboxing",
                  "covid", "pandemic", "firefighter", "sapeurs"}

_CATALOG_ALIASES = [(name, _alias_sets(name)) for name, _r, _h in CATALOG]


def _strip_big_cat(title: str) -> str:
    """"Big cats" is a family label for our own felines, not another
    animal ("LION: King of the Big Cats" must pass for Lion)."""
    return re.sub(r"\bbig[- ]cats?\b", " ", title or "", flags=re.I)


def _title_relevant(title: str, animal: str) -> tuple[bool, str]:
    """Strict BIG-15 title gate for footage of THIS animal.

    Rules (on stemmed tokens):
    1. at least one ALIAS of the animal must appear (multi-word aliases
       match as adjacent phrases — "killer whale" counts for Orca,
       "brown bear" for Grizzly Bear, "rhino" for Rhinoceros);
    2. lookalike species always reject ("snow leopard" ≠ Leopard,
       "water buffalo" ≠ Cape Buffalo, "asian elephant" ≠ the African);
    3. confuser pairs reject ("leopard seal", "tiger shark",
       "wolf spider", "lion fish");
    4. machines/events named after the animal reject (jets, tanks,
       numbered vehicles, sports teams, dishes);
    5. any OTHER animal (Big-15 or common) mentioned BEFORE our animal
       rejects the source — "Zebra escapes the lion" is a zebra video,
       and zoo compilations list their animals up front. Our animal
       first is fine: "Lion hunts zebra" IS lion footage.
    """
    seq = _title_tokens(title)
    mine = _alias_sets(animal)
    if not mine:
        return True, ""
    my_first = min((i for i in (_find_phrase(seq, a) for a in mine)
                    if i is not None), default=None)
    if my_first is None:
        missing = ", ".join(" ".join(a) for a in mine)
        return False, f"animal words missing ({missing})"

    my_words = {w for a in mine for w in a}

    # 2. lookalike species — always reject, but only when the lookalike
    # name actually builds on one of OUR words ("snow leopard" vs the
    # Leopard topic; "sea lion" vs the Lion topic — while "Orca hunting
    # sea lions" stays legal predation footage for the Orca topic)
    for phrase in _LOOKALIKE:
        if set(phrase) <= my_words:
            continue          # part of this animal's own names
        if not (set(phrase) & my_words):
            continue          # lookalike of some OTHER animal, not ours
        if _find_phrase(seq, phrase) is not None:
            return False, f"lookalike species ('{' '.join(phrase)}')"

    # 3. confuser pairs ("leopard seal", "tiger shark"...)
    for i in range(len(seq) - 1):
        if seq[i] in my_words and seq[i + 1] in _CONFUSERS \
                and seq[i + 1] not in my_words:
            return False, f"different species ('{seq[i]} {seq[i + 1]}')"

    # 4. machines / events / dishes named after the animal
    tokens = set(seq)
    if tokens & _MACHINE_WORDS:
        return False, "machine or event named after the animal"
    for w in _common_words(animal):
        if re.search(rf"\b{re.escape(w)}[- ]?\d", title.lower()):
            return False, f"numbered vehicle name ('{w}-<number>')"

    # 5. other animals — subject-first heuristic + menagerie detector
    scan = _title_tokens(_strip_big_cat(title))
    my_scan = min((i for i in (_find_phrase(scan, a) for a in mine)
                   if i is not None), default=0)

    def _idx(words: tuple[str, ...]) -> int | None:
        if set(words) <= my_words:
            return None           # part of our own name (bear/whale/wolf)
        return _find_phrase(scan, words)

    distinct_others: set[str] = set()
    for other_name, alias_list in _CATALOG_ALIASES:
        if other_name.lower() == animal.lower():
            continue
        for a in alias_list:
            idx = _idx(a)
            if idx is None:
                continue
            if idx < my_scan:
                return False, f"other animal first ({other_name})"
            distinct_others.add(other_name)   # present, but after ours
            break
    for phrase in _OTHER_ANIMALS:
        idx = _idx(phrase)
        if idx is None:
            continue
        if idx < my_scan:
            return False, f"other animal first ({' '.join(phrase)})"
        distinct_others.add(" ".join(phrase))
    if len(distinct_others) >= 2:
        return False, ("menagerie / multi-species reel ("
                       + ", ".join(sorted(distinct_others)) + ")")
    for phrase in _PREY_ANIMALS:
        idx = _idx(phrase)
        if idx is not None and idx < my_scan:
            return False, f"prey animal first ({' '.join(phrase)})"
    return True, ""


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
        log.info("YouTube CC footage: no OAuth token yet — using "
                 "Commons/Internet Archive (set YT_REFRESH_TOKEN to enable)")
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
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=420, stdin=subprocess.DEVNULL)
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


def _env_query(topic: EpisodeTopic) -> str:
    """Environment-flavored search variant, e.g. 'Lion Serengeti plains'
    — drives DIFFERENT footage for repeat episodes of the same animal."""
    env = (topic.environment or "").strip()
    for prefix in ("the ", "a ", "its "):
        if env.lower().startswith(prefix):
            env = env[len(prefix):]
    return f"{topic.animal} {env}".strip() if env else topic.animal


def _find_yt_source(topic: EpisodeTopic, settings: Settings,
                    work_dir: Path, exclude: set[str]) -> FootageSource | None:
    for duration in ("medium", "short", "long"):
        for query in (f"{topic.animal} wildlife",
                      f"{topic.animal} {topic.hints}",
                      _env_query(topic), topic.animal):
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
                # strict relevance: this title must be ABOUT the animal
                ok, why = _title_relevant(title, topic.animal)
                if not ok:
                    log.info("  skip YT %r: %s", title[:44], why)
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
    # Animal-specific queries ONLY. There is deliberately NO generic
    # "wildlife" fallback: an episode about Leopards must contain leopards,
    # not whatever the generic search drags in (camels, goats, geese).
    # A thin but on-topic result set is corrected by the script sizing
    # itself to the footage we actually found.
    for query in (f"{topic.animal}", f"{topic.animal} {topic.hints}",
                  f"{topic.animal} hunting", f"{topic.animal} behavior",
                  _env_query(topic),
                  f"{topic.animal} {topic.region}",
                  f"{topic.animal} national park", f"{topic.animal} zoo",
                  f"{topic.animal} cubs"):
        for cand in _commons_search(query):
            url = cand["url"]
            if not url or cand["page"] in exclude:
                continue
            dur = cand["duration"]
            if dur < MIN_SOURCE_SECONDS or dur > 1800:
                continue
            if cand["size"] > MAX_DOWNLOAD_MB * 1e6:
                continue
            # strict relevance for every query (no exceptions)
            ok, why = _title_relevant(cand["title"], topic.animal)
            if not ok:
                log.info("  skip Commons %r: %s", cand["title"][:44], why)
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
    # animal-specific queries ONLY — see note in _find_commons_source
    for query in (f"{topic.animal} wildlife", f"{topic.animal} animals",
                  f"{topic.animal} {topic.hints}", f"{topic.animal} film",
                  _env_query(topic),
                  f"{topic.animal} {topic.region}"):
        for doc in _archive_search(query):
            ident = doc.get("identifier", "")
            if not ident or f"https://archive.org/details/{ident}" in exclude:
                continue
            ok, why = _title_relevant(str(doc.get("title", "")),
                                      topic.animal)
            if not ok:
                log.info("  skip Archive %r: %s",
                         str(doc.get("title", ""))[:44], why)
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
                    work_dir: Path, needed_seconds: float,
                    used_before: set[str] | None = None
                    ) -> list[FootageSource]:
    """Collect real footage until the timeline reaches needed_seconds.

    `used_before` carries the source URLs of this animal's PAST episodes
    (state.json → used_sources): they are excluded so a repeat episode
    is always cut from DIFFERENT source videos.

    Returns the list of sources with their segments assigned. Never raises —
    a short return list simply fails the footage quality gate later.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    sources: list[FootageSource] = []
    exclude: set[str] = set(used_before or set())
    total = 0.0

    log.info("FOOTAGE HUNT: %s — need %.0fs of real video "
             "(%d past source URLs blocked)",
             topic.animal, needed_seconds, len(exclude))
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

        # VLM verification: the animal must actually APPEAR in the frames
        # we are about to cut. Title filters catch most mismatches; this
        # catches the rest (zoo compilations, misfiled videos). None =
        # vision API unavailable → accept (title filter already ran).
        if settings.enable_vlm_verify and settings.gemini_api_key:
            from . import vision
            verdict = vision.frames_show_animal(
                src.path, topic.animal, settings, offsets)
            if verdict is False:
                log.info("VLM REJECT %r: %s not visible at planned cuts — "
                         "dropping this source", src.title[:48],
                         topic.animal)
                src.path.unlink(missing_ok=True)
                continue        # keep hunting; url already excluded

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

"""Orchestrate one episode run: pick animal → collect REAL footage →
script sized to the footage → narrate → assemble → upload.

The channel posts TWICE A DAY (every ~12 hours). Run manually:
    python -m agent.main                     # next episode (dry state safe)
    python -m agent.main --no-upload         # render only (dry run)
    python -m agent.main --force             # bypass the 12-hour guard
    python -m agent.main --animal "Lion"     # override the animal choice
"""
from __future__ import annotations

import argparse
import json
import logging
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import animals as animals_mod, footage as footage_mod, popularity, qa, state as state_mod
from .config import OUTPUT_DIR, ROOT, Settings, WORK_DIR
from .scriptgen import generate_script
from .tts import synth_sections
from .thumbnail import best_frame, make_outro_card, make_thumbnail, make_title_card
from .video import probe_duration, render_episode

log = logging.getLogger("main")


def _banner(title: str) -> None:
    log.info("=" * 62)
    log.info(title)
    log.info("=" * 62)


def build_tags(animal: str, settings: Settings) -> list[str]:
    tags = [
        "wildlife", "animals", "nature", "wildlife documentary",
        "animal documentary", "wild animals", "nature documentary",
        settings.channel_name, animal.lower(),
    ]
    for word in animal.split():
        w = word.strip(".,:;!?\"'()[]").lower()
        if 4 <= len(w) <= 24 and w.isalnum():
            tags.append(w)
    out, used = [], 0
    for t in tags:
        if used + len(t) + 1 > 480:
            break
        out.append(t)
        used += len(t) + 1
    return out


def plan_timeline(sources, narration_seconds: float, title_s: float,
                  outro_s: float) -> list[tuple[Path, float, float]]:
    """Pick exactly enough footage segments to cover the narration.

    Pass 1 uses the planned segments (spread offsets — visual variety).
    Pass 2 tops up from any still-unused ranges of each source, so the
    narration is fully covered whenever the collected footage allows it.

    Returns [(source_path, start_offset, seconds)] whose total equals
    (narration + tail - cards), trimming the last block as needed.
    """
    target = narration_seconds + 2.0 - title_s - outro_s
    if target < 10.0:
        target = 10.0
    plan: list[tuple[Path, float, float]] = []
    acc = 0.0
    intervals: dict[int, list[tuple[float, float]]] = {}

    def _take(path: Path, start: float, avail: float) -> None:
        nonlocal acc
        take = min(avail, target - acc)
        if take < 2.0:
            return
        plan.append((path, start, take))
        intervals.setdefault(id(path), []).append((start, start + take))
        acc += take

    # pass 1: planned segments
    for src in sources:
        for seg in src.segments:
            if acc >= target:
                break
            avail = min(seg.seconds, src.duration * 0.97 - seg.start - 0.3)
            if avail < 2.0:
                continue
            _take(src.path, seg.start, avail)
    # pass 2: top-up from unused ranges (30s filler blocks)
    if acc < target:
        block = 30.0
        for src in sources:
            if acc >= target:
                break
            lo, hi = src.duration * 0.03, src.duration * 0.97 - 0.3
            off = lo
            while off < hi and acc < target:
                end = min(off + block, hi)
                clash = any(not (end <= a or off >= b)
                            for (a, b) in intervals.get(id(src.path), []))
                if clash or end - off < 5.0:
                    off += block
                    continue
                _take(src.path, off, end - off)
                off = end
    return plan


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Animals Planet Space agent")
    parser.add_argument("--no-upload", action="store_true",
                        help="render the video but do not upload")
    parser.add_argument("--keep-state", action="store_true",
                        help="do not advance state.json")
    parser.add_argument("--force", action="store_true",
                        help="bypass the 12-hour cadence guard")
    parser.add_argument("--animal", default="",
                        help="override the animal choice (testing)")
    parser.add_argument("--output-dir", default=str(OUTPUT_DIR))
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    settings = Settings.from_env()
    if args.no_upload:
        settings.dry_run = True
    if args.keep_state:
        settings.keep_state = True
    if args.force:
        settings.force_upload = True

    started = time.time()
    st = state_mod.load(settings=settings)
    _banner(f"ANIMALS PLANET SPACE — {settings.channel_name} (2x daily)")

    # 1. TWICE-A-DAY GUARD -----------------------------------------------
    if not settings.dry_run and not settings.force_upload:
        allowed, why = state_mod.cadence_allows_post(st, settings)
        if not allowed:
            log.info("CADENCE GUARD: %s — skipping this run. The channel "
                     "posts every ~12 hours. Use --force / FORCE_UPLOAD "
                     "to override.", why)
            return 0
        log.info("CADENCE GUARD: %s", why)

    # 1b. WAITING FOR OAUTH ------------------------------------------------
    # A scheduled run without YouTube credentials would burn minutes of
    # rendering for nothing — exit early and wait for the token.
    if not settings.has_youtube_credentials and not settings.dry_run \
            and not settings.force_upload:
        log.warning("YouTube credentials not set (YT_CLIENT_ID / "
                    "YT_CLIENT_SECRET / YT_REFRESH_TOKEN). Run the OAuth "
                    "consent described in README.md, set YT_REFRESH_TOKEN, "
                    "then dispatch the workflow again. Nothing rendered — "
                    "no quota wasted.")
        return 0

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rng = random.Random()

    # 1c. CHANNEL POPULARITY — which animals earned the most views + likes
    # ("the most liked/watched videos are the ones you should create more
    # than others"). Empty on any failure → uniform rotation fallback.
    try:
        scores, past_titles = popularity.channel_performance(st, settings)
    except Exception as exc:  # noqa: BLE001
        log.warning("popularity scoring unavailable (%s) — uniform "
                    "rotation", exc)
        scores, past_titles = {}, {}

    # 2+3. Pick the animal and hunt REAL footage — with RETRIES. The strict
    # relevance gate can come up dry for an obscure animal (every candidate
    # was off-topic or a machine named after it); a slot never dies for one
    # animal's sake: the next animal is tried immediately (up to
    # ANIMAL_ATTEMPTS). Only a fully dry hunt exhausts the run.
    topic = None
    sources: list = []
    footage_total = 0.0
    attempted: list[str] = []
    for attempt in range(1, settings.animal_attempts + 1):
        if args.animal:
            if attempt > 1:
                break          # explicit --animal: single attempt only
            topic = animals_mod.EpisodeTopic(
                animal=args.animal, hints=args.animal, region="custom",
                angle_title=animals_mod.ANGLES[0][0],
                angle_brief=animals_mod.ANGLES[0][1])
        else:
            topic = animals_mod.pick_topic(st, settings, rng=rng,
                                           exclude=set(attempted),
                                           scores=scores)
        attempted.append(topic.animal.lower())
        needed = settings.target_minutes * 60.0
        # block every source video already used in a PAST episode of this
        # animal — repeats are always cut from different footage
        used_before = set(
            (st.get("used_sources") or {})          # type: ignore[arg-type]
            .get(topic.animal.lower(), []))
        sources = footage_mod.collect_footage(topic, settings,
                                              out_dir / "footage", needed,
                                              used_before=used_before)
        footage_total = sum(s.used_seconds for s in sources)
        if footage_total >= settings.min_footage_minutes * 60:
            break
        log.error("FOOTAGE GATE: only %.0fs of clean footage found for %s "
                  "(minimum %.0fs) — %s a different animal.",
                  footage_total, topic.animal,
                  settings.min_footage_minutes * 60,
                  "trying" if attempt < settings.animal_attempts
                  else "no attempts left")

    if footage_total < settings.min_footage_minutes * 60:
        log.error("No animal produced enough clean footage this run "
                  "(tried: %s). The next scheduled slot retries.",
                  ", ".join(attempted))
        qa.write_report(out_dir / "qa_report.json", [
            qa.Check("footage_supply", False,
                     f"{footage_total:.0f}s found (minimum "
                     f"{settings.min_footage_minutes * 60:.0f}s); tried: "
                     f"{', '.join(attempted)}")],
            stage="footage", animal=", ".join(attempted))
        return 1

    # 4. Script sized to the footage -----------------------------------------
    rounds = max(1, settings.script_retry_rounds)
    script_checks = []
    script = None
    my_past_titles = past_titles.get(topic.animal.lower(), [])
    for rnd in range(1, rounds + 1):
        script = generate_script(topic, settings, footage_total,
                                 past_titles=my_past_titles)
        log.info("Script ready — round %d/%d (%d words, %d sections, "
                 "source=%s)", rnd, rounds, script.word_count,
                 len(script.sections), script.source)
        script_checks = qa.check_script(script, settings,
                                        footage_seconds=footage_total)
        if all(c.passed for c in script_checks):
            break
        if not settings.gemini_api_key and script.source == "template":
            log.error("SCRIPT QA failed on a deterministic template with no "
                      "AI backend configured — aborting (no retry).")
            break
        fails = "; ".join(f"{c.name} ({c.detail})" for c in script_checks
                          if not c.passed)
        if rnd < rounds:
            wait_min = settings.storm_wait_seconds // 60
            log.error("SCRIPT QA failed on round %d/%d: %s — the AI backend "
                      "is likely storming. Waiting %d minutes for it to "
                      "recover, then retrying.", rnd, rounds, fails, wait_min)
            time.sleep(settings.storm_wait_seconds)
        else:
            log.error("SCRIPT QA failed on every round: %s", fails)

    qa.log_checks("SCRIPT QA", script_checks)
    if not all(c.passed for c in script_checks):
        qa.write_report(out_dir / "qa_report.json", script_checks,
                        stage="script", script_words=script.word_count,
                        script_source=script.source, animal=topic.animal)
        for c in script_checks:
            if not c.passed:
                log.error("SCRIPT QA FAIL — %s: %s", c.name, c.detail)
        log.error("The script does not meet the channel guidelines — "
                  "refusing to render or upload. The next scheduled slot "
                  "will retry.")
        return 1

    # 5. Narration -------------------------------------------------------------
    narrations = []
    for sec in script.sections:
        text = sec.narration
        if sec.type == "intro" and script.hook:
            text = f"{script.hook} {text}".strip()
        narrations.append(text)
    wavs = synth_sections(narrations, settings.voice, settings.tts_rate,
                          WORK_DIR / "audio")

    from .video import _concat as concat_audio
    narration_wav = WORK_DIR / "narration.wav"
    concat_audio(wavs, narration_wav, "audio")
    narration_dur = probe_duration(narration_wav)
    log.info("Narration: %.1f min over %.1f min of footage",
             narration_dur / 60, footage_total / 60)

    # 6. Plan the footage timeline to the narration ---------------------------
    plan = plan_timeline(sources, narration_dur,
                         settings.title_card_seconds,
                         settings.outro_card_seconds)
    if not plan:
        log.error("Could not plan a footage timeline — aborting.")
        return 1

    # 7. Thumbnail + branded cards (from REAL frames) --------------------------
    hero = max(sources, key=lambda s: (s.views, s.used_seconds))
    frame_png = WORK_DIR / "hero_frame.png"
    if best_frame(hero.path, frame_png) is None:
        for s in sources[1:]:
            if best_frame(s.path, frame_png):
                break
    if not frame_png.exists():
        log.error("Could not extract any frame for the thumbnail — aborting.")
        return 1
    date_slug = datetime.now(timezone.utc).strftime("%Y_%m_%d_%H%M")
    thumb_jpg = out_dir / f"animals_{date_slug}_thumb.jpg"
    make_thumbnail(frame_png, topic.animal,
                   script.thumbnail_text or topic.angle_title, thumb_jpg)
    title_png = WORK_DIR / "card_title.png"
    outro_png = WORK_DIR / "card_outro.png"
    make_title_card(frame_png, topic.animal, script.thumbnail_text,
                    settings.channel_handle, title_png)
    make_outro_card(frame_png, settings.channel_handle, outro_png)

    # 8. Assemble the episode ---------------------------------------------------
    video_path = out_dir / f"animals_{date_slug}.mp4"
    stats = render_episode(title_png, outro_png, plan, narration_wav,
                           video_path,
                           title_seconds=settings.title_card_seconds,
                           outro_seconds=settings.outro_card_seconds,
                           work_dir=WORK_DIR / "segments")

    # 9. Description with chapters ----------------------------------------------
    section_chapters: list[tuple[str, float]] = []
    t_cursor = settings.title_card_seconds
    for sec, wav in zip(script.sections, wavs):
        label = sec.title or sec.type.title()
        section_chapters.append((label[:60], t_cursor))
        t_cursor += probe_duration(wav)
    from .youtube import build_description
    title = (script.title or f"{topic.animal}: Real Wildlife Footage | "
             f"Animals Planet Space").strip()
    footage_lines = footage_mod.attribution_lines(sources)
    description = build_description(script.description, topic.animal,
                                     topic.angle_title, section_chapters,
                                     settings, footage_lines=footage_lines)
    tags = build_tags(topic.animal, settings)

    # 9b. QUALITY GATE — the video must earn its upload -------------------------
    video_checks = qa.check_video(video_path, stats, settings)
    packaging_checks = qa.check_packaging(title, description, thumb_jpg,
                                          len(footage_lines))
    qa.log_checks("VIDEO QA", video_checks + packaging_checks)
    qa_passed = qa.write_report(
        out_dir / "qa_report.json",
        script_checks + video_checks + packaging_checks,
        script_words=script.word_count, script_source=script.source,
        duration_seconds=round(stats["duration"], 1), title=title,
        animal=topic.animal, angle=topic.angle_title,
        footage_sources=[{"provider": s.provider, "title": s.title,
                          "url": s.url, "license": s.license,
                          "views": s.views,
                          "seconds_used": round(s.used_seconds, 1)}
                         for s in sources])
    if not qa_passed:
        for c in video_checks + packaging_checks:
            if not c.passed:
                log.error("VIDEO QA FAIL — %s: %s", c.name, c.detail)
        log.error("QUALITY GATE FAILED — this episode does not meet the "
                  "channel guidelines, so it will NOT be uploaded. The "
                  "next scheduled slot will retry.")
        return 1
    log.info("QUALITY GATE PASSED (%d checks green) — clear to upload.",
             len(script_checks) + len(video_checks) + len(packaging_checks))

    # 10. Upload ---------------------------------------------------------------------
    video_id, video_url = "", ""
    if settings.dry_run:
        log.info("DRY RUN — skipping upload (video saved at %s)", video_path)
    elif not settings.has_youtube_credentials:
        log.warning("YouTube credentials not set — skipping upload. The "
                    "finished video is at %s", video_path)
    else:
        from .youtube import set_thumbnail, upload_video
        result = upload_video(video_path, title, description, tags, settings)
        video_id, video_url = result["video_id"], result["url"]
        set_thumbnail(video_id, thumb_jpg, settings)
        log.info("LIVE: %s", video_url)

    # 11. Persist metadata + advance state --------------------------------------------
    metadata = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "animal": topic.animal,
        "region": topic.region,
        "angle": topic.angle_title,
        "title": title,
        "description": description[:500],
        "tags": tags,
        "footage_sources": [{"provider": s.provider, "title": s.title,
                             "url": s.url, "license": s.license,
                             "views": s.views,
                             "seconds_used": round(s.used_seconds, 1)}
                            for s in sources],
        "video_id": video_id,
        "video_url": video_url,
        "video_file": str(video_path),
        "thumbnail_file": str(thumb_jpg),
        "duration_seconds": round(stats["duration"], 1),
        "footage_seconds": stats.get("footage_seconds"),
        "footage_ratio": stats.get("footage_ratio"),
        "script_source": script.source,
        "script_words": script.word_count,
        "qa_passed": qa_passed,
        "uploaded": bool(video_id),
    }
    (out_dir / "metadata.json").write_text(
        json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    (out_dir / "last_script.json").write_text(
        json.dumps({
            "hook": script.hook, "thumbnail_text": script.thumbnail_text,
            "title": script.title, "description": script.description,
            "sections": [vars(s) for s in script.sections],
        }, indent=2) + "\n", encoding="utf-8")

    state_path = Path(settings.state_file) if settings.state_file \
        else ROOT / "state.json"
    if video_id and not settings.keep_state:
        state_mod.advance(state_path, video_url, video_id, topic, settings,
                          source_urls=[s.url for s in sources
                                       if s.segments])
    elif settings.keep_state:
        log.info("State not advanced (--keep-state).")

    log.info("Done in %.1f min. %s (%s) — %d real footage sources, %.0f%% "
             "real video.", (time.time() - started) / 60, topic.animal,
             topic.angle_title, len(sources),
             (stats.get("footage_ratio", 0) or 0) * 100)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("main").critical("RUN FAILED: %s", exc, exc_info=True)
        sys.exit(1)

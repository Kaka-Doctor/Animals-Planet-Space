"""Assemble REAL footage + narration into the final 720p MP4 with FFmpeg.

Pipeline (footage-first — no static-image content):
1. Collect the visual plan: branded title card (a few seconds of branding
   only) → the real wildlife footage segments in order → outro card.
2. Each footage segment is trimmed at its planned offset, scaled to fit
   1280x720 (padded with the episode background color), and re-encoded
   with IDENTICAL encoder parameters so the concat is lossless.
3. Narration is concatenated, gently tempo-fitted to the footage length,
   and muxed in with fades.

The final duration is planned BEFORE rendering: narration is synthesized
first, then exactly enough footage is used to cover it (plus ~2s tail),
so we never render video we then have to throw away.
"""
from __future__ import annotations

import logging
import math
import subprocess
from pathlib import Path

log = logging.getLogger("video")

ZMAX = 1.08            # max zoom for Ken Burns (title/outro cards only)
ENCODE = [             # identical for every segment → lossless concat
    "-c:v", "libx264", "-preset", "veryfast",
    "-crf", "23", "-profile:v", "high", "-level", "4.1",
    "-pix_fmt", "yuv420p", "-r", "30",
]
BG_COLOR = "0x08130D"  # deep forest tone for letterboxing


def _run(cmd: list[str], label: str = "ffmpeg") -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"{label} failed ({proc.returncode}):\n{' '.join(cmd[:12])}…\n"
            f"{proc.stderr[-1200:]}"
        )
    return proc.stdout


def probe_duration(path: Path) -> float:
    out = _run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=nw=1:nk=1", str(path),
    ], label="ffprobe").strip()
    return float(out)


def _zoom_expr(frames: int, zoom_in: bool) -> str:
    rate = (ZMAX - 1.0) / max(frames, 1)
    if zoom_in:
        return f"min(1+{rate:.10f}*on,{ZMAX})"
    return f"max({ZMAX}-{rate:.10f}*on,1)"


def _render_card(png: Path, seconds: float, out: Path, *,
                 zoom_in: bool, fade_out: bool) -> None:
    frames = math.ceil(seconds * 30)
    chain = [
        "scale=1280:720:force_original_aspect_ratio=increase:"
        "flags=lanczos,crop=1280:720",
        f"zoompan=z='{_zoom_expr(frames, zoom_in)}'"
        f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
        f":d={frames}:s=1280x720:fps=30",
        "format=yuv420p",
        "fade=t=in:st=0:d=0.6",
    ]
    if fade_out:
        chain.append(f"fade=t=out:st={max(seconds - 0.8, 0):.3f}:d=0.8")
    cmd = [
        "ffmpeg", "-y",
        "-loop", "1", "-framerate", "30", "-i", str(png),
        "-filter_complex", "[0:v]" + ",".join(chain) + "[v]",
        "-map", "[v]", "-frames:v", str(frames),
        *ENCODE, "-an", str(out),
    ]
    _run(cmd, label=f"card {png.name}")


def _render_footage(src: Path, start: float, seconds: float,
                    out: Path, *, fade_out: bool) -> None:
    """Trim a real-footage segment and normalize it to 720p30.

    The source's own audio is dropped (the episode narration rides on top).
    """
    fade_out_st = max(seconds - 0.7, 0)
    chain = [
        "scale=1280:720:force_original_aspect_ratio=decrease:"
        "flags=lanczos",
        f"pad=1280:720:(ow-iw)/2:(oh-ih)/2:color={BG_COLOR}",
        "fps=30",
        "format=yuv420p",
        "fade=t=in:st=0:d=0.35",
    ]
    if fade_out:
        chain.append(f"fade=t=out:st={fade_out_st:.3f}:d=0.7")
    cmd = [
        "ffmpeg", "-y", "-ss", f"{start:.2f}", "-t", f"{seconds:.2f}",
        "-i", str(src),
        "-filter_complex", f"[0:v]{','.join(chain)}[v]",
        "-map", "[v]", *ENCODE, "-an", str(out),
    ]
    _run(cmd, label=f"footage {src.name}@{start:.0f}")


def _concat(files: list[Path], out: Path, kind: str) -> None:
    listfile = out.with_suffix(".concat.txt")
    listfile.write_text(
        "".join(f"file '{f.as_posix()}'\n" for f in files), encoding="utf-8"
    )
    if kind == "video":
        cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i",
               str(listfile), "-c", "copy", str(out)]
    else:  # wav
        cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i",
               str(listfile), "-c:a", "pcm_s16le", str(out)]
    _run(cmd, label=f"concat {kind}")
    listfile.unlink(missing_ok=True)


def extract_frame(src: Path, at: float, out_png: Path) -> Path | None:
    """Grab one frame as PNG (best effort)."""
    try:
        _run([
            "ffmpeg", "-y", "-ss", f"{at:.2f}", "-i", str(src),
            "-frames:v", "1", str(out_png),
        ], label=f"frame {src.name}")
        return out_png if out_png.exists() else None
    except RuntimeError:
        return None


def render_episode(title_png: Path, outro_png: Path,
                   footage_plan: list[tuple[Path, float, float]],
                   narration_wav: Path, out_path: Path,
                   title_seconds: float, outro_seconds: float,
                   work_dir: Path) -> dict:
    """Assemble title card + real footage + outro card + narration → MP4.

    `footage_plan` is a list of (source_path, start_offset, seconds)
    segments ALREADY SIZED to the narration. Returns stats including the
    real-footage share of the runtime (for the quality gate).
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    segments: list[Path] = []

    title_seg = work_dir / "card_title.mp4"
    _render_card(title_png, title_seconds, title_seg,
                 zoom_in=True, fade_out=False)
    segments.append(title_seg)

    n = len(footage_plan)
    for i, (src, start, seconds) in enumerate(footage_plan):
        seg = work_dir / f"seg_{i:02d}.mp4"
        _render_footage(src, start, seconds, seg,
                        fade_out=(i == n - 1))
        segments.append(seg)
        log.info("Footage segment %02d/%02d: %.0fs of %s @ %.0fs",
                 i + 1, n, seconds, src.name[:40], start)

    outro_seg = work_dir / "card_outro.mp4"
    _render_card(outro_png, outro_seconds, outro_seg,
                 zoom_in=False, fade_out=True)
    segments.append(outro_seg)

    silent = work_dir / "video_silent.mp4"
    _concat(segments, silent, "video")
    video_dur = probe_duration(silent)

    audio_dur = probe_duration(narration_wav)
    # Gently compress narration if it overshoots the footage (max +15%,
    # imperceptible for a documentary pace). Guard with a hard trim.
    tempo = 1.0
    if audio_dur > video_dur - 0.2:
        tempo = min(audio_dur / max(video_dur - 0.2, 1.0), 1.15)
        if tempo > 1.001:
            log.warning("Narration %.1fs vs footage %.1fs — applying "
                        "atempo %.3f to fit.", audio_dur, video_dur, tempo)
    af = []
    if tempo > 1.001:
        af.append(f"atempo={tempo:.4f}")
    af += [
        "apad=pad_dur=2",
        "afade=t=in:st=0:d=0.25",
        f"afade=t=out:st={max(video_dur - 1.6, 0):.3f}:d=1.55",
        f"atrim=0:{video_dur:.3f}",
    ]

    out_path.parent.mkdir(parents=True, exist_ok=True)
    _run([
        "ffmpeg", "-y", "-i", str(silent), "-i", str(narration_wav),
        "-map", "0:v", "-map", "1:a",
        "-c:v", "copy",
        "-af", ",".join(af),
        "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
        "-movflags", "+faststart",
        str(out_path),
    ], label="final mux")

    final_dur = probe_duration(out_path)
    size_mb = out_path.stat().st_size / 1e6
    footage_seconds = sum(s for _, _, s in footage_plan)
    ratio = footage_seconds / final_dur if final_dur else 0.0
    log.info("Final video: %.1f min, %.1f MB, %.0f%% real footage → %s",
             final_dur / 60, size_mb, ratio * 100, out_path)
    return {
        "duration": final_dur,
        "size_mb": round(size_mb, 1),
        "footage_seconds": round(footage_seconds, 1),
        "card_seconds": round(title_seconds + outro_seconds, 1),
        "footage_ratio": round(ratio, 3),
        "path": str(out_path),
    }

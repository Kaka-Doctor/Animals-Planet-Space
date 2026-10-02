"""Narration via edge-tts (free Microsoft neural voices), converted to WAV."""
from __future__ import annotations

import asyncio
import logging
import subprocess
from pathlib import Path

log = logging.getLogger("tts")

CONCURRENCY = 3


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {' '.join(cmd)}\n{proc.stderr[-800:]}")


def mp3_to_wav(mp3: Path, wav: Path, pad_seconds: float = 0.4) -> None:
    """Decode to 44.1 kHz mono PCM with a little trailing silence so
    sections breathe naturally when concatenated."""
    _run([
        "ffmpeg", "-y", "-i", str(mp3),
        "-af", f"apad=pad_dur={pad_seconds}",
        "-ar", "44100", "-ac", "1",
        "-c:a", "pcm_s16le",
        str(wav),
    ])


async def _synthesize(text: str, voice: str, rate: str, out_mp3: Path,
                      attempts: int = 3) -> None:
    import edge_tts

    for attempt in range(1, attempts + 1):
        try:
            tts = edge_tts.Communicate(text, voice, rate=rate)
            await tts.save(str(out_mp3))
            if out_mp3.exists() and out_mp3.stat().st_size > 1024:
                return
            raise IOError("suspiciously small audio output")
        except Exception as exc:  # noqa: BLE001
            if attempt == attempts:
                raise
            log.warning("TTS attempt %d/%d failed (%s); retrying",
                        attempt, attempts, exc)
            await asyncio.sleep(2 * attempt)


def synth_sections(narrations: list[str], voice: str, rate: str,
                   work_dir: Path) -> list[Path]:
    """Narrate every section; returns ordered list of padded WAV paths."""
    work_dir.mkdir(parents=True, exist_ok=True)

    async def run_all():
        sem = asyncio.Semaphore(CONCURRENCY)

        async def one(i: int, text: str) -> Path:
            mp3 = work_dir / f"narr_{i:02d}.mp3"
            wav = work_dir / f"narr_{i:02d}.wav"
            async with sem:
                await _synthesize(text, voice, rate, mp3)
            await asyncio.to_thread(mp3_to_wav, mp3, wav)
            return wav

        return await asyncio.gather(*(one(i, t) for i, t in enumerate(narrations)))

    wavs = list(asyncio.run(run_all()))
    total = sum(w.stat().st_size for w in wavs)
    log.info("Narrated %d sections (%.1f MB of audio)", len(wavs), total / 1e6)
    return wavs

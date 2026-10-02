"""Epic thumbnails and branded cards — built from REAL footage frames.

The thumbnail is a real frame of the animal (sampled from the downloaded
footage and scored for sharpness + color), not a generated illustration.
Design: full-bleed frame, cinematic dark gradients top and bottom, the
animal name in huge Anton type with a gold accent bar, the episode hook in
Inter, and the channel handle chip.

The title/outro cards reuse the same design language so the episode opens
and closes with the channel brand.
"""
from __future__ import annotations

import logging
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

from .config import FONTS_DIR

log = logging.getLogger("thumbnail")

W, H = 1280, 720
DEEP = (6, 12, 9)            # deep forest background tone
GOLD = (232, 185, 64)         # accent
WHITE = (255, 255, 255)
TW_WIDTH, TW_HEIGHT = 1280, 720   # YouTube thumb size (16:9)


def _fonts(big: int, small: int) -> tuple[ImageFont.FreeTypeFont,
                                          ImageFont.FreetypeFont]:
    anton = ImageFont.truetype(str(FONTS_DIR / "Anton-Regular.ttf"), big)
    inter = ImageFont.truetype(str(FONTS_DIR / "Inter.ttf"), small)
    return anton, inter


def _score(img: Image.Image) -> float:
    """Sharpness (edge energy) + colorfulness + brightness balance."""
    g = img.convert("L").resize((160, 90))
    edges = g.filter(ImageFilter.FIND_EDGES)
    px = list(edges.getdata())
    sharp = (sum(p * p for p in px) / len(px)) ** 0.5
    rgb = img.convert("RGB").resize((160, 90))
    data = list(rgb.getdata())
    colorfulness = sum(max(c) - min(c) for c in data) / len(data)
    bright = sum(sum(c) / 3 for c in data) / len(data)
    # reward well-exposed frames, punish near-black / blown-out
    exposure = 1.0 - abs(bright - 118) / 118
    return sharp * 1.4 + colorfulness * 0.8 + max(exposure, 0.1) * 40


def best_frame(clip: Path, out_png: Path, samples: int = 7,
               rng: random.Random | None = None) -> Path | None:
    """Sample frames across the clip, keep the most spectacular one."""
    from .video import extract_frame, probe_duration
    rng = rng or random.Random()
    try:
        total = probe_duration(clip)
    except Exception:  # noqa: BLE001
        return None
    if total <= 0:
        return None
    tmp = out_png.parent / (out_png.stem + "_samples")
    tmp.mkdir(parents=True, exist_ok=True)
    best, best_score, best_path = None, -1.0, None
    for i in range(samples):
        at = max(0.5, total * (0.08 + 0.84 * i / max(samples - 1, 1))
                 + rng.uniform(-0.4, 0.4))
        cand = tmp / f"f{i:02d}.png"
        got = extract_frame(clip, at, cand)
        if not got:
            continue
        try:
            img = Image.open(cand)
            score = _score(img)
        except Exception:  # noqa: BLE001
            continue
        if score > best_score:
            best, best_score, best_path = img.copy(), score, cand
    if best is None:
        return None
    out_png.parent.mkdir(parents=True, exist_ok=True)
    best.save(out_png)
    # clean the sampled candidates
    for p in tmp.glob("f*.png"):
        p.unlink(missing_ok=True)
    tmp.rmdir()
    log.info("Best frame: %s (score %.0f)", best_path.name, best_score)
    return out_png


def _cover_fit(img: Image.Image, w: int, h: int) -> Image.Image:
    """Scale + center-crop an image to exactly (w, h)."""
    scale = max(w / img.width, h / img.height)
    nw, nh = round(img.width * scale), round(img.height * scale)
    img = img.resize((nw, nh), Image.LANCZOS)
    x, y = (nw - w) // 2, (nh - h) // 2
    return img.crop((x, y, x + w, y + h))


def _gradient(img: Image.Image, top: float, bottom: float,
              alpha_top: int, alpha_bottom: int) -> Image.Image:
    """Darkening gradient bands (top and bottom) for text legibility."""
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    w, h = img.size
    top_px, bottom_px = int(h * top), int(h * bottom)
    for i in range(top_px):
        a = int(alpha_top * (1 - i / top_px))
        d.line([(0, i), (w, i)], fill=(0, 0, 0, a))
    for i in range(bottom_px):
        a = int(alpha_bottom * (i / bottom_px))
        d.line([(0, h - 1 - i), (w, h - 1 - i)], fill=(0, 0, 0, a))
    return Image.alpha_composite(img.convert("RGBA"), overlay)


def _fit_font(text: str, font_path: Path, max_size: int, max_width: int,
              min_size: int = 40) -> ImageFont.FreeTypeFont:
    size = max_size
    while size > min_size:
        f = ImageFont.truetype(str(font_path), size)
        if f.getbbox(text)[2] <= max_width:
            return f
        size -= 6
    return ImageFont.truetype(str(font_path), min_size)


def make_thumbnail(frame_png: Path, animal: str, hook_text: str,
                   out_jpg: Path) -> Path:
    """Epic YouTube thumbnail: real frame + big type + gold accent."""
    img = Image.open(frame_png).convert("RGB")
    img = _cover_fit(img, TW_WIDTH, TW_HEIGHT)
    img = ImageEnhance.Contrast(img).enhance(1.08)
    img = ImageEnhance.Color(img).enhance(1.15)
    img = _gradient(img, top=0.30, bottom=0.52, alpha_top=110,
                    alpha_bottom=235).convert("RGB")
    d = ImageDraw.Draw(img)

    # animal name — huge Anton, up to 3 lines
    name = animal.upper()
    f_big = _fit_font(name, FONTS_DIR / "Anton-Regular.ttf",
                      max_size=200, max_width=TW_WIDTH - 140)
    # wrap if it does not fit even at min size (very long names)
    lines = [name]
    if f_big.getbbox(name)[2] > TW_WIDTH - 140:
        words = name.split()
        mid = len(words) // 2
        lines = [" ".join(words[:mid]), " ".join(words[mid:])]
        longest = max(lines, key=len)
        f_big = _fit_font(longest,
                          FONTS_DIR / "Anton-Regular.ttf",
                          max_size=170, max_width=TW_WIDTH - 140)

    ascent, descent = f_big.getmetrics()
    line_h = ascent + descent
    name_block = line_h * len(lines)
    baseline_y = int(TW_HEIGHT * 0.94) - name_block - int(TW_HEIGHT * 0.10)
    y = baseline_y
    for line in lines:
        bbox = f_big.getbbox(line)
        x = int((TW_WIDTH - bbox[2]) // 2)
        # hard shadow for pop
        d.text((x + 6, y + 6), line, font=f_big, fill=(0, 0, 0))
        d.text((x, y), line, font=f_big, fill=WHITE)
        y += line_h

    # gold accent bar with the hook text
    f_small = ImageFont.truetype(str(FONTS_DIR / "Inter.ttf"), 44)
    hook = (hook_text or "REAL WILDLIFE").upper()[:38]
    bbox = f_small.getbbox(hook)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    pad_x, pad_y = 26, 16
    bar_w, bar_h = tw + pad_x * 2, th + pad_y * 2
    bx = (TW_WIDTH - bar_w) // 2
    by = baseline_y - bar_h - 26
    d.rounded_rectangle((bx, by, bx + bar_w, by + bar_h), radius=14,
                        fill=GOLD)
    d.text((bx + pad_x - bbox[0], by + pad_y - bbox[1]), hook,
           font=f_small, fill=(24, 18, 4))

    out_jpg.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(out_jpg, "JPEG", quality=93)
    log.info("Thumbnail saved: %s", out_jpg)
    return out_jpg


def make_title_card(frame_png: Path, animal: str, hook_text: str,
                    channel_handle: str, out_png: Path) -> Path:
    """Branded opener: real frame + animal name + channel chip."""
    img = Image.open(frame_png).convert("RGB")
    img = _cover_fit(img, W, H)
    img = ImageEnhance.Color(img).enhance(1.05)
    img = _gradient(img, top=0.34, bottom=0.55, alpha_top=130,
                    alpha_bottom=245).convert("RGB")
    d = ImageDraw.Draw(img)

    f_big = _fit_font(animal.upper(), FONTS_DIR / "Anton-Regular.ttf",
                      max_size=170, max_width=W - 120)
    ascent, descent = f_big.getmetrics()
    line_h = ascent + descent
    name_y = int(H * 0.80) - line_h
    bbox = f_big.getbbox(animal.upper())
    x = (W - bbox[2]) // 2
    d.text((x + 5, name_y + 5), animal.upper(), font=f_big, fill=(0, 0, 0))
    d.text((x, name_y), animal.upper(), font=f_big, fill=WHITE)

    # gold rule
    d.rectangle([(W // 2 - 130, name_y - 34), (W // 2 + 130, name_y - 28)],
                fill=GOLD)

    # channel handle chip at the top
    f_chip = ImageFont.truetype(str(FONTS_DIR / "Inter.ttf"), 36)
    label = channel_handle.lstrip("@")
    bbox = f_chip.getbbox(label)
    cw = bbox[2] + 56
    cx, cy = (W - cw) // 2, int(H * 0.10)
    d.rounded_rectangle((cx, cy, cx + cw, cy + 60), radius=30,
                        outline=GOLD, width=3)
    d.text((cx + 28, cy + 12), label, font=f_chip, fill=GOLD)

    # small hook line
    if hook_text:
        f_hook = ImageFont.truetype(str(FONTS_DIR / "Inter.ttf"), 34)
        bbox = f_hook.getbbox(hook_text.upper())
        hx = (W - bbox[2]) // 2
        d.text((hx, name_y + line_h + 18), hook_text.upper(),
               font=f_hook, fill=(226, 226, 216))

    out_png.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_png)
    log.info("Title card saved: %s", out_png)
    return out_png


def make_outro_card(frame_png: Path, channel_handle: str,
                    out_png: Path) -> Path:
    """Branded closer: real frame + subscribe line."""
    img = Image.open(frame_png).convert("RGB")
    img = _cover_fit(img, W, H)
    img = ImageEnhance.Color(img).enhance(1.02)
    img = _gradient(img, top=0.30, bottom=0.55, alpha_top=150,
                    alpha_bottom=250).convert("RGB")
    d = ImageDraw.Draw(img)

    f_big = ImageFont.truetype(str(FONTS_DIR / "Anton-Regular.ttf"), 96)
    text = "STAY WILD"
    bbox = f_big.getbbox(text)
    x, y = (W - bbox[2]) // 2, int(H * 0.36)
    d.text((x + 4, y + 4), text, font=f_big, fill=(0, 0, 0))
    d.text((x, y), text, font=f_big, fill=WHITE)

    f_sub = ImageFont.truetype(str(FONTS_DIR / "Inter.ttf"), 40)
    sub = f"New wildlife documentaries twice a day — {channel_handle}"
    bbox = f_sub.getbbox(sub)
    sx, sy = (W - bbox[2]) // 2, y + 150
    d.text((sx, sy), sub, font=f_sub, fill=GOLD)

    out_png.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_png)
    log.info("Outro card saved: %s", out_png)
    return out_png

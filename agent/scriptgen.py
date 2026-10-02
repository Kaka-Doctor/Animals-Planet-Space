"""Generate the wildlife-documentary episode script.

Priority:
1. Google Gemini (free tier key) — writes the energetic documentary
   narration about the chosen animal + angle, SIZED TO THE FOOTAGE WE
   ACTUALLY COLLECTED (words target derived from footage seconds).
2. Built-in footage-walk template — last resort when every model is
   storming. Honest narration over the real footage: no invented facts,
   just the camera-observing voice that wildlife channels do so well.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

import requests

from .animals import EpisodeTopic
from .config import Settings

log = logging.getLogger("scriptgen")

VALID_TYPES = {"intro", "fact", "take", "outro"}

MAX_DRAFTS = 3

SCHEMA_HINT = """{
  "hook": "explosive spoken first line that stops the scroll (15-25 words)",
  "thumbnail_text": "3-6 PUNCHY words for the thumbnail (ALL CAPS energy)",
  "title": "clickable YouTube title (max 95 chars, no clickbait lies)",
  "description": "2-3 sentence episode summary for YouTube (no hashtags)",
  "sections": [
    {"type": "intro", "title": "Welcome to the Wild",      "narration": "...", "on_screen": ["short phrase", "short phrase"]},
    {"type": "fact",  "title": "Habitat of a Ruler",       "narration": "...", "on_screen": ["...", "...", "..."]},
    {"type": "take",  "title": "Why This Matters",         "narration": "...", "on_screen": ["...", "..."]},
    {"type": "fact",  "title": "The Hunt Begins",          "narration": "...", "on_screen": ["...", "...", "..."]},
    {"type": "outro", "title": "Stay Wild",                "narration": "...", "on_screen": []}
  ]
}"""


def build_prompt(topic: EpisodeTopic, settings: Settings,
                 target_words: int, footage_seconds: float) -> str:
    return f"""You are the energetic narrator of "Animals Planet Space" — a YouTube
wildlife channel. The viewer is watching REAL documentary footage of the
{topic.animal}, filmed in {topic.region.replace('Arctic', 'the Arctic').replace('Ocean', 'the ocean').replace('Rivers', 'rivers and wetlands').replace('Islands', 'islands')}, and your voice carries them through it.

THIS EPISODE'S ANIMAL: {topic.animal}
EPISODE ANGLE: {topic.angle_title} — {topic.angle_brief}
FOOTAGE LENGTH: about {footage_seconds / 60:.1f} minutes of real video plays
under your narration (footage first, script second — never describe things
the camera cannot show).

Write the episode script with {target_words}-{target_words + 250} words of total
narration. CRITICAL LENGTH RULE: NEVER write fewer than {target_words} words —
if unsure, add another fact section rather than going short. Structure:

1. HOOK — one breath-stopping opener: the most jaw-dropping thing about the
   {topic.animal}, spoken like a movie trailer.
2. FACT SECTIONS (4-6) — each one a themed beat about this episode's angle:
   where it lives, how it hunts or eats, how it raises its young, how it
   survives its predators, its most insane adaptations. Concrete and vivid:
   sizes, speeds, lifespans, distances — ONLY widely documented numbers you
   are certain about; if unsure of a number, describe it qualitatively
   instead ("faster than a racehorse", "as tall as a door").
3. TAKE sections (1-3, optional) — a quick "why this matters" beat: the
   animal's role in its ecosystem, its conservation status, or what its
   story teaches us about nature. One sharp insight, not a lecture.
4. OUTRO — "stay wild" energy + invite to subscribe for a new animal
   documentary twice a day.

HARD RULES:
- FACTS: only widely documented, textbook-level facts about the {topic.animal}.
  Never invent numbers, behaviors, or discoveries. When unsure, go
  qualitative — vivid beats precise-but-wrong.
- LENGTH (critical): total narration between {target_words} and
  {target_words + 250} words. Rough targets: intro 70-100; each fact
  section 120-180; each take 60-100; outro 45-70. At most 10 sections.
- Tone: energetic wildlife documentary narrator — vivid verbs, present
  tense, wonder and respect for the animal. Think "the fastest thing on
  six legs launches its attack" not "it is fast". NO profanity, no gore
  glorification, no anthropomorphic cliches ("Mr. Whiskers").
- "narration" is read aloud by a neural voice: plain speakable English. No
  markdown, no emojis, no parentheses, no URLs.
- Refer to what is on screen where it helps: "watch how it moves", "keep
  your eyes on the tall grass" — the footage under your words is real.
- "title" of fact sections: a short cinematic label (3-6 words), e.g.
  "Born for the Night", "The Ambush Unfolds".
- "on_screen": 2-4 ultra-short phrases (max ~7 words), title case, no
  ending punctuation.
- "thumbnail_text": 3-6 words selling THIS animal + angle, ALL-CAPS energy
  (e.g. "KING OF THE SAVANNA!").
- "title" (YouTube): under 95 chars, energetic but truthful, feature the
  animal name in CAPS and the angle, e.g. "LION: The Ultimate Hunter of
  the Savanna | Wildlife Documentary".

Return ONLY valid JSON matching exactly this shape:
{SCHEMA_HINT}"""


# ---------------------------------------------------------------------------
# LLM backends
# ---------------------------------------------------------------------------

def _call_gemini_model(prompt: str, settings: Settings, model: str,
                       max_attempts: int = 5) -> str:
    url = ("https://generativelanguage.googleapis.com/v1beta/models/"
           f"{model}:generateContent")
    headers = {
        "x-goog-api-key": settings.gemini_api_key,
        "Content-Type": "application/json",
    }
    body: dict[str, Any] = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.9,
            "maxOutputTokens": 16384,
        },
    }
    if "2.5" in model or "3" in model:
        body["generationConfig"]["thinkingConfig"] = {"thinkingBudget": 2048}

    retryable = {429, 500, 502, 503, 504}
    for attempt in range(1, max_attempts + 1):
        try:
            r = requests.post(url, headers=headers, json=body, timeout=180)
        except requests.RequestException as exc:
            log.warning("Gemini attempt %d/%d network error: %s",
                        attempt, max_attempts, exc)
        else:
            if r.status_code in retryable:
                log.warning("Gemini attempt %d/%d got HTTP %d (transient)",
                            attempt, max_attempts, r.status_code)
            elif (r.status_code == 400
                  and "thinkingConfig" in body["generationConfig"]):
                body["generationConfig"].pop("thinkingConfig", None)
                log.warning("Gemini rejected thinkingConfig; retrying without it")
                continue
            else:
                r.raise_for_status()
                data = r.json()
                cand = data["candidates"][0]
                finish = cand.get("finishReason", "")
                text = "".join(
                    p.get("text", "") for p in cand["content"]["parts"])
                if not text.strip():
                    raise ValueError(f"empty completion (finishReason={finish})")
                if finish == "MAX_TOKENS":
                    log.warning("Gemini hit MAX_TOKENS; output may be truncated.")
                return text
        if attempt < max_attempts:
            wait = min(8 * 2 ** (attempt - 1), 45)
            log.warning("Retrying %s in %ds…", model, wait)
            time.sleep(wait)
    raise RuntimeError(f"{model} failed after {max_attempts} attempts")


def _call_gemini(prompt: str, settings: Settings) -> tuple[str, str]:
    chain: list[str] = [settings.gemini_model]
    chain += [m for m in settings.gemini_fallback_models
              if m and m != settings.gemini_model]
    last_exc: Exception | None = None
    for i, model in enumerate(chain):
        attempts = 5 if i == 0 else 3
        try:
            return _call_gemini_model(prompt, settings, model,
                                      max_attempts=attempts), model
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            log.warning("Gemini model %s unavailable: %s", model, exc)
            if i < len(chain) - 1:
                log.warning("Switching to fallback model: %s", chain[i + 1])
    raise RuntimeError(
        f"all Gemini models failed ({', '.join(chain)}): {last_exc}")


# ---------------------------------------------------------------------------
# Parsing / validation
# ---------------------------------------------------------------------------

def _extract_json(raw: str) -> dict:
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    start = text.find("{")
    if start == -1:
        raise ValueError("no JSON object found")
    depth = 0
    for i, ch in enumerate(text[start:], start=start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start: i + 1])
    raise ValueError("unbalanced JSON")


@dataclass
class Section:
    type: str
    title: str
    narration: str
    on_screen: list[str] = field(default_factory=list)


@dataclass
class Script:
    hook: str = ""
    thumbnail_text: str = ""
    title: str = ""
    description: str = ""
    sections: list[Section] = field(default_factory=list)
    source: str = "unknown"

    @property
    def word_count(self) -> int:
        return sum(len(s.narration.split()) for s in self.sections) \
            + len(self.hook.split())

    def validate(self) -> None:
        cleaned: list[Section] = []
        for sec in self.sections:
            sec.type = str(sec.type).lower().strip()
            if sec.type not in VALID_TYPES:
                sec.type = "fact"
            sec.title = (sec.title or "").strip()[:80]
            sec.narration = (sec.narration or "").strip()
            sec.on_screen = [str(p).strip()[:60]
                             for p in sec.on_screen if str(p).strip()][:4]
            if sec.narration:
                cleaned.append(sec)
        if not cleaned:
            raise ValueError("script has no usable sections")
        if cleaned[0].type != "intro":
            cleaned[0].type = "intro"
        if cleaned[-1].type != "outro":
            cleaned.append(Section("outro", "Stay Wild", "", []))
        self.sections = cleaned
        self.hook = (self.hook or "").strip()
        self.thumbnail_text = (self.thumbnail_text or "").strip()[:40]
        self.title = (self.title or "").strip()[:100]
        self.description = (self.description or "").strip()


def _parse_script(raw: str, source: str) -> Script:
    data = _extract_json(raw)
    sections = [
        Section(
            type=s.get("type", "fact"),
            title=s.get("title", ""),
            narration=s.get("narration", ""),
            on_screen=s.get("on_screen", []) or [],
        )
        for s in data.get("sections", [])
    ]
    script = Script(
        hook=data.get("hook", ""),
        thumbnail_text=data.get("thumbnail_text", ""),
        title=data.get("title", ""),
        description=data.get("description", ""),
        sections=sections,
        source=source,
    )
    script.validate()
    return script


# ---------------------------------------------------------------------------
# Footage-walk fallback (total AI outage — zero invented facts)
# ---------------------------------------------------------------------------

def _template_script(topic: EpisodeTopic, min_words: int) -> Script:
    """Honest narration over the real footage — never invents facts."""
    a = topic.animal
    sections = [
        Section("intro", "Welcome to the Wild",
                f"Stop scrolling — what you are about to see is one hundred "
                f"percent real. This is the {a}, filmed where it actually "
                f"lives, doing what it actually does. Today's episode dives "
                f"into {topic.angle_brief}. Keep your eyes on the screen, "
                f"because nature does not do second takes.",
                ["One hundred percent real", f"The {a} up close",
                 "Stay with it"]),
        Section("fact", "Eyes on the Wild",
                f"Watch how the {a} moves through its world. Every step, "
                f"every glance, every pause is a decision refined over "
                f"countless generations. This is not a staged scene — this "
                f"is survival, exactly as it happens in the wild. The "
                f"camera simply waits, and the animal does the rest.",
                ["Real behavior, no staging", "Survival in the open",
                 "Watch closely"]),
        Section("fact", "A World of Instinct",
                f"Look at the detail: the muscle under the skin, the ears "
                f"tracking every sound, the eyes locked onto something we "
                f"cannot see yet. Instinct is running this show — the same "
                f"ancient programming that has carried the {a} through "
                f"ice ages, droughts, and everything nature has thrown at "
                f"it.",
                ["Ancient instincts at work", "Every sense engaged",
                 "Nature's programming"]),
        Section("fact", "The Moment of Truth",
                f"Now lean in, because moments like this are exactly why "
                f"wildlife filmmakers sit in hiding for days. Whatever "
                f"happens next, the {a} will handle it the only way it "
                f"knows how — perfectly, because for the wild there is no "
                f"other option. This is the part of nature documentaries "
                f"you never forget.",
                ["Days of waiting for seconds", "The wild answers",
                 "Unforgettable moments"]),
        Section("fact", "More Than a Moment",
                f"And this is just one window into the life of the {a}. "
                f"Somewhere out there, right now, this same scene is "
                f"playing out beyond every camera. That is the quiet "
                f"miracle of the natural world — it runs whether we watch "
                f"or not. We are just lucky enough to catch it on film.",
                ["The wild never pauses", "One window of millions",
                 "Lucky witnesses"]),
        Section("outro", "Stay Wild",
                f"If real footage of the world's most incredible animals "
                f"speaks to you, hit subscribe — Animals Planet Space "
                f"releases a brand new wildlife documentary twice a day, "
                f"every day. Different animals, different corners of the "
                f"planet, always real. Stay wild.",
                ["New wildlife twice daily", "Subscribe and stay wild"]),
    ]
    while sum(len(s.narration.split()) for s in sections) < min_words:
        filler = [
            ("fact", "Still Watching",
             f"Keep watching — the {a} is not done yet. In the wild, the "
             f"quiet moments are the setup, and the payoff arrives when you "
             f"least expect it. Patience is the first skill every survivor "
             f"masters, and the camera has to master it too.",
             ["Patience is survival", "The payoff is coming"]),
            ("fact", "Every Detail Tells a Story",
             f"Look again at the picture in front of you. The light, the "
             f"terrain, the direction the {a} chooses — nothing about this "
             f"moment is random. Reading the wild is a language, and every "
             f"creature on screen is fluent in it.",
             ["Nothing here is random", "The wild speaks"]),
            ("fact", "The Wild Never Sleeps",
             f"And remember — this scene does not pause when the camera "
             f"looks away. Somewhere out there, right now, the {a} is "
             f"living this exact story beyond every lens. That is what "
             f"makes real footage so special: it is a window, not a show.",
             ["A window, not a show", "Always happening"]),
        ][len(sections) % 3]
        sections.insert(-1, Section(*filler))
    title = (f"{a.upper()}: Up Close in the Wild | Real Wildlife Footage")
    return Script(
        hook=f"This is the {a} like you have rarely seen it — no staging, "
             f"no tricks, just the wild.",
        thumbnail_text=f"REAL {a.upper()}!",
        title=title,
        description=(f"Real wildlife footage of the {a}, narrated and "
                     f"explored — {topic.angle_brief}. A new animal "
                     f"documentary from Animals Planet Space."),
        sections=sections,
        source="template",
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def generate_script(topic: EpisodeTopic, settings: Settings,
                    footage_seconds: float) -> Script:
    """Write the episode script sized to the footage we actually have."""
    target_words = max(
        settings.min_template_words,
        int((footage_seconds - settings.title_card_seconds
             - settings.outro_card_seconds) * settings.words_per_second))
    if settings.gemini_api_key:
        prompt = build_prompt(topic, settings, target_words, footage_seconds)
        try:
            raw, model = _call_gemini(prompt, settings)
            script = _parse_script(raw, f"gemini:{model}")
            log.info("Gemini script via %s (%d words, %d sections)",
                     model, script.word_count, len(script.sections))
            return script
        except Exception as exc:  # noqa: BLE001
            log.error("Gemini script generation failed: %s — falling back "
                      "to the honest footage-walk template.", exc)
    log.info("Using footage-walk template (no AI backend).")
    return _template_script(topic, max(target_words,
                                        settings.min_template_words))

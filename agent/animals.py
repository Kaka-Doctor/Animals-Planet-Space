"""Animal catalog + episode selection — the BIG 15.

This channel is dedicated to the world's most iconic BIG animals: the
African Big Five (lion, leopard, elephant, rhinoceros, cape buffalo) plus
ten more giants viewers never tire of (giraffe, hippopotamus, nile
crocodile, cheetah, bengal tiger, polar bear, grizzly bear, gray wolf,
gorilla, orca). ONLY these 15 are ever featured — but they repeat
endlessly with deliberate variety:

  * a fresh ANGLE each time (hunting, family life, night life, battles…)
  * a rotating ENVIRONMENT/SETTING (Serengeti, Kruger, night hunt…)
  * DIFFERENT source videos each time (state.json → used_sources ledger
    blocks every URL already used for that animal)
  * different titles/descriptions (the script prompt is told this is
    episode #N, gets the past titles, and must write a distinctly new one)

SELECTION IS POPULARITY-DRIVEN: animals whose past episodes earned the
most views + likes on the channel get featured MORE often (weighted
random — see agent/popularity.py), so the most-watched animals naturally
dominate the schedule while a short cooldown keeps any animal from
appearing twice within days.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .config import Settings

# THE BIG 15: Big Five + ten more giants. (name, region, footage hints)
CATALOG: list[tuple[str, str, str]] = [
    # --- The African Big Five -------------------------------------------------
    ("Lion", "Africa", "lion pride savanna roar"),
    ("Leopard", "Africa", "leopard tree climbing ambush"),
    ("African Elephant", "Africa", "elephant herd matriarch"),
    ("Rhinoceros", "Africa", "rhino white black horn"),
    ("Cape Buffalo", "Africa", "buffalo herd oxpecker"),
    # --- Ten more giants ------------------------------------------------------
    ("Giraffe", "Africa", "giraffe tower necking"),
    ("Hippopotamus", "Africa", "hippo river pool"),
    ("Nile Crocodile", "Africa", "crocodile ambush hunt"),
    ("Cheetah", "Africa", "cheetah speed sprint"),
    ("Bengal Tiger", "Asia", "tiger jungle ambush"),
    ("Polar Bear", "Arctic", "polar bear ice arctic"),
    ("Grizzly Bear", "Americas", "grizzly salmon fishing"),
    ("Gray Wolf", "Americas", "wolf pack hunt"),
    ("Gorilla", "Africa", "gorilla silverback"),
    ("Orca", "Ocean", "orca killer whale pod"),
]

# Title-matching aliases — COMMON names: a source title matches the
# animal if it contains ANY of these (single words anywhere, multi-word
# as an adjacent phrase). Stemmed at match time ("Rhinos" → "rhino").
ALIASES: dict[str, tuple[str, ...]] = {
    "Lion": ("lion",),
    "Leopard": ("leopard",),
    "African Elephant": ("elephant",),
    "Rhinoceros": ("rhinoceros", "rhino"),
    "Cape Buffalo": ("buffalo",),
    "Giraffe": ("giraffe",),
    "Hippopotamus": ("hippopotamus", "hippo"),
    "Nile Crocodile": ("crocodile", "croc"),
    "Cheetah": ("cheetah",),
    "Bengal Tiger": ("tiger",),
    "Polar Bear": ("polar bear",),
    "Grizzly Bear": ("grizzly", "brown bear", "kodiak"),
    "Gray Wolf": ("wolf",),
    "Gorilla": ("gorilla", "silverback"),
    "Orca": ("orca", "killer whale"),
}

# Scientific names — also accepted for title matching (a large share of
# Wikimedia Commons wildlife files are titled only "Panthera pardus" /
# "Ursus arctos" / "Orcinus orca"). Kept separate from ALIASES because
# vehicle-number patterns ("Leopard 2A7") only ever use COMMON names, so
# the sci words must stay exempt from that check ("Panthera leo 01" is
# just a zoo's file numbering, not a tank).
SCI_ALIASES: dict[str, tuple[str, ...]] = {
    "Lion": ("panthera leo",),
    "Leopard": ("panthera pardus",),
    "African Elephant": ("loxodonta",),
    "Rhinoceros": ("ceratotherium", "diceros"),
    "Cape Buffalo": ("syncerus",),
    "Giraffe": ("giraffa",),
    "Nile Crocodile": ("crocodylus niloticus",),
    "Cheetah": ("acinonyx",),
    "Bengal Tiger": ("panthera tigris",),
    "Polar Bear": ("ursus maritimus",),
    "Grizzly Bear": ("ursus arctos",),
    "Gray Wolf": ("canis lupus",),
    "Orca": ("orcinus",),
}

# Rotating settings so the SAME animal keeps getting FRESH episodes:
# different environments, different situations, different footage hunts.
ENVIRONMENTS: dict[str, list[str]] = {
    "Lion": [
        "the Serengeti plains", "Kruger National Park", "the Masai Mara",
        "the Okavango Delta", "a waterhole at dusk", "night on the savanna",
    ],
    "Leopard": [
        "the Kruger bushveld", "the Sabi Sands riverine forest",
        "a rocky outcrop lair", "the tree canopy", "a night prowl",
        "the forest edge at dawn",
    ],
    "African Elephant": [
        "Amboseli's dust plains", "the Okavango wetlands",
        "a crowded waterhole", "a herd on migration",
        "a river crossing at dusk", "the herd by moonlight",
    ],
    "Rhinoceros": [
        "the open savanna", "a muddy wallow", "the thickets",
        "night grazing under stars", "a waterhole showdown",
        "the grasslands at dawn",
    ],
    "Cape Buffalo": [
        "the open savanna", "a dusty herd march", "the river's edge",
        "night at the waterhole", "the reeds and mud",
        "a breeding herd on the move",
    ],
    "Giraffe": [
        "the acacia savanna", "Etosha's open plains", "a riverine forest",
        "the dry-season waterhole", "sunrise on the plains",
        "a tower at full stretch",
    ],
    "Hippopotamus": [
        "a crowded river pool", "night grazing on land",
        "the Zambezi shallows", "a territorial bull battle",
        "a mud wallow at noon", "the river at dusk",
    ],
    "Nile Crocodile": [
        "the river's edge", "a crossing-point ambush",
        "the sun-baked sandbank", "night eyes on the water",
        "the swamp shallows", "a basking wallow",
    ],
    "Cheetah": [
        "the open grassland", "the Serengeti plains",
        "a termite-mound lookout", "cubs hidden in the grass",
        "a full-speed sprint", "the savanna at golden hour",
    ],
    "Bengal Tiger": [
        "the mangrove swamps", "a jungle waterhole",
        "the tall grass territory", "a night prowl",
        "the monsoon forest", "a river swim",
    ],
    "Polar Bear": [
        "the drifting sea ice", "the Arctic coastline",
        "a hunt on the ice", "the summer tundra",
        "a mother and cubs", "the frozen north at dusk",
    ],
    "Grizzly Bear": [
        "a salmon river run", "the alpine meadows",
        "the temperate rainforest", "a berry hillside in autumn",
        "a mother and cubs", "the mountains at dawn",
    ],
    "Gray Wolf": [
        "a snowy winter hunt", "the northern forest",
        "the pack's meeting grounds", "a hunt in deep snow",
        "the wilderness at dusk", "howling under the moon",
    ],
    "Gorilla": [
        "the mountain mist forest", "a silverback's showdown",
        "the family group at rest", "a forest clearing",
        "a nesting site at dusk", "the volcanic slopes",
    ],
    "Orca": [
        "a hunting pod on the move", "the icy fjords",
        "a coordinated hunt", "the open ocean swell",
        "a family pod at play", "the coast at sunset",
    ],
}

# Every episode explores an ANGLE — rotates so the same animal never
# repeats "in an exact way", and titles stay varied across episodes.
ANGLES: list[tuple[str, str]] = [
    ("hunting and feeding", "how this animal hunts and eats"),
    ("family life", "mating, raising the young, and the bonds that keep them alive"),
    ("survival adaptations", "the adaptations that let it survive where others cannot"),
    ("habitat and territory", "where it lives and how it rules its territory"),
    ("secret behaviors", "behaviors most people have never seen"),
    ("predators and prey", "the enemies it fears and the prey it takes"),
    ("speed and power", "the physical limits of its speed and strength"),
    ("intelligence and communication", "how it thinks, learns, and talks to its kind"),
    ("epic battles and rivals", "its most intense showdowns with rivals and enemies"),
    ("night life", "what it does under the cover of darkness"),
    ("anatomy up close", "the body design and weapons that make it a marvel of evolution"),
    ("conservation and the future", "the fight to protect it and what its future looks like"),
]


@dataclass
class EpisodeTopic:
    animal: str
    hints: str
    region: str
    angle_title: str       # e.g. "hunting and feeding"
    angle_brief: str       # e.g. "how this animal hunts and eats"
    environment: str = ""  # rotating setting, e.g. "the Serengeti plains"
    episode_number: int = 1  # which episode this is for THIS animal


def _covered_names(data: dict, cooldown_days: int) -> set[str]:
    """Animals featured within the cooldown window (case-insensitive)."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(cooldown_days, 1))
    out: set[str] = set()
    for entry in data.get("covered_animals", []):
        try:
            when = datetime.fromisoformat(
                str(entry.get("date", "")).replace("Z", "+00:00"))
        except ValueError:
            when = None
        if when is None or when >= cutoff:
            out.add(str(entry.get("animal", "")).strip().lower())
    return out


def _features_of(data: dict, animal: str) -> list[dict]:
    """Past episodes of one animal (oldest first)."""
    key = animal.strip().lower()
    return [e for e in data.get("covered_animals", [])
            if str(e.get("animal", "")).strip().lower() == key]


def pick_topic(data: dict, settings: Settings,
               rng: random.Random | None = None,
               exclude: set[str] | None = None,
               scores: dict[str, float] | None = None) -> EpisodeTopic:
    """Choose (animal, angle, environment) for this episode.

    POPULARITY-DRIVEN: `scores` maps animal (lowercase) → channel
    performance score (views + weighted likes from our own uploads —
    see agent/popularity.py). Eligible animals are drawn by weighted
    random, so the most watched/liked animals are created more than
    others while everything still rotates. Animals with no history get
    the median score, so unseen giants are still explored.

    An animal is only eligible again after ANIMAL_COOLDOWN_DAYS (short —
    repeats are the point, but never twice within days). `exclude`
    (lowercase names) lets a single run skip animals whose footage hunt
    already came up dry this slot.
    """
    rng = rng or random.Random()
    scores = scores or {}
    covered = _covered_names(data, settings.animal_cooldown_days)
    skip = covered | {x.lower() for x in (exclude or set())}

    eligible = [(n, reg, h) for (n, reg, h) in CATALOG
                if n.lower() not in skip]
    if eligible:
        # weighted random over channel performance: score ratio to the
        # median, damped and clipped — the most watched/liked animals are
        # created MORE than others (up to ~4x) while untested giants
        # still get explored at weight 1.0.
        known = sorted(v for v in scores.values() if v > 0)
        base = known[len(known) // 2] if known else 0.0

        def _weight(animal_name: str) -> float:
            s = scores.get(animal_name.lower())
            if not s or s <= 0 or base <= 0:
                return 1.0                      # untested → explore
            return min(4.0, max(0.5, (s / base) ** 0.75))

        weights = [_weight(n) for (n, _r, _h) in eligible]
        name, region, hints = rng.choices(eligible, weights=weights, k=1)[0]
        picked_by = "popularity-weighted"
    else:
        # everything on cooldown (or excluded) → least-recently-featured
        # animal that is not excluded, fresh angle
        hard_skip = {x.lower() for x in (exclude or set())}
        ledger = {e.get("animal", "").lower(): e
                  for e in data.get("covered_animals", [])}
        order = sorted(
            (t for t in CATALOG if t[0].lower() not in hard_skip),
            key=lambda t: str(ledger.get(t[0].lower(), {}).get("date", "0000")))
        if not order:
            order = CATALOG          # nothing left — serve any giant
        name, region, hints = order[0]
        picked_by = "least-recently-featured"

    # variety for REPEATS: fresh angle + rotating environment + episode #
    feats = _features_of(data, name)
    episode_number = len(feats) + 1
    used_angles = {str(e.get("angle", "")) for e in feats}
    angle_pool = [(a, b) for (a, b) in ANGLES if a not in used_angles] \
        or ANGLES
    angle_title, angle_brief = angle_pool[rng.randrange(len(angle_pool))]
    envs = ENVIRONMENTS.get(name, ["the wild"])
    environment = envs[(episode_number - 1) % len(envs)]

    log_hint = (f"[{region}] episode #{episode_number} for this animal — "
                f"setting: {environment}")
    print(f"TOPIC: {name} — {log_hint} — angle: {angle_title} "
          f"({picked_by})")
    return EpisodeTopic(animal=name, hints=hints, region=region,
                        angle_title=angle_title, angle_brief=angle_brief,
                        environment=environment,
                        episode_number=episode_number)

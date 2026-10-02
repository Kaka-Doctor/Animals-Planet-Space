"""Animal catalog + episode selection.

The channel travels the world: animals are grouped by region and selection
rotates through the regions (Africa → Asia → Arctic → Ocean → Americas →
Islands → Europe → Australia...) so consecutive episodes feature wildlife
from different parts of the planet. An animal is only re-featured after
ANIMAL_COOLDOWN_DAYS, and every re-feature gets a fresh ANGLE (hunting,
family life, survival adaptations...) so nothing repeats "in an exact way".

State ledger (state.json → covered_animals) powers the dedup.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .config import Settings

# (name, region, extra search hints — synonyms that widen footage matching)
CATALOG: list[tuple[str, str, str]] = [
    # --- Africa ---------------------------------------------------------------
    ("Lion", "Africa", "lion savanna big cat"),
    ("African Elephant", "Africa", "elephant herd savanna"),
    ("Giraffe", "Africa", "giraffe savanna"),
    ("Cheetah", "Africa", "cheetah running hunt"),
    ("Leopard", "Africa", "leopard tree climbing"),
    ("Rhinoceros", "Africa", "rhino white black"),
    ("Hippopotamus", "Africa", "hippo river"),
    ("Nile Crocodile", "Africa", "crocodile hunt"),
    ("Spotted Hyena", "Africa", "hyena clan"),
    ("Meerkat", "Africa", "meerkat desert"),
    ("Gorilla", "Africa", "mountain gorilla"),
    ("Chimpanzee", "Africa", "chimp troop forest"),
    ("Zebra", "Africa", "zebra herd migration"),
    ("Wildebeest", "Africa", "wildebeest migration river crossing"),
    ("Okapi", "Africa", "okapi rainforest"),
    ("African Wild Dog", "Africa", "painted dog hunting pack"),
    ("Flamingo", "Africa", "flamingo flock lake"),
    ("Secretary Bird", "Africa", "secretary bird snake hunt"),
    ("Aardvark", "Africa", "aardvark nocturnal"),
    ("Bonobo", "Africa", "bonobo"),
    # --- Asia ---------------------------------------------------------------
    ("Bengal Tiger", "Asia", "tiger hunt jungle"),
    ("Giant Panda", "Asia", "panda bamboo"),
    ("Snow Leopard", "Asia", "snow leopard mountain"),
    ("Orangutan", "Asia", "orangutan canopy"),
    ("Asian Elephant", "Asia", "elephant forest asia"),
    ("Red Panda", "Asia", "red panda climbing"),
    ("King Cobra", "Asia", "king cobra"),
    ("Komodo Dragon", "Asia", "komodo monitor lizard"),
    ("Bengal Slow Loris", "Asia", "loris nocturnal"),
    ("Indian Rhino", "Asia", "one horned rhinoceros"),
    ("Golden Eagle", "Asia", "golden eagle hunting"),
    ("Crane", "Asia", "red crowned crane dance"),
    ("Tarsier", "Asia", "tarsier philippines"),
    ("Pangolin", "Asia", "pangolin scales"),
    ("Bengal Tiger of the Mangroves", "Asia", "tiger swimming sundarbans"),
    ("Yak", "Asia", "yak himalaya"),
    ("Binturong", "Asia", "binturong bearcat"),
    ("Gharial", "Asia", "gharial crocodile fish"),
    # --- Arctic & Cold Regions ---------------------------------------------------------------
    ("Polar Bear", "Arctic", "polar bear ice"),
    ("Arctic Fox", "Arctic", "arctic fox snow"),
    ("Reindeer", "Arctic", "caribou herd tundra"),
    ("Walrus", "Arctic", "walrus haul out"),
    ("Snowy Owl", "Arctic", "snowy owl"),
    ("Musk Ox", "Arctic", "musk ox herd"),
    ("Beluga Whale", "Arctic", "beluga white whale"),
    ("Narwhal", "Arctic", "narwhal tusk"),
    ("Wolverine", "Arctic", "wolverine glutton"),
    ("Lemming", "Arctic", "lemming tundra"),
    # --- Ocean ---------------------------------------------------------------
    ("Great White Shark", "Ocean", "white shark breach"),
    ("Orca", "Ocean", "killer whale orca pod"),
    ("Humpback Whale", "Ocean", "humpback breaching"),
    ("Blue Whale", "Ocean", "blue whale largest"),
    ("Dolphin", "Ocean", "bottlenose dolphin pod"),
    ("Octopus", "Ocean", "octopus camouflage"),
    ("Sea Turtle", "Ocean", "sea turtle nesting"),
    ("Manta Ray", "Ocean", "manta ray glide"),
    ("Sailfish", "Ocean", "sailfish fast fish"),
    ("Hammerhead Shark", "Ocean", "hammerhead school"),
    ("Cuttlefish", "Ocean", "cuttlefish color change"),
    ("Anglerfish", "Ocean", "anglerfish deep sea"),
    ("Mantis Shrimp", "Ocean", "mantis shrimp punch"),
    ("Moray Eel", "Ocean", "moray eel reef"),
    ("Harbor Seal", "Ocean", "seal colony"),
    ("Sperm Whale", "Ocean", "sperm whale diving"),
    # --- The Americas ---------------------------------------------------------------
    ("Grizzly Bear", "Americas", "grizzly salmon fishing"),
    ("Gray Wolf", "Americas", "wolf pack"),
    ("Bald Eagle", "Americas", "bald eagle fishing"),
    ("Jaguar", "Americas", "jaguar hunting"),
    ("Cougar", "Americas", "mountain lion puma"),
    ("Bison", "Americas", "bison herd prairie"),
    ("Moose", "Americas", "moose forest"),
    ("Alligator", "Americas", "alligator swamp"),
    ("Anaconda", "Americas", "green anaconda"),
    ("Harpy Eagle", "Americas", "harpy eagle rainforest"),
    ("Poison Dart Frog", "Americas", "poison dart frog colorful"),
    ("Sloth", "Americas", "sloth slow"),
    ("Scarlet Macaw", "Americas", "scarlet macaw parrot"),
    ("Capuchin Monkey", "Americas", "capuchin intelligence"),
    ("Ocelot", "Americas", "ocelot spotted"),
    ("Prairie Dog", "Americas", "prairie dog town"),
    ("Beaver", "Americas", "beaver dam building"),
    ("Condor", "Americas", "california condor vulture"),
    ("Manatee", "Americas", "manatee sea cow"),
    ("Raccoon", "Americas", "raccoon night clever"),
    # --- Australia & Oceania ---------------------------------------------------------------
    ("Kangaroo", "Australia", "kangaroo hopping"),
    ("Koala", "Australia", "koala eucalyptus"),
    ("Platypus", "Australia", "platypus swimming"),
    ("Tasmanian Devil", "Australia", "tasmanian devil"),
    ("Cassowary", "Australia", "cassowary dangerous bird"),
    ("Dingo", "Australia", "dingo wild dog"),
    ("Saltwater Crocodile", "Australia", "saltie crocodile"),
    ("Wombat", "Australia", "wombat burrow"),
    ("Emu", "Australia", "emu running"),
    ("Echidna", "Australia", "echidna anteater"),
    ("Wallaby", "Australia", "wallaby"),
    ("Frilled Lizard", "Australia", "frilled neck lizard"),
    # --- Islands & Madagascar ---------------------------------------------------------------
    ("Ring-tailed Lemur", "Islands", "lemur madagascar"),
    ("Chameleon", "Islands", "chameleon tongue"),
    ("Fossa", "Islands", "fossa madagascar predator"),
    ("Aye-aye", "Islands", "aye-aye night"),
    ("Galapagos Tortoise", "Islands", "giant tortoise galapagos"),
    ("Marine Iguana", "Islands", "marine iguana swimming"),
    ("Kiwi Bird", "Islands", "kiwi nocturnal new zealand"),
    ("Tuatara", "Islands", "tuatara reptile"),
    ("Dodo's Cousins: Nicobar Pigeon", "Islands", "nicobar pigeon"),
    ("Bird of Paradise", "Islands", "bird of paradise dance"),
    # --- Europe ---------------------------------------------------------------
    ("Brown Bear", "Europe", "brown bear forest"),
    ("Lynx", "Europe", "lynx wild cat"),
    ("Red Deer", "Europe", "red deer stag"),
    ("Eurasian Beaver", "Europe", "beaver europe"),
    ("White Stork", "Europe", "white stork migration"),
    ("Wild Boar", "Europe", "wild boar sounder"),
    ("European Bison", "Europe", "wisent bison"),
    ("Iberian Lynx", "Europe", "iberian lynx rare"),
    ("Chamois", "Europe", "chamois alpine"),
    ("Puffin", "Europe", "puffin colony cliffs"),
    # --- Rivers & Wetlands ---------------------------------------------------------------
    ("Botos: Amazon River Dolphin", "Rivers", "pink river dolphin"),
    ("Giant River Otter", "Rivers", "giant otter amazon"),
    ("Electric Eel", "Rivers", "electric eel shock"),
    ("Piranha", "Rivers", "piranha feeding"),
    ("Arapaima", "Rivers", "arapaima giant fish"),
    ("Bearded Dragon", "Rivers", "bearded dragon"),
    # --- Reptiles & Amphibians ---------------------------------------------------------------
    ("Chameleon Master of Disguise", "Reptiles", "chameleon color"),
    ("Gecko", "Reptiles", "gecko wall climbing"),
    ("Rattlesnake", "Reptiles", "rattlesnake strike"),
    ("Monitor Lizard", "Reptiles", "monitor lizard"),
    ("Axolotl", "Reptiles", "axolotl regeneration"),
    ("Basilisk Lizard", "Reptiles", "jesus christ lizard running water"),
    # --- Birds ---------------------------------------------------------------
    ("Peregrine Falcon", "Birds", "peregrine fastest dive"),
    ("Hummingbird", "Birds", "hummingbird hovering"),
    ("Ostrich", "Birds", "ostrich running"),
    ("Raven", "Birds", "raven intelligence"),
    ("Peacock", "Birds", "peacock display tail"),
    ("Vulture", "Birds", "vulture soaring"),
    ("Albatross", "Birds", "albatross wingspan"),
    ("Penguin", "Birds", "emperor penguin colony"),
    ("Owl", "Birds", "great horned owl night"),
    ("Flamingo Courtship", "Birds", "flamingo dance"),
    # --- Insects & Arachnids ---------------------------------------------------------------
    ("Leafcutter Ant", "Insects", "leafcutter ant colony"),
    ("Orb Weaver Spider", "Insects", "spider web building"),
    ("Praying Mantis", "Insects", "mantis strike"),
    ("Bumblebee", "Insects", "bumblebee pollination"),
    ("Dragonfly", "Insects", "dragonfly flight"),
    ("Firefly", "Insects", "firefly bioluminescence"),
    ("Atlas Moth", "Insects", "atlas moth largest"),
    ("Bullet Ant", "Insects", "bullet ant"),
]

REGION_ORDER = ["Africa", "Asia", "Arctic", "Ocean", "Americas",
                "Australia", "Islands", "Europe", "Rivers", "Reptiles",
                "Birds", "Insects"]

# Every episode explores an ANGLE — rotates so re-featured animals never
# repeat in an exact way, and titles stay varied.
ANGLES: list[tuple[str, str]] = [
    ("hunting and feeding", "how this animal hunts and eats"),
    ("family life", "mating, raising the young, and the bonds that keep them alive"),
    ("survival adaptations", "the adaptations that let it survive where others cannot"),
    ("habitat and territory", "where it lives and how it rules its territory"),
    ("secret behaviors", "behaviors most people have never seen"),
    ("predators and prey", "the enemies it fears and the prey it takes"),
    ("speed and power", "the physical limits of its speed and strength"),
    ("intelligence and communication", "how it thinks, learns, and talks to its kind"),
]


@dataclass
class EpisodeTopic:
    animal: str
    hints: str
    region: str
    angle_title: str       # e.g. "hunting and feeding"
    angle_brief: str       # e.g. "how this animal hunts and eats"


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


def _recent_regions(data: dict, take: int = 3) -> list[str]:
    """Regions of the last few episodes — used to rotate away from them."""
    out = []
    for entry in reversed(data.get("covered_animals", [])):
        r = str(entry.get("region", "")).strip()
        if r and r not in out:
            out.append(r)
        if len(out) >= take:
            break
    return out


def pick_topic(data: dict, settings: Settings,
               rng: random.Random | None = None) -> EpisodeTopic:
    """Choose (animal, angle) for this episode.

    Priority: never-covered animals first, rotating the world regions; when
    everything is on cooldown, the least-recently-featured animal returns
    with a FRESH angle.
    """
    rng = rng or random.Random()
    covered = _covered_names(data, settings.animal_cooldown_days)

    # rotate regions: skip the regions of the last episodes
    recent = set(_recent_regions(data))
    region_cycle = ([r for r in REGION_ORDER if r not in recent]
                    + [r for r in REGION_ORDER if r in recent])

    for region in region_cycle:
        pool = [(n, h) for (n, reg, h) in CATALOG
                if reg == region and n.lower() not in covered]
        if pool:
            name, hints = rng.choice(pool)
            idx = next(i for i, (n, _, _) in enumerate(CATALOG)
                       if n == name)
            break
    else:
        # everything covered → least-recently-featured animal, fresh angle
        ledger = {e.get("animal", "").lower(): e
                  for e in data.get("covered_animals", [])}
        order = sorted(
            CATALOG,
            key=lambda t: str(ledger.get(t[0].lower(), {}).get("date", "0000")))
        name, reg, hints = order[0]
        idx = next(i for i, (n, _, _) in enumerate(CATALOG) if n == name)

    # angle rotation: prefer an angle this animal has NOT had recently
    used_angles: set[str] = set()
    for e in data.get("covered_animals", []):
        if str(e.get("animal", "")).strip().lower() == name.lower():
            used_angles.add(str(e.get("angle", "")))
    angle_pool = [(a, b) for (a, b) in ANGLES if a not in used_angles] or ANGLES
    angle_title, angle_brief = angle_pool[len(data.get("covered_animals", []))
                                          % len(angle_pool)]

    log_hint = f"[{CATALOG[idx][1]}] #{idx + 1} of {len(CATALOG)}"
    print(f"TOPIC: {name} — {log_hint} — angle: {angle_title}")
    return EpisodeTopic(animal=name, hints=hints,
                        region=CATALOG[idx][1],
                        angle_title=angle_title, angle_brief=angle_brief)

# Animals Planet Space — Autonomous Wildlife Video Agent

Posts **two wildlife documentary videos every day** (every ~12 hours) to
https://www.youtube.com/@AnimalsPlanetSpace — fully autonomous, running on
GitHub Actions.

Each episode features **ONE animal from a different part of the world**
(Africa → Asia → the Arctic → the oceans → the Americas → Australia →
islands → Europe, rotating), explored through a rotating angle (hunting &
feeding, family life, survival adaptations, habitat, secret behaviors…).

The visuals are **REAL moving wildlife footage — never static image
slides**, collected legally from three license-clean sources:

| Source | License | Why |
|---|---|---|
| YouTube (Creative Commons search) | CC-BY 3.0 | popular videos — search is ordered by view count. **Note:** YouTube often bot-blocks video downloads from datacenter IPs; when that happens the agent automatically falls through to the sources below (the search starts working fully once `YT_REFRESH_TOKEN` is set and the runner IP is clean). |
| Wikimedia Commons | CC0 / CC-BY / CC-BY-SA / PD | huge trove of real wildlife clips — the reliable backbone |
| Internet Archive | Public domain / CC | classic wildlife films, long footage, ranked by downloads |

Every source actually used is **attributed in the video description**
(title, author, link, license, segments) — that satisfies the CC-BY
attribution requirement. An energetic male documentary narrator
(edge-tts) carries the episode; the script is written by Gemini and sized
to the footage actually collected.

## Repository layout

```
agent/
  animals.py     animal catalog (146, world regions) + selection & dedup
  footage.py     real-footage collector (YouTube CC / Commons / Archive)
  scriptgen.py   Gemini documentary script, sized to the footage
  tts.py         energetic male narration (edge-tts)
  video.py       footage-first assembly (title card + real clips + outro)
  thumbnail.py   epic thumbnails & cards from REAL footage frames
  youtube.py     upload (resumable, chunk-safe) + thumbnails + description
  state.py       12-hour cadence guard + animal ledger (no repeats)
  qa.py          pre-upload quality gate (duration, REAL-footage ratio…)
  main.py        orchestrator
scripts/
  get_refresh_token.py   one-time OAuth consent for the channel
  set_thumb.py           re-apply thumbnails (after channel verification)
.github/workflows/
  daily_animal.yml       2× daily: 06:15 & 18:15 UTC (+19:45 backup slot)
  set_thumbnail.yml      manual thumbnail utility
```

## One-time setup (repo Secrets → Settings → Secrets and variables → Actions)

| Secret | Value |
|---|---|
| `GEMINI_API_KEY` | a Google AI Studio key (script writing) |
| `YT_CLIENT_ID` | the OAuth client ID (same app as the other channels) |
| `YT_CLIENT_SECRET` | the OAuth client secret |
| `YT_REFRESH_TOKEN` | from the OAuth consent below — **for the account that owns THIS channel** |

### Getting `YT_REFRESH_TOKEN`

```bash
YT_CLIENT_ID=... YT_CLIENT_SECRET=... python scripts/get_refresh_token.py
```

Open the printed URL, sign in with the Google account that owns
**Animals Planet Space**, approve, and paste the redirected
`http://localhost:8765/?...` URL back. Put the printed refresh token in the
`YT_REFRESH_TOKEN` secret. Until it is set, scheduled runs exit fast
without rendering (no wasted quota); after it is set, the 06:15 / 18:15 UTC
slots post automatically.

### Verify the channel (unlocks custom thumbnails)

Custom thumbnails need a phone-verified channel:
https://www.youtube.com/verify — once verified, run the
**Set Thumbnail** workflow to retrofit existing episodes. New episodes set
their thumbnail automatically at upload time.

## How it stays fresh (no repeats)

- **Animal ledger** (`state.json → covered_animals`): an animal returns
  only after a 60-day cooldown, and with a **different angle** each time.
- **Region rotation**: consecutive episodes feature wildlife from
  different parts of the planet.
- **12-hour cadence guard** (`min_hours_between_posts = 10.5`): exactly
  two posts per day, tolerant of GitHub cron delays. A backup slot at
  19:45 UTC self-skips when the evening episode is already live.

## Quality gate (an episode must EARN its upload)

- narration ≥ ~550 words (AI) or ~350 (honest footage-walk fallback)
- duration 3–10 minutes
- **≥ 85% of the runtime is real footage** (title/outro cards are the only
  non-footage seconds)
- every footage source attributed in the description (license compliance)
- non-empty title/description + real-frame thumbnail ≥ 20 KB

If anything fails: no upload, no state advance — the next slot retries.
A missing day is better than a bad episode.

## Legal notes

- Only CC-BY / CC0 / CC-BY-SA / public-domain footage is downloaded, and
  attribution is embedded in every description.
- YouTube's monetization "reused content" policy is mitigated by the
  original narration, editing, chaptering, and branding — but if you plan
  to monetize, review YouTube's policy yourself.
- Videos from social networks (Instagram/TikTok/X) are deliberately NOT
  used: their terms forbid downloading, and almost none carry an open
  license. "Public" ≠ "legal to reuse".

## Manual runs

Actions tab → **Daily Animal Documentary Video** → Run workflow:

- `no_upload` — render only (dry run)
- `force` — bypass the 12-hour guard
- `animal` — override the animal (e.g. "Lion")

Or locally: `python -m agent.main --no-upload --animal "Lion"`.

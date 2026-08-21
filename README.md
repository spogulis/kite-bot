# KiteBot 🪁

A self-hosted Telegram bot that watches the wind forecast for your kitesurf
spots and tells you when it's worth rigging up.

- **Daily digest** — posted every morning to every subscribed chat (your kite
  group, your own DM, a specific group topic). Each chat can filter which
  spots its digest covers (`/myspots`), so users who only care about some
  spots subscribe in a DM and pick theirs. Set `post_when_no_wind: false` to
  skip mornings with nothing rideable.
- **On demand** — anyone can tap the `/menu` buttons or type `/check`, in the
  group or in a DM.
- **Configurable spots** — added by sharing a Telegram location, managed with
  admin buttons (`/manage`: delete, toggle allowed wind directions) or by
  editing a file.
- **Latvian UI** — all chat texts are Latvian; wind directions are written out
  in words (e.g. "ziemeļrietumu vējš"). Command names stay English.
- **Free weather data** — [Open-Meteo](https://open-meteo.com), no API key needed.

## How "rideable" is decided

For each spot the bot scans the hourly forecast for the next `forecast_days`
days. An hour counts as rideable when all of these hold:

1. mean wind (10 m) is between the spot's `min_wind` and `max_wind`
   (in your configured `wind_unit` — m/s, knots, km/h or mph),
2. wind direction falls in one of the spot's `good_directions` sectors
   (empty = any direction),
3. the hour is between `day_start_hour` and `day_end_hour` local time.

At least `min_window_hours` consecutive rideable hours form a **window**, and
windows are what gets reported:

```
🪁 Daily kite digest · next 3 days

Podersdorf
✅ Sat 22.08 · 11:00–17:00 · 7–10 m/s (gusts 13) · NW

Rust
— nothing rideable
```

## Setup

Requires Python 3.10+.

1. **Create the bot**: message [@BotFather](https://t.me/BotFather) in
   Telegram → `/newbot` → pick a name and username → copy the token.

2. **Install & configure**:

   ```bash
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   cp .env.example .env                    # then paste your token into .env
   cp config.example.yaml config.yaml     # local settings, not tracked by git
   ```

3. **Set your spots**: edit `config.yaml` (schedule, thresholds, timezone) —
   it's local to each machine and never touched by `git pull`, so server-side
   edits survive updates. Spots live in `data/spots.yaml`, which is created with two
   example spots (Neusiedler See) on first run — replace them with your own,
   either in the file or later in-chat with `/addspot`.

4. **Try it without Telegram** (prints the digest to stdout):

   ```bash
   .venv/bin/python bot.py --dry-run
   ```

5. **Run the bot**:

   ```bash
   .venv/bin/python bot.py
   ```

6. **Hook up your group**: add the bot to the group, then type `/subscribe`
   there (you must be a group admin). For personal updates, DM the bot
   `/subscribe`. That's it — no chat ids to copy around, no privacy-mode
   changes needed.

## Commands

Work the same in groups and in private chats.

| Command | What it does |
| --- | --- |
| `/prognoze` | Forecast for all spots — a pure planning view |
| `/marsruts` | Day-route planner: a button wizard (day → wind preference: stronger/lighter/any → max total driving distance) that keeps editing one message, then becomes the route. In a DM you can also share your location so the plan accounts for the drive from you (and clips today's windows to your arrival); the DM result has a "📤 Nosūtīt grupai" button |
| `/home`, `/quiver 12 9 7`, `/weight 85`, `/profile` | Personal profile: saved home location (offered as a "🏠 No manām mājām" button in the route wizard), kite sizes and weight — with quiver+weight set, routes recommend which kites to take (size ≈ 2.2 × kg / kn) |
| `/menu` (or `/start`) | Buttons: check all spots or a single one with one tap |
| `/check [spot]` | Forecast check as a text command |
| `/spots` | List configured spots and their settings |
| `/addspot Name` | Add a spot the easy way: after sending this, share a location (📎 → Location); the bot then shows direction-toggle buttons |
| `/addspot "Name" lat lon [min=6] [max=20] [dirs=290-20,110-170] [cell=land\|sea\|nearest]` | Full form for power users (min/max in your `wind_unit`) |
| `/manage` | Admin buttons per spot: 🧭 toggle allowed wind directions, 🗑 delete (with confirmation) |
| `/delspot <name>` | Remove a spot as a text command |
| `/testdigest` | Preview today's daily digest in this chat, incl. whether it would actually be sent (admins) |
| `/woorider <name>` | Search WOO Sports for a rider and add them via buttons (admins) |
| `/records` | The crew's jump-record leaderboard (open to everyone) |
| `/riders` | Manage tracked riders: records per rider, 🔗 merge WOO+Surfr, 🗑 delete (admins) |
| `/subscribe` / `/unsubscribe` | Enable/disable the daily digest in the current chat (works inside forum topics too) |
| `/myspots` | Toggle buttons: which spots this chat's daily digest covers (none selected = all) |
| `/id` | Show chat id, user id and subscription status |
| `/help` | Command overview |

**Location-based adding in groups**: Telegram's privacy mode hides non-command
messages (including shared locations) from bots in groups. Either do the
`/addspot Name` + location flow in a DM with the bot, or make the bot a group
admin so it can see the location message.

**Who may do what:**

- Forecast commands (`/check`, `/menu`, `/spots`, `/records`) — everyone.
- Managing a chat's daily digest (`/subscribe`, `/myspots`, `/unsubscribe`) —
  in a **private chat**, always the person themselves (it only affects their
  own DM); in a **group**, only group admins.
- Adding spots and tuning their wind directions — **any member, inside the
  group** (the group is the trust boundary); in private chats only admins
  (see below).
- Deleting spots and managing riders/records — users listed in
  `admin_user_ids` anywhere, and group admins within their group. In private
  chats this is open **only while `admin_user_ids` is empty** (the
  bootstrap default so you can set things up before knowing your user id).

⚠️ **Set `admin_user_ids` before inviting others**: with it empty, *any*
Telegram user who discovers the bot can DM it and edit your spots and riders.
Get your id with `/id`, put it in config.yaml (`admin_user_ids: [12345678]`),
restart — friends keep their personal DM digests, but editing is locked down.

## Configuration reference

`config.yaml` (restart after editing):

| Key | Default | Meaning |
| --- | --- | --- |
| `timezone` | `Europe/Riga` | Timezone for the daily post schedule |
| `wind_unit` | `ms` | Unit for thresholds, messages and forecasts: `kn`, `ms`, `kmh`, `mph` |
| `daily_post_time` | `"07:00"` | When the digest is posted |
| `daily_greeting` | `"Labrīt, kaiteri!"` | First line of the daily post; empty string disables |
| `forecast_days` | `3` | Days ahead to scan (1–7) |
| `post_when_no_wind` | `true` | Post even when nothing is rideable; `false` = stay silent those days |
| `min_window_hours` | `2` | Minimum consecutive rideable hours |
| `wind_band` | per unit (3 m/s / 6 kn) | Max wind spread reported as one line; bigger changes split the day into separate lines (different kite sizes). `0` disables |
| `default_model` | `best_match` | Weather model: `best`, `gfs` (Windguru's GFS 13 km table), `icon`, `ecmwf`, `harmonie` (2 km, ~ Windguru's HARM-DK column; northern/central Europe, best for 1–2 days ahead); per-spot override via `model=` |
| `default_cell` | `land` | Forecast grid cell for newly added spots: `sea` reads the wind over the water — set it if your spots are coastal (the example config does); `land` suits small lakes/lagoons |
| `day_start_hour` / `day_end_hour` | `8` / `20` | Daylight window (spot-local time) |
| `admin_user_ids` | `[]` | Who may change spots/subscriptions (see above) |

`data/spots.yaml` — one entry per spot: `name`, `lat`, `lon`, `min_wind`,
`max_wind` (in your `wind_unit`; legacy `min_knots`/`max_knots` fields are
read as knots and converted), `good_directions` (list of `[from, to]` sectors in degrees,
wind-FROM, may wrap through north like `[290, 20]`), `cell_selection`
(`land`/`sea`/`nearest` — try `sea` for coastal spots).

Wind directions are where the wind blows *from*: `0` = N, `90` = E, `180` = S,
`270` = W. A typical safe setup allows onshore/side-shore sectors and excludes
offshore.

## Rider recap (WOO Sports & Surfr)

The daily digest can include a "Vakardienas varoņi" section with yesterday's
ridden distance and best jump for tracked riders, pulled from the WOO Sports
and Surfr public leaderboards (the same unofficial APIs their leaderboard
sites use — best-effort: if a provider changes or blocks its API, its riders
silently drop out of the section and the forecast still posts). Admins add
riders with `/woorider <name>` (WOO, searches the last 30 days) or
`/surfrider <name>` (Surfr, searches this week then this month) and pick from
button choices; `/riders` lists and removes them, and its 🔗 button merges two
entries that are the same person on both apps — merged riders get one recap
line with the best value per metric (both jump readings shown when the apps
disagree by ≥0.3 m) and a single record. The bot stores each rider's
best jump in `data/riders.json` (seeded from the search window's best at add
time — correct it with `/setrecord <name> <m>`) and celebrates new records in
the digest with the delta. Riders appear only if their sessions are publicly
visible on the respective leaderboards.

## Running 24/7

The bot must run on some always-on machine (home server, Raspberry Pi, small
VPS). With Docker:

```bash
docker compose up -d --build
```

Config stays in `./config.yaml`, mutable state (spots, subscriptions) in
`./data/`. Without Docker, any process manager works (`systemd`, `launchd`,
`tmux` for testing) — the bot is a single long-running process, no open ports
needed (it uses Telegram long-polling).

## Development

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

## Accuracy notes

Open-Meteo serves model forecasts for the 10 m mean wind of a grid cell. The
model is configurable: `default_model` in config.yaml, or per spot via
`/addspot Name model=gfs` — `gfs` is the model Windguru's primary forecast
runs on, so use it if you calibrate against Windguru; `best` is Open-Meteo's
regional blend; `icon` and `ecmwf` are also available. Local effects —
thermal winds, lake/sea breezes, venturi — are not fully captured, so treat
the digest as a "worth watching" signal rather than gospel, and tune
`min_wind` per spot against what you actually experience.

Fetching is resilient: quick retries for interactive commands, up to ~10
minutes of retries for the scheduled morning digest, and
[MET Norway](https://api.met.no/) as an independent fallback provider if
Open-Meteo stays down. Weather data by
[Open-Meteo.com](https://open-meteo.com/) (free for non-commercial use,
CC BY 4.0) and MET Norway (NLOD/CC BY 4.0).

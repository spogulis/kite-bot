"""WOO Sports client (unofficial).

Uses the same anonymous leaderboard API as https://leaderboards.woosports.com —
undocumented, so treat every call as best-effort: on any failure the caller
should skip WOO content rather than break the bot. Anonymous access covers
leaderboards only (no private profiles), so a rider appears here exactly when
their sessions are public on the WOO leaderboards.
"""
from __future__ import annotations

import asyncio
import logging
import unicodedata

import httpx

API_URL = "https://prod.api.woosports.com/v2/leaderboards/"  # trailing slash avoids a 307

# Anonymous token shipped with the public leaderboard site. May rotate —
# override with `woo_token` in config.yaml if WOO requests start failing.
DEFAULT_TOKEN = (
    "cbb1c29372536ad03c725af741cda7282767416ddca7aabbc50d3ed4f2c2"
    "ac81a38f26930ec7baf0a3d4c92f490da97f44107989b9e134c351c95335f139e8b0"
)

PAGE_SIZE = 50
# Per feature. A quiet day is ~150-300 riders worldwide, but 2026-09-27 had 651
# and the old 12-page cap dropped the lowest 51 jumps — the tail is exactly
# where a tracked rider on a small day sits, so leave generous headroom.
MAX_DAY_PAGES = 40
MAX_SEARCH_PAGES = 120  # ~6000 riders; a 30-day window is ~5-6k in season

log = logging.getLogger(__name__)


class WooError(Exception):
    pass


def normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFD", text or "")
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()


async def _page(client: httpx.AsyncClient, token: str, feature: str, game_type: str,
                offset: int, start: "int | None" = None, end: "int | None" = None) -> dict:
    params = {"offset": offset, "limit": PAGE_SIZE, "feature": feature, "game_type": game_type}
    if start is not None:
        params["start_date"] = start
    if end is not None:
        params["end_date"] = end
    try:
        response = await client.get(API_URL, params=params,
                                    headers={"Authorization": token}, timeout=20)
        response.raise_for_status()
        data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise WooError(f"WOO request failed: {exc}") from exc
    if data.get("status") != "ok":
        raise WooError(f"WOO returned an error: {str(data)[:200]}")
    return data


async def _scan(client, token, feature, game_type, start, end, max_pages, on_item) -> None:
    first = await _page(client, token, feature, game_type, 0, start, end)
    size = int(first.get("size") or 0)
    for item in first.get("items") or []:
        on_item(item)
    offsets = list(range(PAGE_SIZE, min(size, max_pages * PAGE_SIZE), PAGE_SIZE))
    if size > max_pages * PAGE_SIZE:
        log.info("WOO %s scan truncated at %d of %d entries", feature, max_pages * PAGE_SIZE, size)
    for batch_start in range(0, len(offsets), 8):
        batch = offsets[batch_start:batch_start + 8]
        pages = await asyncio.gather(
            *(_page(client, token, feature, game_type, o, start, end) for o in batch))
        for page in pages:
            for item in page.get("items") or []:
                on_item(item)


def _item_user(item: dict) -> tuple:
    user = item.get("user") or {}
    name = f"{user.get('first_name') or ''} {user.get('last_name') or ''}".strip()
    return str(user.get("id") or ""), name


async def day_stats(token: str, start: int, end: int, rider_ids: set) -> dict:
    """{woo_id: {"distance_m", "height_m", "spot_name", "kite"}} for riders
    active in the window.

    Only the jump leaderboard says where and on what: each entry is the rider's
    best jump of the day, with its spot and the kite as set in the app
    ("12m Duotone Rebel SLS"). The distance leaderboard is a day total with
    neither, so a rider who logged no jump has no spot or kite. There is no
    spot country — an entry's country_code is the rider's own.
    """
    stats: dict = {}

    def collect(field):
        def on_item(item):
            woo_id, _ = _item_user(item)
            if woo_id in rider_ids:
                entry = stats.setdefault(woo_id, {})
                entry[field] = float(item.get("score") or 0)
                for key, raw in (("spot_name", item.get("spot")), ("kite", item.get("gear"))):
                    value = str(raw or "").strip()
                    if value and not entry.get(key):
                        entry[key] = value
        return on_item

    async with httpx.AsyncClient() as client:
        await _scan(client, token, "total_distance", "freeride", start, end,
                    MAX_DAY_PAGES, collect("distance_m"))
        await _scan(client, token, "height", "big_air", start, end,
                    MAX_DAY_PAGES, collect("height_m"))
    return stats


async def find_riders(token: str, query: str, start: int, end: int, limit: int = 6) -> list:
    """Search riders by name in the window's height leaderboard.

    Returns [{"woo_id", "name", "best_height_m"}]; the score doubles as the
    rider's best jump in the window, used to seed their record.
    """
    needle = normalize(query)
    if not needle:
        return []
    found: list = []

    def on_item(item):
        woo_id, name = _item_user(item)
        if len(found) < limit and woo_id and needle in normalize(name):
            found.append({"rider_id": woo_id, "name": name,
                          "best_height_m": float(item.get("score") or 0)})

    async with httpx.AsyncClient() as client:
        await _scan(client, token, "height", "big_air", start, end,
                    MAX_SEARCH_PAGES, on_item)
    return found


PROVIDER_LABELS = {"woo": "WOO", "surfr": "Surfr"}

# Recap metric markers. The three are easy to confuse once the words are gone,
# so they read as directions: up for how high, sideways for how far the jump
# carried, a road for the distance ridden over the whole session. Units
# disambiguate too — only the ridden distance is in km.
HEIGHT_ICON = "⬆️"
JUMP_DISTANCE_ICON = "↔️"
RIDDEN_ICON = "🛣️"
KITE_ICON = "🪁"
SPOT_ICON = "📍"


def _sources(rider: dict, stats: dict) -> dict:
    """{provider: that app's day stats} for the apps that saw the rider ride."""
    sources = {}
    for provider, rider_id in (rider.get("ids") or {}).items():
        day = stats.get(f"{provider}:{rider_id}")
        if day:
            sources[provider] = day
    return sources


def _best_jump_first(sources: dict) -> list:
    """The day's per-app readings, highest jump first — the kite and spot shown
    on a line describe that jump."""
    return sorted(sources.values(), key=lambda day: -(day.get("height_m") or 0))


def _jump_spot(sources: dict) -> tuple:
    """(spot, country) of the best jump that names one, else ("", "")."""
    for day in _best_jump_first(sources):
        name = (day.get("spot_name") or "").strip()
        if name:
            return name, (day.get("spot_country") or "").strip()
    return "", ""


def _match_spot(name: str, spot_names) -> "str | None":
    """The configured spot an app's spot name refers to, if any.

    Matched loosely (diacritics folded, either side may contain the other)
    because each app spells spots its own way — WOO's "Engures Mols" is the
    configured "Engure".
    """
    needle = normalize(name)
    for spot in spot_names or ():
        known = normalize(spot)
        if known and (needle == known or needle in known or known in needle):
            return spot
    return None


def _spot_label(sources: dict, known_spots) -> str:
    """Where the rider rode, as the app names it ("" if no app says).

    A spot outside the configured list also gets its country code: a rider on
    holiday logs a session in Portugal on a morning the digest found no wind
    anywhere, which reads as a broken forecast unless the line says where it
    happened. Only Surfr reports a country.
    """
    name, country = _jump_spot(sources)
    if not name or not country or _match_spot(name, known_spots) is not None:
        return name
    return f"{name} ({country})"


def fooled_the_forecast(riders: list, stats: dict, had_window: dict) -> bool:
    """True when someone rode where that morning's digest promised no wind.

    had_window: {configured spot name: whether the digest showed a window
    there}, as the daily job recorded it. A rider counts when their best jump
    was at one of those spots and it had no window; a rider no app places
    anywhere counts only if no spot had one. Riders abroad never count — the
    forecast was never about their spot.
    """
    if not had_window:
        return False
    for rider in riders:
        sources = _sources(rider, stats)
        if not sources:
            continue
        name, _ = _jump_spot(sources)
        if not name:
            if not any(had_window.values()):
                return True
            continue
        spot = _match_spot(name, had_window)
        if spot is not None and not had_window[spot]:
            return True
    return False


def summarize(riders: list, stats: dict, known_spots=()) -> tuple:
    """Latvian recap lines for riders who rode, plus riders with updated records.

    riders: [{"name", "record_height_m", "ids": {provider: id}}]; stats: merged
    day_stats() results keyed "provider:rider_id". A person tracked by both
    apps gets one line — the best value counts, and when the apps disagree on
    the jump by 0.3 m or more, both readings are shown. Only WOO reports
    distance ridden and only Surfr reports jump distance, so those two parts
    come from whichever app has them. Metrics are marked with icons rather
    than named, always in the order height, jump distance, distance ridden,
    then the kite (WOO only) and the spot of the best jump. known_spots are
    the configured spot names, used to tell a home spot from one abroad.
    Lines are ranked by jump height, highest first, so the section reads as a
    leaderboard; riders whose app reported no jump sink to the bottom in the
    order they are configured. updated_riders keeps the input order, since it
    is written straight back to riders.json.
    Returns (lines, updated_riders, records_changed).
    """
    def num(value: float) -> str:
        return f"{value:.1f}".replace(".", ",")

    ranked: list = []
    updated: list = []
    changed = False
    for rider in riders:
        entry = dict(rider)
        entry["ids"] = dict(rider.get("ids") or {})
        sources = _sources(entry, stats)
        if sources:
            parts = []
            heights = {p: d["height_m"] for p, d in sources.items() if d.get("height_m")}
            height = max(heights.values()) if heights else 0
            if height:
                text = f"{HEIGHT_ICON} {num(height)} m"
                if len(heights) > 1 and max(heights.values()) - min(heights.values()) >= 0.3:
                    both = " / ".join(
                        f"{PROVIDER_LABELS.get(p, p)} {num(h)}"
                        for p, h in sorted(heights.items(), key=lambda kv: -kv[1]))
                    text += f" ({both})"
                parts.append(text)
            jump_distance = max((d.get("jump_distance_m") or 0) for d in sources.values())
            if jump_distance:
                parts.append(f"{JUMP_DISTANCE_ICON} {num(jump_distance)} m")
            distance = max((d.get("distance_m") or 0) for d in sources.values())
            if distance:
                parts.append(f"{RIDDEN_ICON} {num(distance / 1000)} km")
            kite = next((d["kite"] for d in _best_jump_first(sources) if d.get("kite")), "")
            if kite:
                parts.append(f"{KITE_ICON} {kite}")
            spot = _spot_label(sources, known_spots)
            if spot:
                parts.append(f"{SPOT_ICON} {spot}")
            record = float(rider.get("record_height_m") or 0)
            if height > record:
                # Telegram offers no colored text; the red marker + caps is
                # the loudest formatting a message can carry.
                if record > 0:
                    parts.append(f"🔴 JAUNS REKORDS (+{num(height - record)} m)!")
                else:
                    parts.append("🔴 PIRMAIS REKORDS!")
                entry["record_height_m"] = height
                changed = True
            if parts:
                ranked.append((height, f"🏄 {rider.get('name', '?')} — " + " · ".join(parts)))
        updated.append(entry)
    ranked.sort(key=lambda pair: -pair[0])  # stable, so ties keep config order
    return [line for _, line in ranked], updated, changed

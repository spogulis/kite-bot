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
MAX_DAY_PAGES = 12      # per feature; a single day is ~200-500 riders worldwide
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
    """{woo_id: {"distance_m": float, "height_m": float}} for riders active in the window."""
    stats: dict = {}

    def collect(field):
        def on_item(item):
            woo_id, _ = _item_user(item)
            if woo_id in rider_ids:
                stats.setdefault(woo_id, {})[field] = float(item.get("score") or 0)
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


def summarize(riders: list, stats: dict) -> tuple:
    """Latvian recap lines for riders who rode, plus riders with updated records.

    riders: [{"name", "record_height_m", "ids": {provider: id}}]; stats: merged
    day_stats() results keyed "provider:rider_id". A person tracked by both
    apps gets one line — the best value counts, and when the apps disagree on
    the jump by 0.3 m or more, both readings are shown.
    Returns (lines, updated_riders, records_changed).
    """
    def num(value: float) -> str:
        return f"{value:.1f}".replace(".", ",")

    lines: list = []
    updated: list = []
    changed = False
    for rider in riders:
        entry = dict(rider)
        entry["ids"] = dict(rider.get("ids") or {})
        sources = {}
        for provider, rider_id in entry["ids"].items():
            day = stats.get(f"{provider}:{rider_id}")
            if day:
                sources[provider] = day
        if sources:
            parts = []
            distance = max((d.get("distance_m") or 0) for d in sources.values())
            if distance:
                parts.append(f"{num(distance / 1000)} km")
            heights = {p: d["height_m"] for p, d in sources.items() if d.get("height_m")}
            height = max(heights.values()) if heights else 0
            if height:
                text = f"lēciens {num(height)} m"
                if len(heights) > 1 and max(heights.values()) - min(heights.values()) >= 0.3:
                    both = " / ".join(
                        f"{PROVIDER_LABELS.get(p, p)} {num(h)}"
                        for p, h in sorted(heights.items(), key=lambda kv: -kv[1]))
                    text += f" ({both})"
                parts.append(text)
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
                lines.append(f"🏄 {rider.get('name', '?')} — " + " · ".join(parts))
        updated.append(entry)
    return lines, updated, changed

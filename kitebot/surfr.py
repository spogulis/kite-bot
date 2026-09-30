"""Surfr (thesurfr.app) client — unofficial.

Uses the same backend the community leaderboard site
(https://surfr-leaderboard.vercel.app) calls, with its public access token.
Entries are per-session, sorted by value, with user id/name/country, spot and
local timestamp. All four categories are per-jump bests, not session totals:
height (m), airtime (s), distance (m — the longest jump, not the distance
ridden), speed (km/h). Undocumented API — treat every call as best-effort.
"""
from __future__ import annotations

import asyncio
import logging

import httpx

from .woo import normalize

API_URL = "https://kiter-271715.appspot.com/leaderboards/list/{category}/{period}/{page}"

# Public token shipped with the community leaderboard site; override with
# `surfr_token` in config.yaml if Surfr requests start failing.
DEFAULT_TOKEN = "e16a0f15-67c5-4306-81a5-0c554a55a222"

PAGE_SIZE = 30
MAX_DAY_PAGES = 100      # one worldwide day is ~1-3k sessions per category
MAX_SEARCH_PAGES = 700   # a week is ~5-8k sessions, a month ~20k+

log = logging.getLogger(__name__)


class SurfrError(Exception):
    pass


async def _page(client: httpx.AsyncClient, token: str, category: str, period: str,
                page: int, date_from: "str | None" = None, date_to: "str | None" = None) -> list:
    params = {"accesstoken": token}
    if date_from and date_to:
        params.update({"from": date_from, "to": date_to})
    try:
        response = await client.get(
            API_URL.format(category=category, period=period, page=page),
            params=params, timeout=30)
        response.raise_for_status()
        data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise SurfrError(f"Surfr request failed: {exc}") from exc
    if isinstance(data, dict):  # {"error": ...}
        raise SurfrError(f"Surfr returned an error: {str(data)[:200]}")
    return data


async def _scan(client, token, category, period, on_item, max_pages,
                date_from=None, date_to=None) -> None:
    """Walk leaderboard pages (batches of 10) until an empty page or the cap."""
    for start in range(0, max_pages, 10):
        pages = await asyncio.gather(*(
            _page(client, token, category, period, p, date_from, date_to)
            for p in range(start, min(start + 10, max_pages))))
        exhausted = False
        for page in pages:
            if not page:
                exhausted = True
            for item in page:
                on_item(item)
        if exhausted:
            return
    log.info("Surfr %s/%s scan truncated at %d pages", category, period, max_pages)


def _item_user(item: dict) -> tuple:
    user = item.get("user") or {}
    return str(user.get("id") or ""), str(user.get("name") or "?")


async def day_stats(token: str, date_str: str, rider_ids: set) -> dict:
    """{rider_id: {"jump_distance_m", "height_m", "spot_name", "spot_country"}}
    for one local date.

    Entries are per-session; a rider's best session value wins. Every entry
    names its spot and the spot's country. Pages come sorted by value and the
    height board is scanned first, so the spot kept is where the rider's
    highest jump happened. Note there is no ridden distance here: Surfr's
    "distance" leaderboard is the longest single jump in metres, and its API
    exposes no session totals. Nor is there a kite — entries carry only the
    board type, and session details need a signed-in user's token.
    """
    stats: dict = {}

    def collect(field):
        def on_item(item):
            rider_id, _ = _item_user(item)
            if rider_id in rider_ids:
                value = float(item.get("value") or 0)
                entry = stats.setdefault(rider_id, {})
                entry[field] = max(entry.get(field, 0), value)
                spot = str(item.get("spotName") or "").strip()
                if spot and not entry.get("spot_name"):
                    entry["spot_name"] = spot
                    entry["spot_country"] = str(item.get("spotCountry") or "").strip()
        return on_item

    async with httpx.AsyncClient() as client:
        await _scan(client, token, "height", "custom", collect("height_m"),
                    MAX_DAY_PAGES, date_str, date_str)
        await _scan(client, token, "distance", "custom", collect("jump_distance_m"),
                    MAX_DAY_PAGES, date_str, date_str)
    return stats


async def find_riders(token: str, query: str, limit: int = 6) -> list:
    """Search riders by name, first in this week's sessions, then this month's.

    Returns [{"rider_id", "name", "country", "best_height_m"}]; entries are
    sorted by value, so a rider's first appearance is their window best.
    """
    needle = normalize(query)
    if not needle:
        return []
    found: dict = {}

    def on_item(item):
        rider_id, name = _item_user(item)
        if not rider_id or rider_id in found or len(found) >= limit:
            return
        if needle in normalize(name):
            user = item.get("user") or {}
            found[rider_id] = {
                "rider_id": rider_id,
                "name": name,
                "country": str(user.get("country") or ""),
                "best_height_m": float(item.get("value") or 0),
            }

    async with httpx.AsyncClient() as client:
        for period in ("weekly", "monthly"):
            await _scan(client, token, "height", period, on_item, MAX_SEARCH_PAGES)
            if found:
                break
    return list(found.values())

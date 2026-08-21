"""Surfr (thesurfr.app) client — unofficial.

Uses the same backend the community leaderboard site
(https://surfr-leaderboard.vercel.app) calls, with its public access token.
Entries are per-session, sorted by value, with user id/name/country, spot and
local timestamp. Categories: height (m), airtime, distance (km), speed.
Undocumented API — treat every call as best-effort.
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
    """{rider_id: {"distance_m": float, "height_m": float}} for one local date.

    Entries are per-session; a rider's best session value wins.
    """
    stats: dict = {}

    def collect(field, factor):
        def on_item(item):
            rider_id, _ = _item_user(item)
            if rider_id in rider_ids:
                value = float(item.get("value") or 0) * factor
                entry = stats.setdefault(rider_id, {})
                entry[field] = max(entry.get(field, 0), value)
        return on_item

    async with httpx.AsyncClient() as client:
        await _scan(client, token, "distance", "custom", collect("distance_m", 1000.0),
                    MAX_DAY_PAGES, date_str, date_str)  # value is km
        await _scan(client, token, "height", "custom", collect("height_m", 1.0),
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

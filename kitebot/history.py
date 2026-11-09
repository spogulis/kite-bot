"""Backfill of the riders' progress chart: each tracked rider's best jump per
month, read back from the WOO and Surfr leaderboards (both answer for any past
date range). Runs once, in the background, when data/history.json does not
exist yet; /backfill reruns it. A month of history costs a full leaderboard
scan per app, about two minutes, so the months go one after another and each
is saved as soon as it is done.
"""
from __future__ import annotations

import logging
from calendar import monthrange
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from . import config, surfr, woo

log = logging.getLogger(__name__)

DEFAULT_MONTHS = 12
MAX_MONTHS = 24


def month_ranges(today: date, months: int) -> list:
    """[(month_key, first_day, last_day, point_date)] for the last `months`
    calendar months up to yesterday, oldest first. A full month's point sits
    mid-month; the current, partial month's sits on its last day so far."""
    yesterday = today - timedelta(days=1)
    out = []
    year, month = yesterday.year, yesterday.month
    for _ in range(months):
        first = date(year, month, 1)
        last = min(date(year, month, monthrange(year, month)[1]), yesterday)
        point = date(year, month, 15) if last.day == monthrange(year, month)[1] else last
        out.append((f"{year}-{month:02d}", first, last, point))
        year, month = (year, month - 1) if month > 1 else (year - 1, 12)
    return list(reversed(out))


async def month_entries(settings, riders: list, first: date, last: date) -> tuple:
    """(session-log style entries, failed providers) for one month."""
    tz = ZoneInfo(settings.timezone)
    woo_ids = {r["ids"]["woo"] for r in riders if "woo" in r["ids"]}
    surfr_ids = {r["ids"]["surfr"] for r in riders if "surfr" in r["ids"]}
    stats: dict = {}
    failed = []
    if woo_ids:
        start = int(datetime(first.year, first.month, first.day, tzinfo=tz).timestamp())
        stop = last + timedelta(days=1)
        end = int(datetime(stop.year, stop.month, stop.day, tzinfo=tz).timestamp())
        try:
            found = await woo.range_best(settings.woo_token or woo.DEFAULT_TOKEN, start, end, woo_ids)
            stats.update({f"woo:{k}": v for k, v in found.items()})
        except woo.WooError as exc:
            log.warning("history: WOO %s failed: %s", first, exc)
            failed.append("WOO")
    if surfr_ids:
        try:
            found = await surfr.range_best(settings.surfr_token or surfr.DEFAULT_TOKEN,
                                           first.isoformat(), last.isoformat(), surfr_ids)
            stats.update({f"surfr:{k}": v for k, v in found.items()})
        except surfr.SurfrError as exc:
            log.warning("history: Surfr %s failed: %s", first, exc)
            failed.append("Surfr")
    spot_names = [s.name for s in config.load_spots(settings)]
    entries = [e for e in woo.session_entries(riders, stats, spot_names) if e.get("height_m")]
    return entries, failed


async def backfill(settings, months: int = DEFAULT_MONTHS, today: "date | None" = None) -> str:
    """Fill data/history.json; returns a one-line Latvian summary."""
    riders = config.load_riders()
    if not riders:
        return "Nav neviena braucēja — nav ko ielādēt."
    today = today or datetime.now(ZoneInfo(settings.timezone)).date()
    history = config.load_history()
    filled, failures = 0, set()
    for key, first, last, point in month_ranges(today, max(1, min(months, MAX_MONTHS))):
        entries, failed = await month_entries(settings, riders, first, last)
        failures.update(failed)
        if failed and not entries:
            continue  # keep whatever an earlier run stored for this month
        history[key] = {"date": point.isoformat(), "entries": entries}
        config.save_history(history)
        filled += 1
        log.info("history: %s done, %d rider(s) jumped", key, len(entries))
    note = f" ({'/'.join(sorted(failures))} daļēji neatbildēja)" if failures else ""
    return f"Vēsture ielādēta: {filled} mēneši{note}."

"""Fetch forecasts for all spots concurrently and turn them into kiteable windows.

Primary source is Open-Meteo (with the spot's configured weather model); if it
stays unreachable through all retries, MET Norway serves as an independent
fallback so the digest almost never shows a fetch error.
"""
from __future__ import annotations

import asyncio
import logging

import httpx

from .analysis import find_windows
from .config import Settings, effective_band
from .forecast import (
    INTERACTIVE_DELAYS, ROBUST_DELAYS, ForecastError, fetch_hours, fetch_hours_metno,
)
from .messages import SpotResult

log = logging.getLogger(__name__)


async def gather_results(spots: list, settings: Settings, robust: bool = False) -> list:
    """robust=True (the scheduled daily job) waits out long provider hiccups;
    interactive commands use quick retries plus the fallback provider."""
    delays = ROBUST_DELAYS if robust else INTERACTIVE_DELAYS
    async with httpx.AsyncClient() as client:

        async def check(spot) -> SpotResult:
            model = spot.model or settings.default_model
            try:
                points = await fetch_hours(client, spot, settings.forecast_days,
                                           settings.wind_unit, model=model, delays=delays)
            except ForecastError as exc:
                log.warning("forecast for %s failed: %s — trying MET Norway", spot.name, exc)
                try:
                    points = await fetch_hours_metno(client, spot, settings.forecast_days,
                                                     settings.wind_unit, settings.timezone)
                    log.info("used MET Norway fallback for %s", spot.name)
                except ForecastError as fallback_exc:
                    log.warning("fallback for %s failed too: %s", spot.name, fallback_exc)
                    return SpotResult(spot=spot, error=str(fallback_exc))
            windows = find_windows(
                points,
                spot,
                min_hours=settings.min_window_hours,
                day_start=settings.day_start_hour,
                day_end=settings.day_end_hour,
                band=effective_band(settings),
            )
            return SpotResult(spot=spot, windows=windows)

        return list(await asyncio.gather(*(check(s) for s in spots)))

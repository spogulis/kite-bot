"""Open-Meteo client. Free forecast API, no key required.

https://open-meteo.com/en/docs — hourly 10 m wind, requested directly in the
configured wind unit, timestamps returned in the spot's local timezone.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from .analysis import HourPoint

API_URL = "https://api.open-meteo.com/v1/forecast"
METNO_URL = "https://api.met.no/weatherapi/locationforecast/2.0/complete"
METNO_HEADERS = {"User-Agent": "kitebot/0.1 github.com/spogulis/kite-bot"}

# m/s (MET Norway's unit) -> configured wind unit
MS_TO = {"ms": 1.0, "kn": 1.943844, "kmh": 3.6, "mph": 2.236936}

# Sleeps between retries. The daily 07:00 job can afford to wait out a long
# Open-Meteo hiccup; interactive /check should stay snappy.
INTERACTIVE_DELAYS = (2, 5)
ROBUST_DELAYS = (30, 60, 120, 180, 240)  # ~10.5 min in total

log = logging.getLogger(__name__)


class ForecastError(Exception):
    pass


async def fetch_hours(client: httpx.AsyncClient, spot, days: int, unit: str = "ms",
                      model: str = "best_match", delays: tuple = INTERACTIVE_DELAYS) -> list:
    params = {
        "latitude": spot.lat,
        "longitude": spot.lon,
        "hourly": "wind_speed_10m,wind_gusts_10m,wind_direction_10m,precipitation",
        "wind_speed_unit": unit,
        "timezone": "auto",
        "forecast_days": max(1, min(int(days), 7)),
        "cell_selection": spot.cell_selection,
    }
    if model and model != "best_match":
        params["models"] = model
    attempts = len(delays) + 1
    data = None
    last_exc: "Exception | None" = None
    for attempt in range(attempts):
        try:
            response = await client.get(API_URL, params=params, timeout=20)
            response.raise_for_status()
            data = response.json()
            break
        except httpx.HTTPError as exc:
            last_exc = exc
            log.warning("Open-Meteo attempt %d/%d failed for %.3f,%.3f: %s",
                        attempt + 1, attempts, spot.lat, spot.lon, exc)
            if attempt < attempts - 1:
                await asyncio.sleep(delays[attempt])
    if data is None:
        raise ForecastError(f"Open-Meteo request failed after {attempts} attempts: {last_exc}") from last_exc

    try:
        tz = ZoneInfo(data["timezone"])
        hourly = data["hourly"]
        points = []
        rain_values = hourly.get("precipitation") or [0] * len(hourly["time"])
        for stamp, speed, gust, direction, rain in zip(
            hourly["time"],
            hourly["wind_speed_10m"],
            hourly["wind_gusts_10m"],
            hourly["wind_direction_10m"],
            rain_values,
        ):
            if speed is None or direction is None:
                continue
            points.append(HourPoint(
                time=datetime.fromisoformat(stamp).replace(tzinfo=tz),
                speed=float(speed),
                gusts=float(gust) if gust is not None else float(speed),
                direction=float(direction),
                rain=float(rain or 0),
            ))
        return points
    except (KeyError, ValueError) as exc:
        raise ForecastError(f"unexpected Open-Meteo response: {exc!r}") from exc


async def fetch_hours_metno(client: httpx.AsyncClient, spot, days: int,
                            unit: str, tz_name: str) -> list:
    """Fallback provider: MET Norway (free, no key, independent of Open-Meteo).

    Hourly for roughly the next 2.5 days, 6-hourly beyond — the sparse tail
    yields no windows (window detection needs consecutive hours), which is an
    acceptable trade-off for a fallback. Local times use the bot's configured
    timezone, since MET Norway does not report the spot's zone.
    """
    from datetime import timedelta

    factor = MS_TO.get(unit, 1.0)
    try:
        response = await client.get(
            METNO_URL,
            params={"lat": round(spot.lat, 4), "lon": round(spot.lon, 4)},
            headers=METNO_HEADERS, timeout=30)
        response.raise_for_status()
        data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise ForecastError(f"MET Norway request failed: {exc}") from exc
    try:
        tz = ZoneInfo(tz_name)
        horizon = datetime.now(tz) + timedelta(days=max(1, min(int(days), 7)))
        points = []
        for entry in data["properties"]["timeseries"]:
            when = datetime.fromisoformat(entry["time"].replace("Z", "+00:00")).astimezone(tz)
            if when > horizon:
                break
            details = entry["data"]["instant"]["details"]
            speed = details.get("wind_speed")
            direction = details.get("wind_from_direction")
            if speed is None or direction is None:
                continue
            gust = details.get("wind_speed_of_gust", speed)
            rain = ((entry["data"].get("next_1_hours") or {})
                    .get("details", {}).get("precipitation_amount", 0))
            points.append(HourPoint(
                time=when,
                speed=float(speed) * factor,
                gusts=float(gust) * factor,
                direction=float(direction),
                rain=float(rain or 0),
            ))
        return points
    except (KeyError, ValueError) as exc:
        raise ForecastError(f"unexpected MET Norway response: {exc!r}") from exc

"""Pure logic: deciding which forecast hours are rideable and grouping them
into contiguous kiteable windows. Wind speeds are unit-agnostic — thresholds
and forecast values just need to share the same unit."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from math import atan2, cos, degrees, radians, sin

COMPASS = [
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
]

# 45°-wide sectors centred on the 8 main compass points; index 0 = N, 1 = NE, …
# Used by the direction-toggle buttons in the Telegram UI.
DIRECTION_SECTORS = [
    [337.5, 22.5], [22.5, 67.5], [67.5, 112.5], [112.5, 157.5],
    [157.5, 202.5], [202.5, 247.5], [247.5, 292.5], [292.5, 337.5],
]


@dataclass
class HourPoint:
    time: datetime          # timezone-aware, local time at the spot
    speed: float            # mean wind speed at 10 m, in the configured unit
    gusts: float            # gust speed at 10 m
    direction: float        # meteorological: degrees the wind blows FROM
    rain: float = 0.0       # precipitation for the hour, mm


@dataclass
class Window:
    start: datetime
    end: datetime           # exclusive: last rideable hour + 1 h
    min_speed: float
    max_speed: float
    max_gust: float
    direction: float        # circular mean over the window
    rain_mm: float = 0.0    # total precipitation over the window

    @property
    def hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600


def compass(deg: float) -> str:
    return COMPASS[int(deg / 22.5 + 0.5) % 16]


def in_sectors(deg: float, sectors: list) -> bool:
    """True if `deg` falls in any [lo, hi] sector. A sector with lo > hi wraps
    through north (e.g. [290, 20]). An empty sector list means any direction."""
    if not sectors:
        return True
    deg = deg % 360
    for lo, hi in sectors:
        if lo <= hi:
            if lo <= deg <= hi:
                return True
        elif deg >= lo or deg <= hi:
            return True
    return False


def toggles_from_sectors(sectors: list) -> list:
    """Which of the 8 main directions are allowed by `sectors` (by centre degree)."""
    return [in_sectors(i * 45, sectors) for i in range(8)]


def sectors_from_toggles(toggles: list) -> list:
    return [list(DIRECTION_SECTORS[i]) for i, on in enumerate(toggles) if on]


def circular_mean(degs: list) -> float:
    x = sum(cos(radians(d)) for d in degs)
    y = sum(sin(radians(d)) for d in degs)
    mean = degrees(atan2(y, x)) % 360
    # a tiny negative atan2 result rounds to exactly 360.0 under % 360
    return mean if mean < 360 else 0.0


def _rideable(point: HourPoint, spot, day_start: int, day_end: int) -> bool:
    return (
        day_start <= point.time.hour < day_end
        and spot.min_wind <= point.speed <= spot.max_wind
        and in_sectors(point.direction, spot.good_directions)
    )


def _window_from(segment: list) -> Window:
    return Window(
        start=segment[0].time,
        end=segment[-1].time + timedelta(hours=1),
        min_speed=min(p.speed for p in segment),
        max_speed=max(p.speed for p in segment),
        max_gust=max(p.gusts for p in segment),
        direction=circular_mean([p.direction for p in segment]),
        rain_mm=sum(p.rain for p in segment),
    )


def _close_run(run: list, min_hours: int, band: float) -> list:
    """Turn a contiguous rideable run into report windows, splitting whenever
    the wind spread within one window would exceed `band` — different wind
    strengths mean different kite sizes, so they deserve separate lines."""
    if len(run) < max(1, min_hours):
        return []
    segments: list = []
    segment = [run[0]]
    lo = hi = run[0].speed
    for point in run[1:]:
        new_lo, new_hi = min(lo, point.speed), max(hi, point.speed)
        if band > 0 and new_hi - new_lo > band:
            segments.append(segment)
            segment = [point]
            lo = hi = point.speed
        else:
            segment.append(point)
            lo, hi = new_lo, new_hi
    segments.append(segment)
    return [_window_from(s) for s in segments]


def find_windows(points: list, spot, *, min_hours: int, day_start: int, day_end: int,
                 band: float = 3.0) -> list:
    """Group consecutive rideable hours into windows of at least `min_hours`.

    Windows never span days because the day_start/day_end filter breaks the
    hourly sequence overnight. `band` caps the wind spread reported as one
    window (0 disables splitting).
    """
    good = [p for p in points if _rideable(p, spot, day_start, day_end)]
    windows: list = []
    run: list = []
    for point in good:
        if run and point.time - run[-1].time != timedelta(hours=1):
            windows.extend(_close_run(run, min_hours, band))
            run = []
        run.append(point)
    windows.extend(_close_run(run, min_hours, band))
    return windows

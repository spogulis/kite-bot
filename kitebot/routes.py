"""Day-route planning: when the wind moves during the day, chain rideable
windows across different spots into one itinerary, respecting driving time.

Travel time is estimated offline: straight-line distance × ROAD_FACTOR at
AVG_SPEED_KMH. A later leg may start mid-window (ride spot A to the end of its
window, arrive at spot B while its window is already running).
"""
from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import timedelta
from math import asin, cos, radians, sin, sqrt

from .messages import WEEKDAYS_LV, direction_word, unit_label

ROAD_FACTOR = 1.3        # straight line -> road distance
AVG_SPEED_KMH = 70.0
MIN_LEG_HOURS = 1.0      # a leg shorter than this is not worth the drive
MAX_WINDOWS = 14         # safety cap for the search

LEG_EMOJI = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣"]


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    rlat1, rlon1, rlat2, rlon2 = map(radians, (lat1, lon1, lat2, lon2))
    a = sin((rlat2 - rlat1) / 2) ** 2 + cos(rlat1) * cos(rlat2) * sin((rlon2 - rlon1) / 2) ** 2
    return 2 * 6371.0 * asin(sqrt(a))


def travel_between(spot_a, spot_b) -> tuple:
    """(minutes, km) by road estimate between two spots."""
    return travel_from(spot_a.lat, spot_a.lon, spot_b)


def travel_from(lat: float, lon: float, spot) -> tuple:
    km = haversine_km(lat, lon, spot.lat, spot.lon) * ROAD_FACTOR
    return km / AVG_SPEED_KMH * 60.0, km


@dataclass
class Leg:
    spot: object
    window: object
    start: object        # effective start: window start, or arrival if later
    travel_min: float    # from the previous leg (0 for the first)
    travel_km: float

    @property
    def hours(self) -> float:
        return (self.window.end - self.start).total_seconds() / 3600


def _chain_score(path: list, total: float, prefer: "str | None") -> tuple:
    """Primary: water time in half-hour buckets. Secondary: the wind
    preference — among similar-length chains, 'strong' picks the windiest,
    'light' the calmest (hour-weighted mean of window mid-speeds)."""
    if total <= 0:
        return (0, 0.0)
    mean = sum(l.hours * (l.window.min_speed + l.window.max_speed) / 2 for l in path) / total
    if prefer == "strong":
        tie = mean
    elif prefer == "light":
        tie = -mean
    else:
        tie = 0.0
    return (round(total * 2), tie)


def day_route(items: list, prefer: "str | None" = None, origin: "tuple | None" = None,
              depart_earliest=None, max_drive_km: "float | None" = None) -> tuple:
    """items: [(spot, window)] within ONE local day.

    origin: optional (lat, lon) the rider starts from — the drive to the first
    spot is reported, and with depart_earliest set (planning for today) the
    first leg cannot start before arrival. prefer: 'strong' | 'light' | None.
    max_drive_km caps the TOTAL day's driving (including from origin).

    Returns (legs, route_hours, best_single_spot_hours); legs is the best
    multi-spot chain, or [] when none involving two spots is feasible.
    """
    items = sorted(items, key=lambda sw: sw[1].start)
    if len(items) > MAX_WINDOWS:
        items = sorted(items, key=lambda sw: -sw[1].hours)[:MAX_WINDOWS]
        items.sort(key=lambda sw: sw[1].start)

    by_spot: dict = {}
    for spot, window in items:
        by_spot[spot.name] = by_spot.get(spot.name, 0.0) + window.hours
    best_single = max(by_spot.values(), default=0.0)

    best_legs: list = []
    best_total = 0.0
    best_score = (0, 0.0)

    def extend(path: list, used: set, total: float, driven_km: float) -> None:
        nonlocal best_legs, best_total, best_score
        if len({leg.spot.name for leg in path}) >= 2:
            score = _chain_score(path, total, prefer)
            if score > best_score:
                best_score, best_total, best_legs = score, total, list(path)
        last = path[-1]
        for idx, (spot, window) in enumerate(items):
            if idx in used or window.end <= last.window.end:
                continue
            if spot.name == last.spot.name:
                travel_min, travel_km = 0.0, 0.0
            else:
                travel_min, travel_km = travel_between(last.spot, spot)
            if max_drive_km is not None and driven_km + travel_km > max_drive_km:
                continue
            arrive = last.window.end + timedelta(minutes=travel_min)
            start = max(window.start, arrive)
            if (window.end - start).total_seconds() / 3600 < MIN_LEG_HOURS:
                continue
            leg = Leg(spot=spot, window=window, start=start,
                      travel_min=travel_min, travel_km=travel_km)
            extend(path + [leg], used | {idx}, total + leg.hours, driven_km + travel_km)

    for idx, (spot, window) in enumerate(items):
        travel_min = travel_km = 0.0
        start = window.start
        if origin is not None:
            travel_min, travel_km = travel_from(origin[0], origin[1], spot)
            if max_drive_km is not None and travel_km > max_drive_km:
                continue
            if depart_earliest is not None:
                start = max(start, depart_earliest + timedelta(minutes=travel_min))
                if (window.end - start).total_seconds() / 3600 < MIN_LEG_HOURS:
                    continue
        first = Leg(spot=spot, window=window, start=start,
                    travel_min=travel_min, travel_km=travel_km)
        extend([first], {idx}, first.hours, travel_km)

    return best_legs, best_total, best_single


def _duration_lv(minutes: float) -> str:
    minutes = round(minutes / 5) * 5
    hours, mins = divmod(int(minutes), 60)
    if hours and mins:
        return f"{hours} h {mins} min"
    if hours:
        return f"{hours} h"
    return f"{mins} min"


def _hours_lv(hours: float) -> str:
    value = round(hours * 2) / 2
    text = f"{value:g}".replace(".", ",")
    return f"~{text} h ūdenī"


def format_route(legs: list, total_hours: float, settings) -> str:
    """Latvian HTML block for one day's route."""
    label = unit_label(settings.wind_unit)
    day = legs[0].start
    total_km = sum(leg.travel_km for leg in legs)
    drive = f" · 🚗 ~{round(total_km)} km" if total_km else ""
    lines = [f"🗺 <b>Maršruts</b> · {WEEKDAYS_LV[day.weekday()]} {day:%d.%m} · "
             f"{_hours_lv(total_hours)}{drive}"]
    for i, leg in enumerate(legs):
        if leg.travel_min:
            origin_note = " no tevis" if i == 0 else ""
            lines.append(f"🚗{origin_note} ~{_duration_lv(leg.travel_min)} ({round(leg.travel_km)} km)")
        w = leg.window
        lo, hi = round(w.min_speed), round(w.max_speed)
        speed = f"{lo} {label}" if lo == hi else f"{lo}–{hi} {label}"
        emoji = LEG_EMOJI[i] if i < len(LEG_EMOJI) else f"{i + 1}."
        lines.append(f"{emoji} {leg.start:%H:%M}–{w.end:%H:%M} · "
                     f"{html.escape(leg.spot.name)} · {speed} · {direction_word(w.direction)}")
    return "\n".join(lines)



"""Telegram Mini App: a JSON forecast API plus static pages, served from the
bot's own process so the bot stays the only writer of data/ files.

The page runs inside Telegram and sends Telegram's signed `initData` string
with every API call. verify_init_data() checks that signature against the bot
token before anything is read, and Access then limits the app to config
admins and members of chats that receive the daily digest.

Nothing here listens unless config.yaml sets webapp_port; the public HTTPS
side is a Cloudflare Tunnel (see README "Mini App").
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import json
import logging
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl
from zoneinfo import ZoneInfo

from aiohttp import web
from telegram.constants import ParseMode
from telegram.error import TelegramError

from . import checker, config, routes
from .analysis import dry_windows, in_sectors
from .config import UNIT_LABELS, Spot, effective_band, effective_foil_min, to_knots
from .messages import WEEKDAYS_LV

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "webapp_static"
INIT_DATA_HEADER = "X-Telegram-Init-Data"
INIT_DATA_MAX_AGE_S = 24 * 3600   # a Mini App left open keeps its first initData
FORECAST_TTL_S = 600               # one Open-Meteo round per 10 minutes, however many viewers
FORECAST_ERROR_TTL_S = 60          # retry sooner when a spot failed
ACCESS_TTL_S = 3600
ACCESS_DENIED_TTL_S = 300
RUNNER_KEY = "webapp_runner"

# eight-wind abbreviations, index 0 = N: Z(iemeļi), A(ustrumi), D(ienvidi), R(ietumi)
DIR_ABBR_LV = ["Z", "ZA", "A", "DA", "D", "DR", "R", "ZR"]
MODEL_LABELS = {
    "best_match": "Open-Meteo", "gfs_seamless": "GFS", "icon_seamless": "ICON",
    "ecmwf_ifs025": "ECMWF", "dmi_seamless": "HARMONIE", "knmi_seamless": "KNMI",
}
# What a chat member's status must be to see the app (ChatMemberStatus values).
_MEMBER_STATUSES = {"creator", "administrator", "member"}

# Leaflet (spot editor map) comes from unpkg with SRI hashes in app.js; tiles
# from OpenStreetMap, whose usage policy wants a Referer — hence the policy below.
CSP = ("default-src 'self'; script-src 'self' https://telegram.org https://unpkg.com; "
       "style-src 'self' 'unsafe-inline' https://unpkg.com; "
       "img-src 'self' data: https://tile.openstreetmap.org; connect-src 'self'; "
       "object-src 'none'; base-uri 'none'; form-action 'none'")
REFERRER_POLICY = "strict-origin-when-cross-origin"  # the origin only, never paths

DENIED_LV = ("Prognoze pieejama tikai to čatu dalībniekiem, kuros ieslēgta "
             "ikdienas ziņa (/subscribe).")
NO_INIT_DATA_LV = "Atver šo lietotni no KiteBot Telegram čata."

_HASH_RE = re.compile(r"[0-9a-f]{64}")


# --- Telegram initData -----------------------------------------------------------

def _check_hash(fields: dict, secret: bytes) -> str:
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    return hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()


def verify_init_data(init_data: str, bot_token: str, *, max_age: int = INIT_DATA_MAX_AGE_S,
                     now: "float | None" = None) -> "dict | None":
    """The initData fields, with `user` decoded, when the string carries a
    valid signature from this bot's Telegram and is fresh; else None.

    https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
    — HMAC-SHA256 over the sorted key=value lines, keyed with
    HMAC-SHA256("WebAppData", bot_token).
    """
    if not init_data or not bot_token:
        return None
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return None
    fields = dict(pairs)
    if len(fields) != len(pairs):  # a repeated key: refuse rather than guess
        return None
    received = fields.pop("hash", "")
    if not _HASH_RE.fullmatch(received):
        return None
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    # Newer clients add an Ed25519 `signature` field. It is part of the signed
    # string for the HMAC; accepting the string without it too costs nothing,
    # since either variant still needs the bot token to forge.
    variants = [fields]
    if "signature" in fields:
        variants.append({k: v for k, v in fields.items() if k != "signature"})
    if not any(hmac.compare_digest(_check_hash(v, secret), received) for v in variants):
        return None
    try:
        auth_date = int(fields.get("auth_date", ""))
    except ValueError:
        return None
    now = time.time() if now is None else now
    if not now - max_age <= auth_date <= now + 60:
        return None
    try:
        user = json.loads(fields.get("user", ""))
    except ValueError:
        return None
    if not isinstance(user, dict) or not isinstance(user.get("id"), int):
        return None
    fields["user"] = user
    return fields


# --- who may look ----------------------------------------------------------------

class Access:
    """Config admins, anyone with a private digest subscription, and members
    of subscribed group chats. Group membership is asked of Telegram (the bot
    sits in those groups) and cached, so opening the app costs no API call
    most of the time."""

    def __init__(self, bot, clock=time.monotonic):
        self.bot = bot
        self._clock = clock
        self._cache: dict = {}

    async def allowed(self, user_id: int, settings) -> bool:
        if user_id in settings.admin_user_ids:
            return True
        subs = config.load_subscriptions()
        if any(s.chat_id == user_id for s in subs):
            return True
        now = self._clock()
        hit = self._cache.get(user_id)
        if hit is not None and hit[1] > now:
            return hit[0]
        ok = False
        for chat_id in sorted({s.chat_id for s in subs if s.chat_id < 0}):
            try:
                member = await self.bot.get_chat_member(chat_id, user_id)
            except TelegramError as exc:
                log.debug("membership check %s in %s failed: %s", user_id, chat_id, exc)
                continue
            status = str(member.status)
            if status in _MEMBER_STATUSES or (status == "restricted"
                                              and getattr(member, "is_member", False)):
                ok = True
                break
        self._cache[user_id] = (ok, now + (ACCESS_TTL_S if ok else ACCESS_DENIED_TTL_S))
        return ok


# --- forecast cache --------------------------------------------------------------

def _cache_key(spots: list, settings) -> str:
    return json.dumps({
        "spots": [s.to_dict() for s in spots],
        "unit": settings.wind_unit, "model": settings.default_model,
        "days": settings.forecast_days, "min_hours": settings.min_window_hours,
        "day": [settings.day_start_hour, settings.day_end_hour],
        "band": effective_band(settings), "foil": effective_foil_min(settings),
    }, sort_keys=True)


class ForecastCache:
    """The latest gather_results() for the current spot list. One lock, so a
    burst of viewers after the morning post triggers a single fetch."""

    def __init__(self, fetch=None, ttl: float = FORECAST_TTL_S,
                 error_ttl: float = FORECAST_ERROR_TTL_S, clock=time.monotonic):
        self._fetch = fetch
        self._ttl = ttl
        self._error_ttl = error_ttl
        self._clock = clock
        self._lock = asyncio.Lock()
        self._entry: "tuple | None" = None  # (key, expires, results, fetched_at)

    async def get(self, spots: list, settings) -> tuple:
        key = _cache_key(spots, settings)
        async with self._lock:
            now = self._clock()
            if self._entry is not None and self._entry[0] == key and self._entry[1] > now:
                return self._entry[2], self._entry[3]
            fetch = self._fetch or checker.gather_results
            results = await fetch(spots, settings)
            fetched_at = datetime.now(timezone.utc)
            ttl = self._error_ttl if any(r.error for r in results) else self._ttl
            self._entry = (key, now + ttl, results, fetched_at)
            return results, fetched_at


# --- payload ---------------------------------------------------------------------

def dir_abbr(deg: float) -> str:
    return DIR_ABBR_LV[int(deg / 45 + 0.5) % 8]


def _r(value: float) -> float:
    return round(float(value), 1)


def day_tabs(settings, today: date) -> list:
    tabs = []
    for offset in range(settings.forecast_days):
        day = today + timedelta(days=offset)
        label = "Šodien" if offset == 0 else f"{WEEKDAYS_LV[day.weekday()]} {day:%d.%m}"
        tabs.append({"offset": offset, "date": day.isoformat(), "label": label})
    return tabs


def _window_json(w, settings, profile: dict) -> dict:
    start_h = w.start.hour + w.start.minute / 60
    return {
        "start": f"{w.start:%H:%M}", "end": f"{w.end:%H:%M}",
        "start_h": start_h, "end_h": start_h + w.hours,
        "min": _r(w.min_speed), "max": _r(w.max_speed), "gust": _r(w.max_gust),
        "dir": round(w.direction), "dir_lv": dir_abbr(w.direction),
        "rain": _r(w.rain_mm), "dry": bool(dry_windows([w])), "foil": w.foil_only,
        "kite": routes.pick_kite((w.min_speed + w.max_speed) / 2, settings.wind_unit,
                                 profile.get("weight_kg"), profile.get("quiver")),
    }


def _turn_notes(spot, windows: list, by_time: dict, foil_min: float) -> list:
    """Why a window stops when the wind is still there: the next hour is
    off-sector or too strong. A window that ends because the wind drops
    needs no note — the bars already show it."""
    low = min(spot.min_wind, foil_min) if foil_min > 0 else spot.min_wind
    notes, seen = [], set()
    for w in windows:
        point = by_time.get(w.end)
        if point is None or point.time.hour in seen:
            continue
        if point.speed >= low and not in_sectors(point.direction, spot.good_directions):
            kind = "sector"
        elif point.speed > spot.max_wind:
            kind = "strong"
        else:
            continue
        seen.add(point.time.hour)
        notes.append({"h": point.time.hour, "kind": kind,
                      "dir": round(point.direction), "dir_lv": dir_abbr(point.direction)})
    return notes


def spot_day(result, settings, target: date, now: datetime, profile: dict) -> dict:
    spot = result.spot
    foil_min = effective_foil_min(settings)
    windows = [w for w in result.windows if w.start.date() == target]
    points = [p for p in result.points if p.time.date() == target
              and settings.day_start_hour <= p.time.hour < settings.day_end_hour]
    hours = []
    for p in points:
        index = next((i for i, w in enumerate(windows) if w.start <= p.time < w.end), None)
        hours.append({
            "h": p.time.hour, "speed": _r(p.speed), "gust": _r(p.gusts),
            "dir": round(p.direction), "rain": _r(p.rain), "w": index,
            "in_sector": in_sectors(p.direction, spot.good_directions),
            "past": p.time + timedelta(hours=1) <= now,
        })
    by_time = {p.time: p for p in result.points}
    model = spot.model or settings.default_model
    return {
        "name": spot.name,
        "error": bool(result.error),
        "min_wind": _r(spot.min_wind), "max_wind": _r(spot.max_wind),
        "foil_min": _r(foil_min) if 0 < foil_min < spot.min_wind else None,
        "sectors": [[_r(lo), _r(hi)] for lo, hi in spot.good_directions],
        "model": MODEL_LABELS.get(model, model),
        "windows": [_window_json(w, settings, profile) for w in windows],
        "hours": hours,
        "notes": _turn_notes(spot, windows, by_time, foil_min),
    }


def forecast_payload(results: list, settings, day: int, now: datetime, profile: dict,
                     fetched_at: datetime) -> dict:
    today = now.date()
    target = today + timedelta(days=day)
    tz = now.tzinfo
    return {
        "unit": UNIT_LABELS.get(settings.wind_unit, settings.wind_unit),
        "day": day,
        "days": day_tabs(settings, today),
        "day_start": settings.day_start_hour,
        "day_end": settings.day_end_hour,
        "updated": f"{fetched_at.astimezone(tz):%H:%M}",
        "now_h": round(now.hour + now.minute / 60, 2) if day == 0 else None,
        "has_kites": bool(profile.get("weight_kg") and profile.get("quiver")),
        "spots": [spot_day(r, settings, target, now, profile) for r in results],
    }


# --- route planner and profile ---------------------------------------------------

ROUTE_PREFS = {"strong": "strong", "light": "light", "any": None}
ROUTE_KMS = (50, 100, 200, 0)   # 0 = no limit, as in /marsruts
QUIVER_MAX = 8
SHARE_COOLDOWN_S = 60


def kite_table(profile: dict, unit: str) -> list:
    """Which of the rider's kites routes would pick for which wind, as the
    wind ranges pick_kite() switches at (biggest kite = lightest wind)."""
    weight = profile.get("weight_kg")
    quiver = sorted(profile.get("quiver") or [], reverse=True)
    if not weight or not quiver:
        return []
    per_knot = 1 / to_knots(1.0, unit)

    def switch(a: float, b: float) -> float:  # wind where kites a and b are equally close
        return _r(routes.KITE_FACTOR * weight / ((a + b) / 2) * per_knot)

    return [{"size": size,
             "lo": switch(quiver[i - 1], size) if i > 0 else None,
             "hi": switch(size, quiver[i + 1]) if i < len(quiver) - 1 else None}
            for i, size in enumerate(quiver)]


def _home(profile: dict) -> "tuple | None":
    home = profile.get("home") or {}
    try:
        return float(home["lat"]), float(home["lon"])
    except (KeyError, TypeError, ValueError):
        return None


def route_args(source, settings) -> "dict | None":
    """day / prefer / km / dry / home from a query string or JSON body, or None."""
    try:
        day = int(source.get("day", 0))
        km = int(source.get("km", 100))
    except (TypeError, ValueError):
        return None
    prefer = str(source.get("prefer", "any"))
    if not 0 <= day < settings.forecast_days or km not in ROUTE_KMS or prefer not in ROUTE_PREFS:
        return None

    def flag(name: str) -> bool:
        return str(source.get(name, "0")).lower() in ("1", "true")

    return {"day": day, "prefer": ROUTE_PREFS[prefer], "km": km,
            "dry": flag("dry"), "home": flag("home")}


def _plan(results: list, settings, args: dict, now: datetime, profile: dict):
    origin = _home(profile) if args["home"] else None
    return routes.plan_day(results, args["day"], now, prefer=args["prefer"], origin=origin,
                           max_drive_km=args["km"] or None, dry=args["dry"])


def _leg_json(leg, settings, profile: dict) -> dict:
    w = leg.window
    return {
        "spot": leg.spot.name, "start": f"{leg.start:%H:%M}", "end": f"{w.end:%H:%M}",
        "min": _r(w.min_speed), "max": _r(w.max_speed), "gust": _r(w.max_gust),
        "dir_lv": dir_abbr(w.direction), "rain": _r(w.rain_mm),
        "kite": routes.pick_kite((w.min_speed + w.max_speed) / 2, settings.wind_unit,
                                 profile.get("weight_kg"), profile.get("quiver")),
        "travel_min": round(leg.travel_min), "travel_km": round(leg.travel_km),
    }


def route_payload(plan, settings, profile: dict, km: int) -> dict:
    if not plan.items:
        return {"kind": "none"}
    if plan.legs:
        legs = [_leg_json(leg, settings, profile) for leg in plan.legs]
        kites = sorted({leg["kite"] for leg in legs if leg["kite"] is not None}, reverse=True)
        return {"kind": "route", "total_h": round(plan.total * 2) / 2,
                "drive_km": round(sum(leg.travel_km for leg in plan.legs)),
                "legs": legs, "kites": kites}
    name, hours, drive_km, within = plan.stay
    return {"kind": "stay", "spot": name, "hours": round(hours * 2) / 2,
            "drive_km": round(drive_km) if drive_km is not None else None,
            "within": within, "km": km,
            "windows": [_window_json(w, settings, profile) for s, w in plan.items if s.name == name]}


def profile_payload(profile: dict, settings) -> dict:
    return {
        "unit": UNIT_LABELS.get(settings.wind_unit, settings.wind_unit),
        "home": _home(profile) is not None,
        "quiver": sorted(profile.get("quiver") or [], reverse=True),
        "weight_kg": profile.get("weight_kg"),
        "kite_table": kite_table(profile, settings.wind_unit),
    }


def parse_profile(body) -> "dict | None":
    """Validated profile fields from a JSON body (same limits as /quiver, /weight)."""
    if not isinstance(body, dict):
        return None
    out: dict = {}
    if "quiver" in body:
        quiver = body["quiver"]
        if not isinstance(quiver, list) or len(quiver) > QUIVER_MAX:
            return None
        try:
            sizes = [float(s) for s in quiver]
        except (TypeError, ValueError):
            return None
        if any(not 2 <= s <= 25 for s in sizes):
            return None
        out["quiver"] = sorted(sizes, reverse=True)
    if "weight_kg" in body:
        weight = body["weight_kg"]
        if weight is not None:
            try:
                weight = float(weight)
            except (TypeError, ValueError):
                return None
            if not 30 <= weight <= 200:
                return None
        out["weight_kg"] = weight
    if "home" in body:
        home = body["home"]
        if home is not None:
            try:
                lat, lon = float(home["lat"]), float(home["lon"])
            except (KeyError, TypeError, ValueError):
                return None
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                return None
            home = {"lat": round(lat, 5), "lon": round(lon, 5)}
        out["home"] = home
    return out or None


# --- spot editor -----------------------------------------------------------------

MAX_SPOT_NAME = 25   # same cap as /addspot: spot names travel in callback data
MAX_SECTORS = 16
CELLS = ("land", "sea", "nearest")


def spot_json(spot) -> dict:
    return {"name": spot.name, "lat": spot.lat, "lon": spot.lon,
            "min_wind": _r(spot.min_wind), "max_wind": _r(spot.max_wind),
            "sectors": [[_r(lo), _r(hi)] for lo, hi in spot.good_directions],
            "cell": spot.cell_selection}


def parse_spot(body) -> "dict | None":
    """Validated spot fields from the editor, or None."""
    if not isinstance(body, dict):
        return None
    try:
        name = " ".join(str(body["name"]).split())
        lat, lon = float(body["lat"]), float(body["lon"])
        lo, hi = float(body["min_wind"]), float(body["max_wind"])
        sectors = [[round(float(a) % 360, 1), round(float(b) % 360, 1)]
                   for a, b in body.get("sectors") or []]
    except (KeyError, TypeError, ValueError):
        return None
    cell = str(body.get("cell", "land"))
    original = body.get("original")
    if (not name or len(name) > MAX_SPOT_NAME or ":" in name
            or not (-90 <= lat <= 90 and -180 <= lon <= 180)
            or not (0 < lo < hi <= 200) or len(sectors) > MAX_SECTORS
            or any(a == b for a, b in sectors) or cell not in CELLS
            or (original is not None and not isinstance(original, str))):
        return None
    return {"name": name, "lat": round(lat, 5), "lon": round(lon, 5), "min_wind": lo,
            "max_wind": hi, "sectors": sectors, "cell": cell, "original": original}


def save_spot(fields: dict) -> "str | None":
    """Add or update a spot; on rename, chats' /myspots filters follow along.
    Returns an error message, or None when saved."""
    spots = config.load_spots()
    original = fields["original"]
    index = next((i for i, s in enumerate(spots) if s.name == original), None)
    if original is not None and index is None:
        return "Šāda spota vairs nav."
    clash = any(s.name.lower() == fields["name"].lower() for i, s in enumerate(spots) if i != index)
    if clash:
        return "Spots ar šādu nosaukumu jau ir."
    old = spots[index] if index is not None else None
    spot = Spot(name=fields["name"], lat=fields["lat"], lon=fields["lon"],
                min_wind=fields["min_wind"], max_wind=fields["max_wind"],
                good_directions=fields["sectors"], cell_selection=fields["cell"],
                model=old.model if old else "")
    if index is None:
        spots.append(spot)
    else:
        spots[index] = spot
    config.save_spots(spots)
    if old is not None and old.name != spot.name:
        for sub in config.load_subscriptions():
            if old.name in sub.spots:
                renamed = tuple(spot.name if n == old.name else n for n in sub.spots)
                config.set_subscription_spots(sub.chat_id, sub.thread_id, renamed)
    return None


# --- riders ----------------------------------------------------------------------

def riders_payload(riders: list, sessions: dict) -> dict:
    """Records ranked, each tracked rider's logged best jumps as a series, and
    the latest logged day's sessions."""
    days = sorted(sessions)
    tracked = [r["name"] for r in sorted(riders, key=lambda r: -r["record_height_m"])]
    points: dict = {name: [] for name in tracked}
    for day in days:
        for entry in sessions[day]:
            if entry.get("name") in points and entry.get("height_m"):
                points[entry["name"]].append([day, entry["height_m"]])
    latest_day = days[-1] if days else None
    latest = [e for e in sessions.get(latest_day, []) if e.get("name") in points]
    latest_by_name = {e["name"]: e for e in latest}
    rows = []
    for rider in sorted(riders, key=lambda r: -r["record_height_m"]):
        last = latest_by_name.get(rider["name"])
        rows.append({
            "name": rider["name"], "record": _r(rider["record_height_m"]),
            "sessions": len(points[rider["name"]]),
            # the record fell on the latest logged day
            "new_record": bool(last and last.get("height_m")
                               and last["height_m"] >= rider["record_height_m"] - 0.05),
        })
    return {
        "riders": rows,
        "series": [{"name": name, "points": points[name]} for name in tracked if points[name]],
        "latest": {"date": latest_day,
                   "entries": sorted(latest, key=lambda e: -(e.get("height_m") or 0))},
        "since": days[0] if days else None,
    }


# --- HTTP ------------------------------------------------------------------------

TOKEN = web.AppKey("token", str)
ACCESS = web.AppKey("access", object)
CACHE = web.AppKey("cache", ForecastCache)
SETTINGS_LOADER = web.AppKey("settings_loader", object)
SPOTS_LOADER = web.AppKey("spots_loader", object)
BOT = web.AppKey("bot", object)
SHARED_AT = web.AppKey("shared_at", dict)


def _error(status: int, message: str) -> web.Response:
    return web.json_response({"error": message}, status=status,
                             headers={"Cache-Control": "no-store"})


def _json(payload: dict) -> web.Response:
    return web.json_response(payload, headers={"Cache-Control": "no-store"},
                             dumps=lambda obj: json.dumps(obj, ensure_ascii=False))


async def _authorize(request: web.Request) -> "tuple | web.Response":
    """(settings, user) for a signed request from an allowed user, else the error."""
    settings = request.app[SETTINGS_LOADER]()
    fields = verify_init_data(request.headers.get(INIT_DATA_HEADER, ""), request.app[TOKEN])
    if fields is None:
        return _error(401, NO_INIT_DATA_LV)
    if not await request.app[ACCESS](fields["user"]["id"], settings):
        return _error(403, DENIED_LV)
    return settings, fields["user"]


async def _results(request: web.Request, settings) -> tuple:
    spots = request.app[SPOTS_LOADER](settings)
    return await request.app[CACHE].get(spots, settings)


async def handle_forecast(request: web.Request) -> web.Response:
    auth = await _authorize(request)
    if isinstance(auth, web.Response):
        return auth
    settings, user = auth
    try:
        day = int(request.query.get("day", "0"))
    except ValueError:
        day = -1
    if not 0 <= day < settings.forecast_days:
        return _error(400, "Nederīga diena.")
    results, fetched_at = await _results(request, settings)
    now = datetime.now(ZoneInfo(settings.timezone))
    payload = forecast_payload(results, settings, day, now, config.get_profile(user["id"]),
                               fetched_at)
    payload["is_admin"] = _is_admin(user, settings)
    return _json(payload)


async def handle_route(request: web.Request) -> web.Response:
    auth = await _authorize(request)
    if isinstance(auth, web.Response):
        return auth
    settings, user = auth
    args = route_args(request.query, settings)
    if args is None:
        return _error(400, "Nederīgi maršruta parametri.")
    profile = config.get_profile(user["id"])
    results, _ = await _results(request, settings)
    now = datetime.now(ZoneInfo(settings.timezone))
    payload = route_payload(_plan(results, settings, args, now, profile), settings,
                            profile, args["km"])
    payload["has_home"] = _home(profile) is not None
    payload["can_share"] = request.app[BOT] is not None and any(
        s.chat_id < 0 for s in config.load_subscriptions())
    return _json(payload)


async def handle_route_share(request: web.Request) -> web.Response:
    """Post the planned route to the subscribed groups the user is in, like
    the chat wizard's "📤 Nosūtīt grupai"."""
    auth = await _authorize(request)
    if isinstance(auth, web.Response):
        return auth
    settings, user = auth
    bot = request.app[BOT]
    if bot is None:
        return _error(503, "Sūtīšana šobrīd nav pieejama.")
    try:
        body = await request.json()
    except ValueError:
        body = None
    args = route_args(body, settings) if isinstance(body, dict) else None
    if args is None:
        return _error(400, "Nederīgi maršruta parametri.")
    now_s = time.monotonic()
    last = request.app[SHARED_AT].get(user["id"])
    if last is not None and now_s - last < SHARE_COOLDOWN_S:
        return _error(429, "Pagaidi minūti pirms nākamā maršruta.")
    profile = config.get_profile(user["id"])
    results, _ = await _results(request, settings)
    now = datetime.now(ZoneInfo(settings.timezone))
    plan = _plan(results, settings, args, now, profile)
    if not plan.items:
        return _error(400, "Šai dienai nav ko sūtīt.")
    text = routes.format_plan(plan, settings, routes.day_label(args["day"], now, args["dry"]),
                              args["km"] or None, weight_kg=profile.get("weight_kg"),
                              quiver=profile.get("quiver"))
    name = str(user.get("first_name") or "").strip()
    if name:
        text += f"\n\n👤 {html.escape(name)}"
    request.app[SHARED_AT][user["id"]] = now_s
    sent = 0
    for chat_id in sorted({s.chat_id for s in config.load_subscriptions() if s.chat_id < 0}):
        try:
            member = await bot.get_chat_member(chat_id, user["id"])
            if str(member.status) in ("left", "kicked"):
                continue
            await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML)
            sent += 1
        except TelegramError as exc:
            log.warning("Mini App route share to %s failed: %s", chat_id, exc)
    return _json({"sent": sent})


def _is_admin(user: dict, settings) -> bool:
    return user["id"] in settings.admin_user_ids


async def handle_spots(request: web.Request) -> web.Response:
    auth = await _authorize(request)
    if isinstance(auth, web.Response):
        return auth
    settings, user = auth
    return _json({
        "is_admin": _is_admin(user, settings),
        "unit": UNIT_LABELS.get(settings.wind_unit, settings.wind_unit),
        "spots": [spot_json(s) for s in request.app[SPOTS_LOADER](settings)],
    })


async def _admin_body(request: web.Request) -> "tuple | web.Response":
    auth = await _authorize(request)
    if isinstance(auth, web.Response):
        return auth
    settings, user = auth
    if not _is_admin(user, settings):
        return _error(403, "Spotus var mainīt tikai config.yaml norādītie admini.")
    try:
        return settings, await request.json()
    except ValueError:
        return settings, None


async def handle_spot_save(request: web.Request) -> web.Response:
    got = await _admin_body(request)
    if isinstance(got, web.Response):
        return got
    settings, body = got
    fields = parse_spot(body)
    if fields is None:
        return _error(400, f"Nederīgi spota dati (nosaukums līdz {MAX_SPOT_NAME} zīmēm, "
                           "min < max vējš).")
    problem = save_spot(fields)
    if problem:
        return _error(409, problem)
    log.info("Mini App: spot %r saved", fields["name"])
    return _json({"spots": [spot_json(s) for s in config.load_spots(settings)]})


async def handle_spot_delete(request: web.Request) -> web.Response:
    got = await _admin_body(request)
    if isinstance(got, web.Response):
        return got
    settings, body = got
    name = body.get("name") if isinstance(body, dict) else None
    spots = config.load_spots(settings)
    remaining = [s for s in spots if s.name != name]
    if not isinstance(name, str) or len(remaining) == len(spots):
        return _error(404, "Šāda spota nav.")
    config.save_spots(remaining)
    log.info("Mini App: spot %r deleted", name)
    return _json({"spots": [spot_json(s) for s in remaining]})


async def handle_riders(request: web.Request) -> web.Response:
    auth = await _authorize(request)
    if isinstance(auth, web.Response):
        return auth
    return _json(riders_payload(config.load_riders(), config.load_sessions()))


async def handle_profile_get(request: web.Request) -> web.Response:
    auth = await _authorize(request)
    if isinstance(auth, web.Response):
        return auth
    settings, user = auth
    return _json(profile_payload(config.get_profile(user["id"]), settings))


async def handle_profile_post(request: web.Request) -> web.Response:
    auth = await _authorize(request)
    if isinstance(auth, web.Response):
        return auth
    settings, user = auth
    try:
        body = await request.json()
    except ValueError:
        body = None
    fields = parse_profile(body)
    if fields is None:
        return _error(400, "Nederīgi profila dati: kaiti 2–25 m² (līdz 8), svars 30–200 kg.")
    return _json(profile_payload(config.update_profile(user["id"], **fields), settings))


async def handle_index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


async def handle_health(request: web.Request) -> web.Response:
    return web.Response(text="ok", headers={"Cache-Control": "no-store"})


async def _security_headers(request: web.Request, response: web.StreamResponse) -> None:
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", REFERRER_POLICY)
    response.headers.setdefault("Content-Security-Policy", CSP)


def build_app(token: str, access, *, cache: "ForecastCache | None" = None,
              settings_loader=None, spots_loader=None, bot=None) -> web.Application:
    """`access(user_id, settings) -> bool` decides who may use the app; `bot`
    (the Telegram bot) is needed only to post routes to groups."""
    app = web.Application(client_max_size=16 * 1024)
    app[TOKEN] = token
    app[ACCESS] = access
    app[CACHE] = cache or ForecastCache()
    app[SETTINGS_LOADER] = settings_loader or config.load_settings
    app[SPOTS_LOADER] = spots_loader or config.load_spots
    app[BOT] = bot
    app[SHARED_AT] = {}
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/forecast", handle_forecast)
    app.router.add_get("/api/route", handle_route)
    app.router.add_post("/api/route/share", handle_route_share)
    app.router.add_get("/api/riders", handle_riders)
    app.router.add_get("/api/spots", handle_spots)
    app.router.add_post("/api/spots/save", handle_spot_save)
    app.router.add_post("/api/spots/delete", handle_spot_delete)
    app.router.add_get("/api/profile", handle_profile_get)
    app.router.add_post("/api/profile", handle_profile_post)
    app.router.add_get("/healthz", handle_health)
    app.router.add_static("/static", STATIC_DIR)
    app.on_response_prepare.append(_security_headers)
    return app


async def start(application) -> None:
    """Start the Mini App server inside the bot's event loop (post_init)."""
    settings = config.load_settings()
    if not settings.webapp_port:
        return
    app = build_app(application.bot.token, Access(application.bot).allowed, bot=application.bot)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, settings.webapp_host, settings.webapp_port)
    try:
        await site.start()
    except OSError as exc:
        await runner.cleanup()
        log.error("Mini App server could not listen on %s:%d: %s — bot continues without it",
                  settings.webapp_host, settings.webapp_port, exc)
        return
    application.bot_data[RUNNER_KEY] = runner
    log.info("Mini App server listening on %s:%d", settings.webapp_host, settings.webapp_port)


async def stop(application) -> None:
    runner = application.bot_data.pop(RUNNER_KEY, None)
    if runner is not None:
        await runner.cleanup()

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
import json
import logging
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl
from zoneinfo import ZoneInfo

from aiohttp import web
from telegram.error import TelegramError

from . import checker, config, routes
from .analysis import dry_windows, in_sectors
from .config import UNIT_LABELS, effective_band, effective_foil_min
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

CSP = ("default-src 'self'; script-src 'self' https://telegram.org; "
       "style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; "
       "object-src 'none'; base-uri 'none'; form-action 'none'")

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


# --- HTTP ------------------------------------------------------------------------

TOKEN = web.AppKey("token", str)
ACCESS = web.AppKey("access", object)
CACHE = web.AppKey("cache", ForecastCache)
SETTINGS_LOADER = web.AppKey("settings_loader", object)
SPOTS_LOADER = web.AppKey("spots_loader", object)


def _error(status: int, message: str) -> web.Response:
    return web.json_response({"error": message}, status=status,
                             headers={"Cache-Control": "no-store"})


async def handle_forecast(request: web.Request) -> web.Response:
    settings = request.app[SETTINGS_LOADER]()
    fields = verify_init_data(request.headers.get(INIT_DATA_HEADER, ""), request.app[TOKEN])
    if fields is None:
        return _error(401, NO_INIT_DATA_LV)
    user_id = fields["user"]["id"]
    if not await request.app[ACCESS](user_id, settings):
        return _error(403, DENIED_LV)
    try:
        day = int(request.query.get("day", "0"))
    except ValueError:
        day = -1
    if not 0 <= day < settings.forecast_days:
        return _error(400, "Nederīga diena.")
    spots = request.app[SPOTS_LOADER](settings)
    results, fetched_at = await request.app[CACHE].get(spots, settings)
    now = datetime.now(ZoneInfo(settings.timezone))
    payload = forecast_payload(results, settings, day, now, config.get_profile(user_id), fetched_at)
    return web.json_response(payload, headers={"Cache-Control": "no-store"},
                             dumps=lambda obj: json.dumps(obj, ensure_ascii=False))


async def handle_index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


async def handle_health(request: web.Request) -> web.Response:
    return web.Response(text="ok", headers={"Cache-Control": "no-store"})


async def _security_headers(request: web.Request, response: web.StreamResponse) -> None:
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Content-Security-Policy", CSP)


def build_app(token: str, access, *, cache: "ForecastCache | None" = None,
              settings_loader=None, spots_loader=None) -> web.Application:
    """`access(user_id, settings) -> bool` decides who may read the forecast."""
    app = web.Application()
    app[TOKEN] = token
    app[ACCESS] = access
    app[CACHE] = cache or ForecastCache()
    app[SETTINGS_LOADER] = settings_loader or config.load_settings
    app[SPOTS_LOADER] = spots_loader or config.load_spots
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/forecast", handle_forecast)
    app.router.add_get("/healthz", handle_health)
    app.router.add_static("/static", STATIC_DIR)
    app.on_response_prepare.append(_security_headers)
    return app


async def start(application) -> None:
    """Start the Mini App server inside the bot's event loop (post_init)."""
    settings = config.load_settings()
    if not settings.webapp_port:
        return
    app = build_app(application.bot.token, Access(application.bot).allowed)
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

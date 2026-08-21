"""Configuration and persistence.

- config.yaml (repo root): hand-edited settings, read fresh on every use;
  schedule changes need a restart.
- data/spots.yaml: the spot list, editable via /addspot & /delspot or by hand.
- data/subscriptions.json: chats that receive the daily digest.

Wind speeds (spot thresholds, messages, forecast requests) all use the unit
configured as `wind_unit`.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

ROOT = Path(os.environ.get("KITEBOT_HOME", Path(__file__).resolve().parent.parent))
CONFIG_FILE = ROOT / "config.yaml"
DATA_DIR = ROOT / "data"
SPOTS_FILE = DATA_DIR / "spots.yaml"
SUBSCRIPTIONS_FILE = DATA_DIR / "subscriptions.json"
RIDERS_FILE = DATA_DIR / "riders.json"
USERS_FILE = DATA_DIR / "users.json"

# Wind units supported by Open-Meteo's wind_speed_unit parameter.
UNIT_LABELS = {"kn": "kn", "ms": "m/s", "kmh": "km/h", "mph": "mph"}
UNIT_DEFAULT_RANGE = {
    "kn": (12.0, 38.0),
    "ms": (6.0, 20.0),
    "kmh": (22.0, 70.0),
    "mph": (14.0, 44.0),
}
_KNOTS_TO = {"kn": 1.0, "ms": 0.514444, "kmh": 1.852, "mph": 1.150779}
# Default max wind spread reported as a single window line, per unit — a
# bigger change within the day means a different kite and gets its own line.
UNIT_BAND = {"kn": 6.0, "ms": 3.0, "kmh": 11.0, "mph": 7.0}
_UNIT_ALIASES = {
    "kn": "kn", "knots": "kn", "kt": "kn", "kts": "kn",
    "ms": "ms", "m/s": "ms",
    "kmh": "kmh", "km/h": "kmh", "kph": "kmh",
    "mph": "mph",
}

# Open-Meteo weather models; "gfs" is the model Windguru's primary forecast uses.
WEATHER_MODELS = {
    "best": "best_match", "best_match": "best_match",
    "gfs": "gfs_seamless", "gfs_seamless": "gfs_seamless",
    "icon": "icon_seamless", "icon_seamless": "icon_seamless",
    "ecmwf": "ecmwf_ifs025", "ecmwf_ifs025": "ecmwf_ifs025",
}


def normalize_model(value) -> str:
    model = WEATHER_MODELS.get(str(value).strip().lower())
    if model is None:
        raise ValueError(f"modelim jābūt vienam no: best, gfs, icon, ecmwf — saņēmu {value!r}")
    return model


SPOTS_HEADER = """\
# Kite spots checked by the bot.
# The bot rewrites this file on /addspot and /delspot, so hand-edit it only
# while the bot is stopped. Fields:
#   min_wind / max_wind: rideable wind range (10 m mean wind), in the unit set
#     as wind_unit in config.yaml. (Legacy min_knots/max_knots fields are read
#     as knots and converted automatically.)
#   good_directions: [from, to] sectors in degrees (direction the wind blows FROM).
#     A sector may wrap through north, e.g. [290, 20]. Empty list = any direction.
#   cell_selection: which Open-Meteo grid cell to use — "land" (default),
#     "sea" or "nearest". "sea" can represent coastal spots better.
"""


def normalize_wind_unit(value) -> str:
    unit = _UNIT_ALIASES.get(str(value).strip().lower())
    if unit is None:
        raise SystemExit(f"wind_unit must be one of kn, ms, kmh, mph — got {value!r}")
    return unit


@dataclass
class Spot:
    name: str
    lat: float
    lon: float
    min_wind: float = 6.0     # in the configured wind unit
    max_wind: float = 20.0
    good_directions: list = field(default_factory=list)
    cell_selection: str = "land"
    model: str = ""  # per-spot weather model override; empty = settings.default_model

    @classmethod
    def from_dict(cls, raw: dict, unit: str = "ms") -> "Spot":
        lo_default, hi_default = UNIT_DEFAULT_RANGE[unit]
        to_unit = _KNOTS_TO[unit]
        if "min_wind" in raw:
            min_wind = float(raw["min_wind"])
        elif "min_knots" in raw:  # legacy field, always knots
            min_wind = float(raw["min_knots"]) * to_unit
        else:
            min_wind = lo_default
        if "max_wind" in raw:
            max_wind = float(raw["max_wind"])
        elif "max_knots" in raw:
            max_wind = float(raw["max_knots"]) * to_unit
        else:
            max_wind = hi_default
        return cls(
            name=str(raw["name"]),
            lat=float(raw["lat"]),
            lon=float(raw["lon"]),
            min_wind=min_wind,
            max_wind=max_wind,
            good_directions=[[float(lo), float(hi)] for lo, hi in raw.get("good_directions") or []],
            cell_selection=str(raw.get("cell_selection", "land")),
            model=_safe_model(raw.get("model")),
        )

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "lat": self.lat,
            "lon": self.lon,
            "min_wind": round(self.min_wind, 2),
            "max_wind": round(self.max_wind, 2),
            "good_directions": self.good_directions,
            "cell_selection": self.cell_selection,
            "model": self.model,
        }


def _safe_model(value) -> str:
    if not value:
        return ""
    try:
        return normalize_model(value)
    except ValueError:
        log.warning("unknown weather model %r in spots file, using the default", value)
        return ""


def default_spots(unit: str) -> list:
    lo, hi = UNIT_DEFAULT_RANGE[unit]
    return [
        Spot(name="Podersdorf", lat=47.854, lon=16.835,
             min_wind=lo, max_wind=hi, good_directions=[[290, 20], [110, 170]]),
        Spot(name="Rust", lat=47.796, lon=16.685,
             min_wind=lo, max_wind=hi, good_directions=[[290, 20], [110, 170]]),
    ]


@dataclass
class Settings:
    timezone: str = "Europe/Riga"
    wind_unit: str = "ms"
    daily_post_time: str = "07:00"
    daily_greeting: str = "Labrīt, kaiteri!"
    forecast_days: int = 3
    post_when_no_wind: bool = True
    min_window_hours: int = 2
    day_start_hour: int = 8
    day_end_hour: int = 20
    wind_band: float = 0.0  # 0 = per-unit default from UNIT_BAND
    admin_user_ids: list = field(default_factory=list)
    woo_token: str = ""  # empty = use the built-in anonymous leaderboard token
    surfr_token: str = ""  # empty = use the built-in public leaderboard token
    default_model: str = "best_match"  # weather model unless a spot overrides it


def load_settings() -> Settings:
    raw: dict = {}
    if CONFIG_FILE.exists():
        try:
            raw = yaml.safe_load(CONFIG_FILE.read_text()) or {}
        except yaml.YAMLError as exc:
            raise SystemExit(f"Could not parse {CONFIG_FILE}: {exc}") from exc
    if not isinstance(raw, dict):
        raise SystemExit(f"{CONFIG_FILE} must contain a mapping of settings")
    s = Settings()
    s.timezone = str(raw.get("timezone", s.timezone))
    s.wind_unit = normalize_wind_unit(raw.get("wind_unit", s.wind_unit))
    s.daily_post_time = str(raw.get("daily_post_time", s.daily_post_time))
    s.daily_greeting = str(raw.get("daily_greeting", s.daily_greeting)).strip()
    s.forecast_days = min(7, max(1, int(raw.get("forecast_days", s.forecast_days))))
    s.post_when_no_wind = bool(raw.get("post_when_no_wind", s.post_when_no_wind))
    s.min_window_hours = max(1, int(raw.get("min_window_hours", s.min_window_hours)))
    s.wind_band = max(0.0, float(raw.get("wind_band", 0) or 0))
    s.day_start_hour = min(23, max(0, int(raw.get("day_start_hour", s.day_start_hour))))
    s.day_end_hour = min(24, max(s.day_start_hour + 1, int(raw.get("day_end_hour", s.day_end_hour))))
    s.admin_user_ids = [int(x) for x in raw.get("admin_user_ids") or []]
    s.woo_token = str(raw.get("woo_token", s.woo_token)).strip()
    s.surfr_token = str(raw.get("surfr_token", s.surfr_token)).strip()
    try:
        s.default_model = normalize_model(raw.get("default_model", s.default_model))
    except ValueError as exc:
        raise SystemExit(f"config.yaml default_model: {exc}") from exc
    return s


def effective_band(settings: "Settings") -> float:
    return settings.wind_band or UNIT_BAND.get(settings.wind_unit, 3.0)


def load_spots(settings: "Settings | None" = None) -> list:
    settings = settings or load_settings()
    if not SPOTS_FILE.exists():
        spots = default_spots(settings.wind_unit)
        save_spots(spots)
        log.info("created %s with example spots", SPOTS_FILE)
        return spots
    raw = yaml.safe_load(SPOTS_FILE.read_text()) or []
    if not isinstance(raw, list):
        raise SystemExit(f"{SPOTS_FILE} must contain a list of spots")
    return [Spot.from_dict(item, settings.wind_unit) for item in raw]


def save_spots(spots: list) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    body = yaml.safe_dump([s.to_dict() for s in spots], sort_keys=False, allow_unicode=True)
    SPOTS_FILE.write_text(SPOTS_HEADER + body)


@dataclass(frozen=True)
class Subscription:
    chat_id: int
    thread_id: "int | None" = None  # set when subscribed inside a forum topic
    spots: tuple = ()               # spot names this chat's digest covers; empty = all


def _sub_key(sub: Subscription) -> tuple:
    return (sub.chat_id, sub.thread_id)


def load_subscriptions() -> list:
    if not SUBSCRIPTIONS_FILE.exists():
        return []
    try:
        raw = json.loads(SUBSCRIPTIONS_FILE.read_text())
    except json.JSONDecodeError:
        log.warning("could not parse %s, treating as empty", SUBSCRIPTIONS_FILE)
        return []
    subs = []
    for item in raw.get("subscriptions", []):
        thread_id = item.get("thread_id")
        subs.append(Subscription(
            chat_id=int(item["chat_id"]),
            thread_id=int(thread_id) if thread_id is not None else None,
            spots=tuple(str(n) for n in item.get("spots") or []),
        ))
    return subs


def _save_subscriptions(subs: list) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    seen: set = set()
    unique = []
    for s in subs:
        if _sub_key(s) in seen:
            continue
        seen.add(_sub_key(s))
        unique.append(s)
    payload = {"subscriptions": [
        {"chat_id": s.chat_id, "thread_id": s.thread_id, "spots": list(s.spots)} for s in unique
    ]}
    SUBSCRIPTIONS_FILE.write_text(json.dumps(payload, indent=2))


def find_subscription(chat_id: int, thread_id) -> "Subscription | None":
    for s in load_subscriptions():
        if _sub_key(s) == (chat_id, thread_id):
            return s
    return None


def add_subscription(sub: Subscription) -> bool:
    subs = load_subscriptions()
    if any(_sub_key(s) == _sub_key(sub) for s in subs):
        return False
    subs.append(sub)
    _save_subscriptions(subs)
    return True


def remove_subscription(sub: Subscription) -> bool:
    subs = load_subscriptions()
    kept = [s for s in subs if _sub_key(s) != _sub_key(sub)]
    if len(kept) == len(subs):
        return False
    _save_subscriptions(kept)
    return True


def set_subscription_spots(chat_id: int, thread_id, names: tuple) -> "Subscription | None":
    subs = load_subscriptions()
    updated = None
    result = []
    for s in subs:
        if _sub_key(s) == (chat_id, thread_id):
            updated = Subscription(chat_id=s.chat_id, thread_id=s.thread_id, spots=tuple(names))
            result.append(updated)
        else:
            result.append(s)
    if updated is not None:
        _save_subscriptions(result)
    return updated


def drop_chat(chat_id: int) -> None:
    _save_subscriptions([s for s in load_subscriptions() if s.chat_id != chat_id])


def migrate_chat(old_chat_id: int, new_chat_id: int) -> None:
    _save_subscriptions([
        Subscription(chat_id=new_chat_id, thread_id=s.thread_id, spots=s.spots)
        if s.chat_id == old_chat_id else s
        for s in load_subscriptions()
    ])


def load_riders() -> list:
    """[{"name", "record_height_m", "ids": {"woo": id, "surfr": id}}] — one
    entry per person; a merged person carries an id per app. Legacy formats
    (flat provider/rider_id, or woo_id) migrate on load."""
    if not RIDERS_FILE.exists():
        return []
    try:
        raw = json.loads(RIDERS_FILE.read_text())
    except json.JSONDecodeError:
        log.warning("could not parse %s, treating as empty", RIDERS_FILE)
        return []
    riders = []
    for item in raw.get("riders", []):
        ids = {str(k): str(v) for k, v in (item.get("ids") or {}).items() if v}
        if not ids:
            rider_id = str(item.get("rider_id") or item.get("woo_id") or "")
            if rider_id:
                ids = {str(item.get("provider") or "woo"): rider_id}
        riders.append({
            "name": str(item.get("name") or "?"),
            "record_height_m": float(item.get("record_height_m") or 0),
            "ids": ids,
        })
    return [r for r in riders if r["ids"]]


def save_riders(riders: list) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    seen: set = set()
    unique = []
    for r in riders:
        keys = set(r["ids"].items())
        if keys and keys <= seen:
            continue
        seen |= keys
        unique.append(r)
    RIDERS_FILE.write_text(json.dumps({"riders": unique}, indent=2, ensure_ascii=False))


def add_rider(provider: str, rider_id: str, name: str, record_height_m: float) -> bool:
    """Add a person, or refresh an existing one that already has this app id
    (keeping their possibly-merged identity). Returns True if newly added."""
    riders = load_riders()
    for r in riders:
        if r["ids"].get(provider) == rider_id:
            r["record_height_m"] = max(r["record_height_m"], float(record_height_m))
            save_riders(riders)
            return False
    riders.append({"name": name, "record_height_m": float(record_height_m),
                   "ids": {provider: rider_id}})
    save_riders(riders)
    return True


def remove_rider(rider_id: str) -> bool:
    riders = load_riders()
    kept = [r for r in riders if rider_id not in r["ids"].values()]
    if len(kept) == len(riders):
        return False
    save_riders(kept)
    return True


def find_rider_by_prefix(riders: list, prefix: str) -> "dict | None":
    """Resolve a rider by an id prefix (used in callback buttons, which are
    too small for two full WOO UUIDs). None unless exactly one rider matches."""
    matches = [r for r in riders
               if any(v.startswith(prefix) for v in r["ids"].values())]
    return matches[0] if len(matches) == 1 else None


def merge_riders(prefix_a: str, prefix_b: str) -> "dict | None":
    """Merge rider B into rider A (ids united, record = max). The merged entry
    keeps A's name. Returns the merged rider, or None if unresolvable."""
    riders = load_riders()
    a = find_rider_by_prefix(riders, prefix_a)
    b = find_rider_by_prefix(riders, prefix_b)
    if a is None or b is None or a is b:
        return None
    for provider, rider_id in b["ids"].items():
        a["ids"].setdefault(provider, rider_id)
    a["record_height_m"] = max(a["record_height_m"], b["record_height_m"])
    riders.remove(b)
    save_riders(riders)
    return a


def to_knots(value: float, unit: str) -> float:
    """Convert a wind speed in the configured unit to knots."""
    return value / _KNOTS_TO.get(unit, 1.0)


def _load_profiles() -> dict:
    if not USERS_FILE.exists():
        return {}
    try:
        return json.loads(USERS_FILE.read_text())
    except json.JSONDecodeError:
        log.warning("could not parse %s, treating as empty", USERS_FILE)
        return {}


def get_profile(user_id: int) -> dict:
    """Personal profile: {"home": {"lat","lon"}, "quiver": [m2...], "weight_kg": float}."""
    return _load_profiles().get(str(user_id), {})


def update_profile(user_id: int, **fields) -> dict:
    profiles = _load_profiles()
    profile = profiles.setdefault(str(user_id), {})
    profile.update(fields)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    USERS_FILE.write_text(json.dumps(profiles, indent=2, ensure_ascii=False))
    return profile


def get_token() -> str:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN is not set. Create a bot with @BotFather, then put the "
            "token in a .env file (see .env.example) or export it in the environment."
        )
    return token

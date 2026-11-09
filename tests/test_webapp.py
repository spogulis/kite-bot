"""Mini App: initData signatures, who may look, the forecast payload, the
HTTP surface, and the bot-side buttons."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import pytest

pytest.importorskip("aiohttp")

from kitebot import config, handlers, webapp  # noqa: E402
from kitebot.analysis import HourPoint, find_windows  # noqa: E402
from kitebot.config import Settings, Spot, Subscription  # noqa: E402
from kitebot.messages import SpotResult  # noqa: E402

TOKEN = "123456:TEST-token"
RIGA = ZoneInfo("Europe/Riga")


def signed(token: str = TOKEN, user_id: int = 42, auth_date: "int | None" = None,
           extra: "dict | None" = None, unsigned: "dict | None" = None) -> str:
    """initData the way Telegram builds it; `unsigned` fields are added after signing."""
    fields = {
        "auth_date": str(int(time.time()) if auth_date is None else auth_date),
        "query_id": "AAHdF6IQAAAAAN0XohDhrOrc",
        "user": json.dumps({"id": user_id, "first_name": "Jānis"},
                           separators=(",", ":"), ensure_ascii=False),
    }
    fields.update(extra or {})
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    fields.update(unsigned or {})
    return urlencode(fields)


# --- initData ------------------------------------------------------------------

def test_valid_init_data_gives_the_user():
    fields = webapp.verify_init_data(signed(), TOKEN)
    assert fields is not None
    assert fields["user"] == {"id": 42, "first_name": "Jānis"}


def test_init_data_signed_for_another_bot_is_refused():
    assert webapp.verify_init_data(signed(token="999:OTHER"), TOKEN) is None


def test_tampered_init_data_is_refused():
    forged = signed().replace("%22id%22%3A42", "%22id%22%3A1")
    assert "%22id%22%3A1" in forged
    assert webapp.verify_init_data(forged, TOKEN) is None


def test_stale_or_future_init_data_is_refused():
    now = time.time()
    assert webapp.verify_init_data(signed(auth_date=int(now) - 25 * 3600), TOKEN, now=now) is None
    assert webapp.verify_init_data(signed(auth_date=int(now) + 3600), TOKEN, now=now) is None
    assert webapp.verify_init_data(signed(auth_date=int(now) - 3600), TOKEN, now=now) is not None


@pytest.mark.parametrize("raw", ["", "garbage", "user=%7B%7D", "hash=zz&auth_date=1"])
def test_malformed_init_data_is_refused(raw):
    assert webapp.verify_init_data(raw, TOKEN) is None


def test_repeated_keys_are_refused():
    assert webapp.verify_init_data(signed() + "&auth_date=1", TOKEN) is None


def test_signature_field_works_signed_or_not():
    assert webapp.verify_init_data(signed(extra={"signature": "abc"}), TOKEN) is not None
    assert webapp.verify_init_data(signed(unsigned={"signature": "abc"}), TOKEN) is not None


def test_empty_token_never_verifies():
    assert webapp.verify_init_data(signed(token=""), "") is None


# --- access ----------------------------------------------------------------------

class Member:
    def __init__(self, status, is_member=False):
        self.status = status
        self.is_member = is_member


class FakeBot:
    def __init__(self, members: dict):
        self.members = members  # (chat_id, user_id) -> status
        self.calls = 0

    async def get_chat_member(self, chat_id, user_id):
        from telegram.error import BadRequest
        self.calls += 1
        status = self.members.get((chat_id, user_id))
        if status is None:
            raise BadRequest("user not found")
        return Member(status, is_member=status == "restricted")


def test_access_rules(monkeypatch):
    subs = [Subscription(chat_id=-100), Subscription(chat_id=-200), Subscription(chat_id=77)]
    monkeypatch.setattr(config, "load_subscriptions", lambda: subs)
    bot = FakeBot({(-200, 5): "member", (-100, 6): "left", (-100, 8): "restricted"})
    access = webapp.Access(bot)
    settings = Settings(admin_user_ids=[1])

    def allowed(uid):
        return asyncio.run(access.allowed(uid, settings))

    assert allowed(1)        # admin
    assert allowed(77)       # own private chat is subscribed
    assert allowed(5)        # member of a subscribed group
    assert allowed(8)        # restricted but still in the group
    assert not allowed(6)    # left the group
    assert not allowed(9)    # never in any of them
    calls = bot.calls
    assert allowed(5) and not allowed(9)
    assert bot.calls == calls  # answered from the cache


def test_access_cache_expires():
    clock = [0.0]
    bot = FakeBot({(-100, 5): "member"})
    access = webapp.Access(bot, clock=lambda: clock[0])
    settings = Settings()
    config_subs = [Subscription(chat_id=-100)]

    async def check():
        return await access.allowed(5, settings)

    original = config.load_subscriptions
    config.load_subscriptions = lambda: config_subs
    try:
        assert asyncio.run(check())
        bot.members.clear()  # left the group
        clock[0] = webapp.ACCESS_TTL_S - 1
        assert asyncio.run(check())
        clock[0] = webapp.ACCESS_TTL_S + 1
        assert not asyncio.run(check())
    finally:
        config.load_subscriptions = original


# --- forecast cache --------------------------------------------------------------

def test_forecast_cache_fetches_once_per_ttl():
    clock = [0.0]
    calls = []

    async def fetch(spots, settings):
        calls.append(1)
        return [SpotResult(spot=s) for s in spots]

    cache = webapp.ForecastCache(fetch=fetch, ttl=100, error_ttl=10, clock=lambda: clock[0])
    spots = [Spot(name="A", lat=1, lon=2)]
    settings = Settings()

    async def get(the_spots=spots):
        return await cache.get(the_spots, settings)

    asyncio.run(get())
    asyncio.run(get())
    assert len(calls) == 1
    clock[0] = 101
    asyncio.run(get())
    assert len(calls) == 2
    asyncio.run(get([Spot(name="B", lat=1, lon=2)]))  # spot list changed
    assert len(calls) == 3


def test_forecast_cache_retries_errors_sooner():
    clock = [0.0]
    calls = []

    async def fetch(spots, settings):
        calls.append(1)
        return [SpotResult(spot=s, error="down") for s in spots]

    cache = webapp.ForecastCache(fetch=fetch, ttl=100, error_ttl=10, clock=lambda: clock[0])
    spots = [Spot(name="A", lat=1, lon=2)]
    asyncio.run(cache.get(spots, Settings()))
    clock[0] = 11
    asyncio.run(cache.get(spots, Settings()))
    assert len(calls) == 2


# --- payload ---------------------------------------------------------------------

ENGURE = Spot(name="Engure", lat=57.16, lon=23.22, min_wind=6, max_wind=20,
              good_directions=[[247.5, 337.5]])  # west to north-west, not north
SPEEDS = [4, 5, 6, 7, 8, 8, 9, 9, 8, 7, 5, 4]       # 08:00 … 19:00


def engure_day(day) -> SpotResult:
    points = []
    for i, speed in enumerate(SPEEDS):
        hour = 8 + i
        points.append(HourPoint(
            time=datetime(day.year, day.month, day.day, hour, tzinfo=RIGA),
            speed=speed, gusts=speed + 3,
            direction=315 if hour < 14 else 0,  # turns north at 14:00
        ))
    settings = Settings()
    windows = find_windows(points, ENGURE, min_hours=2, day_start=8, day_end=20,
                           band=3, foil_min=4.5)
    assert settings.wind_unit == "ms"
    return SpotResult(spot=ENGURE, windows=windows, points=points)


def test_payload_window_hours_and_turn_note():
    now = datetime(2026, 11, 9, 12, 30, tzinfo=RIGA)
    payload = webapp.forecast_payload(
        [engure_day(now.date())], Settings(), 0, now,
        {"weight_kg": 85, "quiver": [12, 9, 7]}, now)
    assert payload["unit"] == "m/s"
    assert payload["days"][0] == {"offset": 0, "date": "2026-11-09", "label": "Šodien"}
    assert payload["days"][1]["label"] == "Ot 10.11"
    assert payload["has_kites"] is True
    assert payload["now_h"] == 12.5
    [spot] = payload["spots"]
    [window] = spot["windows"]
    assert (window["start"], window["end"]) == ("10:00", "14:00")
    assert (window["min"], window["max"], window["gust"]) == (6, 8, 11)
    assert window["dir_lv"] == "ZR" and window["dry"] and not window["foil"]
    assert window["kite"] == 12  # 85 kg in ~7 m/s
    states = {h["h"]: h["w"] for h in spot["hours"]}
    assert [h for h, w in states.items() if w == 0] == [10, 11, 12, 13]
    assert [h["h"] for h in spot["hours"] if h["past"]] == [8, 9, 10, 11]
    assert spot["hours"][6]["in_sector"] is False  # 14:00, from the north
    assert spot["notes"] == [{"h": 14, "kind": "sector", "dir": 0, "dir_lv": "Z"}]
    assert spot["foil_min"] == 4.5
    assert spot["sectors"] == [[247.5, 337.5]]


def test_payload_other_day_has_no_hours_of_today():
    now = datetime(2026, 11, 9, 7, 0, tzinfo=RIGA)
    payload = webapp.forecast_payload([engure_day(now.date())], Settings(), 1, now, {}, now)
    [spot] = payload["spots"]
    assert spot["hours"] == [] and spot["windows"] == []
    assert payload["has_kites"] is False
    assert payload["now_h"] is None  # no "now" line on another day


def test_payload_without_profile_has_no_kite_pick():
    now = datetime(2026, 11, 9, 7, 0, tzinfo=RIGA)
    payload = webapp.forecast_payload([engure_day(now.date())], Settings(), 0, now, {}, now)
    assert payload["spots"][0]["windows"][0]["kite"] is None


def test_too_strong_turn_note():
    spot = Spot(name="X", lat=1, lon=2, min_wind=6, max_wind=10)
    day = datetime(2026, 11, 9, tzinfo=RIGA)
    points = [HourPoint(time=day.replace(hour=h), speed=s, gusts=s + 2, direction=270)
              for h, s in zip(range(10, 14), [8, 9, 14, 15])]
    windows = find_windows(points, spot, min_hours=2, day_start=8, day_end=20)
    out = webapp.spot_day(SpotResult(spot=spot, windows=windows, points=points),
                          Settings(), day.date(), day, {})
    assert out["notes"] == [{"h": 12, "kind": "strong", "dir": 270, "dir_lv": "R"}]


# --- HTTP ------------------------------------------------------------------------

def test_http_surface(monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer

    monkeypatch.setattr(config, "get_profile", lambda uid: {})
    today = datetime.now(RIGA).date()

    async def fetch(spots, settings):
        return [engure_day(today)]

    async def access(user_id, settings):
        return user_id == 42

    app = webapp.build_app(TOKEN, access, cache=webapp.ForecastCache(fetch=fetch),
                           settings_loader=Settings, spots_loader=lambda s: [ENGURE])

    async def run():
        async with TestClient(TestServer(app)) as client:
            header = webapp.INIT_DATA_HEADER
            r = await client.get("/api/forecast")
            assert r.status == 401
            r = await client.get("/api/forecast", headers={header: signed(user_id=7)})
            assert r.status == 403
            r = await client.get("/api/forecast?day=9", headers={header: signed()})
            assert r.status == 400
            r = await client.get("/api/forecast?day=0", headers={header: signed()})
            assert r.status == 200
            assert r.headers["Cache-Control"] == "no-store"
            body = await r.json()
            assert body["spots"][0]["name"] == "Engure"
            assert body["spots"][0]["windows"][0]["start"] == "10:00"
            r = await client.get("/")
            assert r.status == 200
            assert "default-src 'self'" in r.headers["Content-Security-Policy"]
            assert "static/app.js" in await r.text()
            assert (await client.get("/static/app.js")).status == 200
            assert (await client.get("/static/app.css")).status == 200
            assert (await client.get("/static/../webapp.py")).status in (403, 404)
            assert await (await client.get("/healthz")).text() == "ok"

    asyncio.run(run())


# --- bot side ----------------------------------------------------------------------

def test_webapp_markup_private_group_and_off():
    on = Settings(webapp_url="https://kite.example.com", webapp_short_name="prognoze")
    private = handlers.webapp_markup(on, 42, "kitebot")
    assert private.inline_keyboard[0][0].web_app.url == "https://kite.example.com"
    group = handlers.webapp_markup(on, -100, "kitebot", start="d0")
    assert group.inline_keyboard[0][0].url == "https://t.me/kitebot/prognoze?startapp=d0"
    assert handlers.webapp_markup(Settings(webapp_url="https://k.example"), -100, "kitebot") is None
    assert handlers.webapp_markup(Settings(), 42, "kitebot") is None
    # a private chat without webapp_url still gets the t.me link
    link_only = handlers.webapp_markup(Settings(webapp_short_name="prognoze"), 42, "kitebot")
    assert link_only.inline_keyboard[0][0].url == "https://t.me/kitebot/prognoze"


def test_app_command_and_help_only_once_configured():
    off, on = Settings(), Settings(webapp_url="https://kite.example.com")
    assert "app" not in [c.command for c in handlers.bot_commands(off)]
    assert "app" in [c.command for c in handlers.bot_commands(on)]
    assert "/app" not in handlers.help_text(off)
    assert "/app" in handlers.help_text(on)
    assert handlers.help_text(off).count("\n") == handlers.HELP_LV.count("\n") - 1


def test_digest_button_goes_on_the_last_part_only():
    sent: list = []

    class Bot:
        async def send_message(self, **kwargs):
            sent.append(kwargs.get("reply_markup"))

    class Context:
        bot = Bot()

    markup = object()
    asyncio.run(handlers._send_digest(Context(), Subscription(chat_id=1), ["a", "b"], markup))
    assert sent == [None, markup]


def test_daily_job_attaches_the_app_button(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "FORECAST_LOG_FILE", tmp_path / "forecast_log.json")
    sent: list = []

    class Bot:
        username = "kitebot"

        async def send_message(self, **kwargs):
            sent.append(kwargs)

    class Context:
        bot = Bot()

    async def windy(spots, settings, robust=False):
        return [engure_day(datetime.now(RIGA).date())]

    async def nobody(settings, update_records):
        return None, "neviens nebrauca"

    monkeypatch.setattr(config, "load_subscriptions", lambda: [Subscription(chat_id=-100)])
    monkeypatch.setattr(config, "load_spots", lambda settings=None: [ENGURE])
    monkeypatch.setattr(config, "load_settings", lambda: Settings(webapp_short_name="prognoze"))
    monkeypatch.setattr(handlers, "gather_results", windy)
    monkeypatch.setattr(handlers, "build_woo_section", nobody)
    asyncio.run(handlers.daily_job(Context()))
    [message] = sent
    assert message["reply_markup"].inline_keyboard[0][0].url == "https://t.me/kitebot/prognoze"


# --- config ------------------------------------------------------------------------

def _settings_from(tmp_path, monkeypatch, text: str) -> Settings:
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_FILE", path)
    return config.load_settings()


def test_webapp_settings_parse(tmp_path, monkeypatch):
    s = _settings_from(tmp_path, monkeypatch,
                       'webapp_port: 8080\nwebapp_url: "https://kite.example.com/"\n'
                       "webapp_short_name: prognoze\n")
    assert (s.webapp_port, s.webapp_url, s.webapp_short_name) == (
        8080, "https://kite.example.com", "prognoze")
    assert s.webapp_host == "0.0.0.0"
    assert _settings_from(tmp_path, monkeypatch, "timezone: Europe/Riga\n").webapp_port == 0


@pytest.mark.parametrize("text", [
    'webapp_url: "http://kite.example.com"\n',
    "webapp_short_name: no spaces\n",
    "webapp_port: 70000\n",
    "webapp_port: abc\n",
])
def test_webapp_settings_reject_bad_values(tmp_path, monkeypatch, text):
    with pytest.raises(SystemExit):
        _settings_from(tmp_path, monkeypatch, text)

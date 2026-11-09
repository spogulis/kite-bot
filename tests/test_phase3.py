"""Phase 3: rider session log and leaderboard, the spot editor API, and the
map digest."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

pytest.importorskip("aiohttp")

from kitebot import config, handlers, webapp, woo  # noqa: E402
from kitebot.analysis import Window  # noqa: E402
from kitebot.config import Settings, Spot, Subscription  # noqa: E402
from kitebot.messages import SpotResult  # noqa: E402
from test_webapp import TOKEN, signed  # noqa: E402

RIGA = ZoneInfo("Europe/Riga")
DAY = datetime(2026, 11, 9, tzinfo=RIGA)


# --- session log ------------------------------------------------------------------

def test_session_entries_merge_both_apps():
    riders = [{"name": "Alfa", "record_height_m": 9.0, "ids": {"woo": "a", "surfr": "1"}},
              {"name": "Beta", "record_height_m": 5.0, "ids": {"surfr": "2"}},
              {"name": "Gamma", "record_height_m": 4.0, "ids": {"woo": "g"}}]
    stats = {
        "woo:a": {"height_m": 8.4, "distance_m": 22100, "spot_name": "Engures Mols",
                  "kite": "12m Rebel"},
        "surfr:1": {"height_m": 8.9, "jump_distance_m": 30.0, "spot_name": "Engure",
                    "spot_country": "LV"},
        "surfr:2": {"jump_distance_m": 12.0},
    }
    entries = woo.session_entries(riders, stats, ["Engure"])
    assert [e["name"] for e in entries] == ["Alfa", "Beta"]  # Gamma did not ride
    alfa, beta = entries
    assert (alfa["height_m"], alfa["jump_distance_m"], alfa["distance_km"]) == (8.9, 30.0, 22.1)
    assert alfa["kite"] == "12m Rebel" and alfa["spot"]
    assert beta["height_m"] is None and beta["distance_km"] is None and beta["kite"] is None


def test_sessions_are_kept_per_day(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SESSIONS_FILE", tmp_path / "sessions.json")
    assert config.load_sessions() == {}
    config.record_sessions("2026-11-08", [{"name": "Alfa", "height_m": 8.0}])
    config.record_sessions("2026-11-07", [{"name": "Alfa", "height_m": 7.0}])
    config.record_sessions("2026-11-08", [{"name": "Alfa", "height_m": 8.5}])  # rerun replaces
    assert list(config.load_sessions()) == ["2026-11-07", "2026-11-08"]
    assert config.load_sessions()["2026-11-08"][0]["height_m"] == 8.5


def test_daily_recap_logs_sessions_but_testdigest_does_not(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SESSIONS_FILE", tmp_path / "sessions.json")
    monkeypatch.setattr(config, "RIDERS_FILE", tmp_path / "riders.json")
    monkeypatch.setattr(config, "FORECAST_LOG_FILE", tmp_path / "forecast_log.json")
    monkeypatch.setattr(config, "load_riders", lambda: [
        {"name": "Alfa", "record_height_m": 9.0, "ids": {"woo": "a"}}])
    monkeypatch.setattr(config, "save_riders", lambda riders: None)
    monkeypatch.setattr(config, "load_spots", lambda settings=None: [])

    async def day_stats(token, start, end, ids):
        return {"a": {"height_m": 7.5}}

    monkeypatch.setattr(woo, "day_stats", day_stats)
    asyncio.run(handlers.build_woo_section(Settings(), update_records=False))
    assert config.load_sessions() == {}
    asyncio.run(handlers.build_woo_section(Settings(), update_records=True))
    [(day, entries)] = config.load_sessions().items()
    assert entries[0]["name"] == "Alfa" and entries[0]["height_m"] == 7.5


def test_riders_payload():
    riders = [{"name": "Beta", "record_height_m": 6.1, "ids": {"woo": "b"}},
              {"name": "Alfa", "record_height_m": 11.7, "ids": {"woo": "a"}}]
    sessions = {
        "2026-11-01": [{"name": "Alfa", "height_m": 10.2}, {"name": "Beta", "height_m": 5.0}],
        "2026-11-08": [{"name": "Alfa", "height_m": 11.7}, {"name": "Gone", "height_m": 3.0},
                       {"name": "Beta", "height_m": None, "jump_distance_m": 12.0}],
    }
    out = webapp.riders_payload(riders, sessions)
    assert [r["name"] for r in out["riders"]] == ["Alfa", "Beta"]
    assert out["riders"][0]["new_record"] and not out["riders"][1]["new_record"]
    assert out["series"] == [{"name": "Alfa", "points": [["2026-11-01", 10.2], ["2026-11-08", 11.7]]},
                             {"name": "Beta", "points": [["2026-11-01", 5.0]]}]
    assert out["latest"]["date"] == "2026-11-08"
    assert [e["name"] for e in out["latest"]["entries"]] == ["Alfa", "Beta"]  # untracked dropped
    assert out["since"] == "2026-11-01"
    empty = webapp.riders_payload(riders, {})
    assert empty["series"] == [] and empty["latest"]["date"] is None and empty["since"] is None


# --- spot editor -------------------------------------------------------------------

GOOD = {"name": "Jauns  spots", "lat": 57.1234567, "lon": 23.2, "min_wind": 6,
        "max_wind": 20, "sectors": [[337.5, 22.5], [160, 190]], "cell": "sea"}


def test_parse_spot():
    fields = webapp.parse_spot(GOOD)
    assert fields["name"] == "Jauns spots" and fields["lat"] == 57.12346
    assert fields["sectors"] == [[337.5, 22.5], [160.0, 190.0]] and fields["original"] is None
    for bad in ({**GOOD, "name": " "}, {**GOOD, "name": "x" * 26}, {**GOOD, "min_wind": 20},
                {**GOOD, "lat": 91}, {**GOOD, "cell": "air"}, {**GOOD, "sectors": [[10, 10]]},
                {**GOOD, "sectors": [[0, 10]] * 17}, {**GOOD, "name": "a:b"}, "nope"):
        assert webapp.parse_spot(bad) is None


@pytest.fixture
def spot_files(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "SPOTS_FILE", tmp_path / "spots.yaml")
    monkeypatch.setattr(config, "SUBSCRIPTIONS_FILE", tmp_path / "subscriptions.json")
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.yaml")
    config.save_spots([Spot(name="Engure", lat=57.16, lon=23.23, model="gfs_seamless"),
                       Spot(name="Mērsrags", lat=57.33, lon=23.13)])
    config.add_subscription(Subscription(chat_id=-100, spots=("Engure", "Mērsrags")))
    return tmp_path


def test_save_spot_add_rename_and_clash(spot_files):
    assert webapp.save_spot(webapp.parse_spot(GOOD)) is None
    assert [s.name for s in config.load_spots()][-1] == "Jauns spots"
    clash = webapp.parse_spot({**GOOD, "name": "engure"})
    assert webapp.save_spot(clash) == "Spots ar šādu nosaukumu jau ir."
    rename = webapp.parse_spot({**GOOD, "name": "Engure Mols", "original": "Engure",
                                "lat": 57.16, "lon": 23.23})
    assert webapp.save_spot(rename) is None
    spot = next(s for s in config.load_spots() if s.name == "Engure Mols")
    assert spot.model == "gfs_seamless"  # kept from the old spot
    assert spot.good_directions == [[337.5, 22.5], [160.0, 190.0]]
    assert config.load_subscriptions()[0].spots == ("Engure Mols", "Mērsrags")
    gone = webapp.parse_spot({**GOOD, "original": "Nav tāda"})
    assert webapp.save_spot(gone) == "Šāda spota vairs nav."


def test_spot_endpoints_need_an_admin(spot_files):
    from aiohttp.test_utils import TestClient, TestServer

    async def access(user_id, settings):
        return True

    app = webapp.build_app(TOKEN, access, settings_loader=lambda: Settings(admin_user_ids=[42]),
                           spots_loader=config.load_spots)

    async def run():
        async with TestClient(TestServer(app)) as client:
            admin = {webapp.INIT_DATA_HEADER: signed(user_id=42)}
            member = {webapp.INIT_DATA_HEADER: signed(user_id=7)}
            body = await (await client.get("/api/spots", headers=member)).json()
            assert body["is_admin"] is False and [s["name"] for s in body["spots"]] == ["Engure", "Mērsrags"]
            assert (await client.get("/api/spots", headers=admin)).status == 200
            r = await client.post("/api/spots/save", json=GOOD, headers=member)
            assert r.status == 403
            r = await client.post("/api/spots/save", json=GOOD, headers=admin)
            assert r.status == 200 and len((await r.json())["spots"]) == 3
            r = await client.post("/api/spots/save", json={**GOOD, "name": "Engure"}, headers=admin)
            assert r.status == 409
            r = await client.post("/api/spots/save", json={**GOOD, "min_wind": 30}, headers=admin)
            assert r.status == 400
            r = await client.post("/api/spots/delete", json={"name": "Mērsrags"}, headers=member)
            assert r.status == 403
            r = await client.post("/api/spots/delete", json={"name": "Mērsrags"}, headers=admin)
            assert r.status == 200
            assert [s.name for s in config.load_spots()] == ["Engure", "Jauns spots"]
            r = await client.post("/api/spots/delete", json={"name": "Mērsrags"}, headers=admin)
            assert r.status == 404

    asyncio.run(run())


def test_csp_allows_the_map_and_sends_only_the_origin():
    assert "https://unpkg.com" in webapp.CSP and "https://tile.openstreetmap.org" in webapp.CSP
    assert webapp.REFERRER_POLICY == "strict-origin-when-cross-origin"


# --- map digest -------------------------------------------------------------------------

def window(a, b, foil=False):
    return Window(start=DAY.replace(hour=a), end=DAY.replace(hour=b), min_speed=6, max_speed=8,
                  max_gust=11, direction=315, foil_only=foil)


GULF = [SpotResult(spot=Spot(name="Engure", lat=57.16, lon=23.23), windows=[window(10, 14)]),
        SpotResult(spot=Spot(name="Mērsrags", lat=57.335, lon=23.13),
                   windows=[window(9, 11, foil=True), window(13, 18)]),
        SpotResult(spot=Spot(name="Salacgrīva", lat=57.75, lon=24.35), windows=[window(12, 15, True)]),
        SpotResult(spot=Spot(name="Saulkrasti", lat=57.26, lon=24.40)),
        SpotResult(spot=Spot(name="Bez datiem", lat=57.0, lon=24.0), error="down")]


def test_spot_labels():
    from kitebot import digestmap
    labels = [digestmap.spot_label(r)[0] for r in GULF]
    assert labels == ["Engure 10–14", "Mērsrags 13–18 +1", "Salacgrīva 12–15 matracis",
                      "Saulkrasti —", "Bez datiem ?"]


def test_map_needs_spots_inside_the_coastline():
    from kitebot import digestmap
    assert digestmap.covered([r.spot for r in GULF])
    assert not digestmap.covered([Spot(name="Podersdorf", lat=47.85, lon=16.84)])
    assert not digestmap.covered([])


def test_map_renders_a_png():
    pytest.importorskip("PIL")
    from kitebot import digestmap
    png = digestmap.render(GULF, "Pr 09.11 · šodien")
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 10_000
    from PIL import Image
    import io
    assert Image.open(io.BytesIO(png)).size == (digestmap.WIDTH, digestmap.HEIGHT)
    assert digestmap.render([SpotResult(spot=Spot(name="Rust", lat=47.8, lon=16.7))], "x") is None


def test_photo_caption_folds_short_text_and_refuses_long():
    caption = handlers.photo_caption(["Labrīt!", "🪁 <b>Kaita prognoze</b>"])
    assert caption == "<blockquote expandable>Labrīt!\n\n🪁 <b>Kaita prognoze</b></blockquote>"
    assert handlers.photo_caption(["x" * 1200]) is None


class PhotoBot:
    username = "kitebot"

    def __init__(self):
        self.calls = []

    async def send_photo(self, **kwargs):
        self.calls.append(("photo", kwargs))

    async def send_message(self, **kwargs):
        self.calls.append(("text", kwargs))


def test_digest_with_a_map(monkeypatch):
    bot = PhotoBot()

    class Context:
        pass

    Context.bot = bot
    markup = object()
    asyncio.run(handlers._send_digest(Context(), Subscription(chat_id=-1), ["Labrīt!", "Engure"],
                                      markup, photo=b"png"))
    [(kind, sent)] = bot.calls
    assert kind == "photo" and sent["reply_markup"] is markup
    assert sent["caption"].startswith("<blockquote expandable>Labrīt!")
    bot.calls.clear()
    asyncio.run(handlers._send_digest(Context(), Subscription(chat_id=-1), ["x" * 1200, "y"],
                                      markup, photo=b"png"))
    assert [k for k, _ in bot.calls] == ["photo", "text", "text"]
    assert bot.calls[0][1]["caption"] is None and bot.calls[-1][1]["reply_markup"] is markup


def test_daily_job_sends_the_map_only_when_switched_on(monkeypatch, tmp_path):
    pytest.importorskip("PIL")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "FORECAST_LOG_FILE", tmp_path / "forecast_log.json")

    async def gather(spots, settings, robust=False):
        now = datetime.now(RIGA).replace(minute=0, second=0, microsecond=0)
        start = now.replace(hour=12)
        w = Window(start=start, end=start.replace(hour=15), min_speed=6, max_speed=8,
                   max_gust=11, direction=315)
        return [SpotResult(spot=r.spot, windows=[w] if i == 0 else []) for i, r in enumerate(GULF[:4])]

    async def nobody(settings, update_records):
        return None, "neviens nebrauca"

    monkeypatch.setattr(handlers, "gather_results", gather)
    monkeypatch.setattr(handlers, "build_woo_section", nobody)
    monkeypatch.setattr(config, "load_subscriptions", lambda: [Subscription(chat_id=-100)])
    monkeypatch.setattr(config, "load_spots", lambda settings=None: [r.spot for r in GULF[:4]])

    class Context:
        bot = PhotoBot()

    for on, expected in ((False, ["text"]), (True, ["photo"])):
        Context.bot.calls.clear()
        monkeypatch.setattr(config, "load_settings", lambda on=on: Settings(digest_map=on))
        asyncio.run(handlers.daily_job(Context()))
        assert [k for k, _ in Context.bot.calls] == expected


def test_digest_map_setting(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text("digest_map: true\n", encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_FILE", path)
    assert config.load_settings().digest_map is True
    path.write_text("timezone: Europe/Riga\n", encoding="utf-8")
    assert config.load_settings().digest_map is False


def test_coastline_asset_is_sane():
    data = json.loads((config.ROOT / "kitebot" / "assets" / "baltic_land.json").read_text("utf-8"))
    assert "Natural Earth" in data["source"] and len(data["rings"]) > 50
    lon0, lat0, lon1, lat1 = data["bbox"]
    assert all(lon0 <= x <= lon1 and lat0 <= y <= lat1 for ring in data["rings"] for x, y in ring)

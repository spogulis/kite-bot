from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from kitebot.analysis import HourPoint, circular_mean, compass, find_windows, in_sectors
from kitebot.config import Spot, normalize_wind_unit

TZ = ZoneInfo("Europe/Vienna")


def hp(hour: int, speed: float = 15.0, direction: float = 315.0, gusts: "float | None" = None) -> HourPoint:
    return HourPoint(
        time=datetime(2026, 8, 21, hour, tzinfo=TZ),
        speed=speed,
        gusts=speed + 5 if gusts is None else gusts,
        direction=direction,
    )


def spot(**overrides) -> Spot:
    defaults = dict(
        name="Test", lat=47.85, lon=16.84,
        min_wind=12, max_wind=38,
        good_directions=[[290, 20], [110, 170]],
    )
    defaults.update(overrides)
    return Spot(**defaults)


def windows(points, s, **kwargs):
    args = dict(min_hours=2, day_start=8, day_end=20)
    args.update(kwargs)
    return find_windows(points, s, **args)


def test_in_sectors_simple():
    assert in_sectors(140, [[110, 170]])
    assert not in_sectors(200, [[110, 170]])


def test_in_sectors_wraps_through_north():
    assert in_sectors(350, [[290, 20]])
    assert in_sectors(10, [[290, 20]])
    assert not in_sectors(45, [[290, 20]])


def test_in_sectors_empty_means_any():
    assert in_sectors(123, [])


def test_in_sectors_full_circle():
    assert in_sectors(200, [[0, 360]])


def test_compass():
    assert compass(0) == "N"
    assert compass(315) == "NW"
    assert compass(100) == "E"
    assert compass(170) == "S"
    assert compass(359) == "N"


def test_circular_mean_wraps():
    assert circular_mean([350, 10]) == pytest.approx(0.0, abs=1e-9)


def test_finds_contiguous_window():
    points = [hp(h) for h in range(10, 15)]
    result = windows(points, spot())
    assert len(result) == 1
    w = result[0]
    assert (w.start.hour, w.end.hour) == (10, 15)
    assert w.hours == 5


def test_short_blip_ignored():
    assert windows([hp(10)], spot()) == []


def test_gap_splits_windows():
    points = [hp(10), hp(11), hp(13), hp(14)]
    result = windows(points, spot())
    assert [(w.start.hour, w.end.hour) for w in result] == [(10, 12), (13, 15)]


def test_wrong_direction_breaks_window():
    points = [hp(10), hp(11, direction=45), hp(12)]
    assert windows(points, spot()) == []


def test_too_light_or_too_strong_excluded():
    assert windows([hp(10, speed=8), hp(11, speed=8)], spot()) == []
    assert windows([hp(10, speed=45), hp(11, speed=45)], spot()) == []


def test_night_hours_ignored():
    assert windows([hp(5), hp(6), hp(7)], spot()) == []


def test_window_stats():
    points = [hp(10, speed=14, gusts=20), hp(11, speed=16, gusts=26)]
    w = windows(points, spot())[0]
    assert w.min_speed == 14
    assert w.max_speed == 16
    assert w.max_gust == 26


def test_window_splits_by_wind_band():
    # 6-8 m/s until 13:00, then 10-12 m/s: different kite sizes, two lines
    points = [
        hp(11, speed=6, gusts=9), hp(12, speed=8, gusts=9),
        hp(13, speed=10, gusts=14), hp(14, speed=11, gusts=14), hp(15, speed=12, gusts=14),
    ]
    result = windows(points, spot(min_wind=5, max_wind=20), band=3)
    assert [(w.start.hour, w.end.hour, w.min_speed, w.max_speed) for w in result] == [
        (11, 13, 6, 8), (13, 16, 10, 12),
    ]
    # band=0 disables splitting
    result = windows(points, spot(min_wind=5, max_wind=20), band=0)
    assert len(result) == 1


def test_spot_defaults_follow_unit():
    s = Spot.from_dict({"name": "X", "lat": 1, "lon": 2}, unit="ms")
    assert (s.min_wind, s.max_wind) == (6.0, 20.0)
    s = Spot.from_dict({"name": "X", "lat": 1, "lon": 2}, unit="kn")
    assert (s.min_wind, s.max_wind) == (12.0, 38.0)


def test_spot_converts_legacy_knots_fields():
    s = Spot.from_dict({"name": "X", "lat": 1, "lon": 2, "min_knots": 12, "max_knots": 38}, unit="ms")
    assert s.min_wind == pytest.approx(6.17, abs=0.01)
    assert s.max_wind == pytest.approx(19.55, abs=0.01)


def test_direction_toggle_roundtrip():
    from kitebot.analysis import sectors_from_toggles, toggles_from_sectors
    toggles = [True, False, False, False, False, False, True, True]  # N, W, NW
    sectors = sectors_from_toggles(toggles)
    assert toggles_from_sectors(sectors) == toggles


def test_toggling_one_direction_keeps_another_narrowed_sector():
    """A hand-tuned sector must survive editing an unrelated direction.

    36. līnija faces north, so its west sector is cut to 265°–292.5° to keep
    side-offshore wind out. Toggling north off used to rebuild every sector
    from the full octants and silently widen west back to 247.5°.
    """
    from kitebot.analysis import sectors_from_toggles, toggles_from_sectors
    current = [[337.5, 22.5], [22.5, 67.5], [265.0, 292.5]]
    toggles = toggles_from_sectors(current)
    assert toggles == [True, True, False, False, False, False, True, False]
    toggles[0] = False  # user switches north off
    rebuilt = sectors_from_toggles(toggles, current)
    assert [265.0, 292.5] in rebuilt
    assert [247.5, 292.5] not in rebuilt


def test_re_enabling_a_direction_restores_the_full_octant():
    from kitebot.analysis import sectors_from_toggles
    current = [[265.0, 292.5]]
    off = sectors_from_toggles([False] * 8, current)
    assert off == []
    back_on = sectors_from_toggles([False] * 6 + [True, False], off)
    assert back_on == [[247.5, 292.5]]


def test_toggling_off_splits_a_multi_octant_sector():
    """[0, 180] covers N..S; switching east off must not keep east allowed."""
    from kitebot.analysis import sectors_from_toggles, toggles_from_sectors
    current = [[0.0, 180.0]]
    toggles = toggles_from_sectors(current)
    assert toggles == [True, True, True, True, True, False, False, False]
    assert sectors_from_toggles(toggles, current) == [[0.0, 180.0]]
    toggles[2] = False  # east off
    rebuilt = sectors_from_toggles(toggles, current)
    assert [0.0, 180.0] not in rebuilt
    assert toggles_from_sectors(rebuilt) == toggles


def test_narrowed_bounds_only_flags_sub_octant_sectors():
    from kitebot.analysis import narrowed_bounds
    assert narrowed_bounds(6, [[265.0, 292.5]]) == (265.0, 292.5)
    assert narrowed_bounds(6, [[247.5, 292.5]]) is None
    assert narrowed_bounds(0, [[0.0, 180.0]]) is None
    assert narrowed_bounds(0, [[350.0, 10.0]]) == (350.0, 10.0)


def test_direction_words_latvian():
    from kitebot.messages import direction_word
    assert direction_word(0) == "ziemeļu vējš"
    assert direction_word(350) == "ziemeļu vējš"
    assert direction_word(315) == "ziemeļrietumu vējš"
    assert direction_word(180) == "dienvidu vējš"


def test_describe_directions_words_and_degrees():
    from kitebot.analysis import sectors_from_toggles
    from kitebot.messages import describe_directions
    assert describe_directions([]) == "jebkurš virziens"
    toggles = [False] * 8
    toggles[6] = toggles[7] = True  # W, NW
    assert describe_directions(sectors_from_toggles(toggles)) == "Rietumi, Ziemeļrietumi"
    assert "290°–20°" in describe_directions([[290, 20]])
    # a narrowed sector spells itself out without hiding the named ones
    assert describe_directions([[337.5, 22.5], [265.0, 292.5]]) == "Ziemeļi, 265°–292°"


def test_subscription_spot_filter_roundtrip(tmp_path, monkeypatch):
    from kitebot import config
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "SUBSCRIPTIONS_FILE", tmp_path / "subs.json")

    sub = config.Subscription(chat_id=1, thread_id=None)
    assert config.add_subscription(sub)
    # same chat+thread counts as already subscribed, regardless of spot filter
    assert not config.add_subscription(config.Subscription(chat_id=1, thread_id=None, spots=("X",)))

    updated = config.set_subscription_spots(1, None, ("Engure", "Pāvilosta"))
    assert updated.spots == ("Engure", "Pāvilosta")
    assert config.find_subscription(1, None).spots == ("Engure", "Pāvilosta")

    assert config.remove_subscription(sub)
    assert config.find_subscription(1, None) is None


def test_woo_normalize():
    from kitebot.woo import normalize
    assert normalize("Āķīšu Ņša 🇱🇻") == "akisu nsa 🇱🇻"
    assert "zagata" in normalize("Žagata Ūdens")


def test_woo_summarize_records_and_lines():
    from kitebot.woo import summarize
    riders = [
        {"name": "Alfa", "record_height_m": 12.0, "ids": {"woo": "a"}},
        {"name": "Beta", "record_height_m": 10.0, "ids": {"woo": "b"}},
        {"name": "Gamma", "record_height_m": 16.6, "ids": {"woo": "c"}},
    ]
    stats = {
        "woo:a": {"distance_m": 34200, "height_m": 12.6},  # rode, new record
        "woo:b": {"distance_m": 21000, "height_m": 8.0},   # rode, no record
    }
    lines, updated, changed = summarize(riders, stats)
    assert changed
    assert len(lines) == 2
    assert "34,2 km" in lines[0] and "12,6 m" in lines[0] and "JAUNS REKORDS (+0,6 m)" in lines[0]
    assert "21,0 km" in lines[1] and "rekords" not in lines[1]
    assert updated[0]["record_height_m"] == 12.6
    assert updated[1]["record_height_m"] == 10.0
    assert updated[2]["record_height_m"] == 16.6  # did not ride, unchanged


def test_summarize_merged_rider_reconciles_apps():
    from kitebot.woo import summarize
    riders = [{"name": "Alfa", "record_height_m": 13.6,
               "ids": {"woo": "uuid-1", "surfr": "54321"}}]
    stats = {
        "woo:uuid-1": {"distance_m": 30000, "height_m": 14.2},
        "surfr:54321": {"jump_distance_m": 31.0, "height_m": 13.1},
    }
    lines, updated, changed = summarize(riders, stats)
    assert len(lines) == 1
    # best of each metric, both jump readings shown, record from the max
    assert "🛣️ 30,0 km" in lines[0]
    assert "⬆️ 14,2 m (WOO 14,2 / Surfr 13,1)" in lines[0]
    assert "↔️ 31,0 m" in lines[0]
    assert "JAUNS REKORDS (+0,6 m)" in lines[0]
    assert changed and updated[0]["record_height_m"] == 14.2


def test_summarize_orders_metrics_height_then_jump_then_ridden():
    from kitebot.woo import summarize
    riders = [{"name": "Alfa", "record_height_m": 20.0,
               "ids": {"woo": "w", "surfr": "s"}}]
    stats = {"woo:w": {"distance_m": 16100}, "surfr:s": {"jump_distance_m": 30.0,
                                                        "height_m": 4.7}}
    lines, _, _ = summarize(riders, stats)
    assert lines[0] == "🏄 Alfa — ⬆️ 4,7 m · ↔️ 30,0 m · 🛣️ 16,1 km"


def test_summarize_ranks_lines_by_jump_height():
    """Config order must not leak into the section: highest jump leads, and a
    rider with no jump reading sinks below everyone who has one."""
    from kitebot.woo import summarize
    riders = [
        {"name": "Zems", "record_height_m": 9.9, "ids": {"woo": "a"}},
        {"name": "Bezleciena", "record_height_m": 9.9, "ids": {"woo": "b"}},
        {"name": "Augsts", "record_height_m": 9.9, "ids": {"woo": "c"}},
    ]
    stats = {
        "woo:a": {"distance_m": 40000, "height_m": 4.7},
        "woo:b": {"distance_m": 99000},  # rode far, but no jump recorded
        "woo:c": {"distance_m": 1000, "height_m": 11.7},
    }
    lines, updated, _ = summarize(riders, stats)
    assert [line.split(" — ")[0] for line in lines] == [
        "🏄 Augsts", "🏄 Zems", "🏄 Bezleciena"]
    # records are written back in config order regardless of how lines rank
    assert [r["name"] for r in updated] == ["Zems", "Bezleciena", "Augsts"]


def test_summarize_surfr_jump_distance_is_not_km():
    """Surfr's 'distance' leaderboard is the longest jump in metres; a 30 m
    jump must not surface as 30 km ridden."""
    from kitebot.woo import summarize
    riders = [{"name": "Kristina", "record_height_m": 3.0, "ids": {"surfr": "9"}}]
    stats = {"surfr:9": {"jump_distance_m": 30.0, "height_m": 2.4}}
    lines, _, changed = summarize(riders, stats)
    assert not changed
    assert "km" not in lines[0]
    assert "⬆️ 2,4 m" in lines[0] and "↔️ 30,0 m" in lines[0]


def test_summarize_adds_the_country_of_a_spot_abroad():
    """The Kristina case: a session on a day every configured spot forecast no
    wind reads as a forecast bug unless the line says where it happened."""
    from kitebot.woo import summarize
    riders = [{"name": "Kristina", "record_height_m": 3.0, "ids": {"surfr": "9"}}]
    stats = {"surfr:9": {"jump_distance_m": 35.0, "spot_name": "Ria de Alvor",
                         "spot_country": "PT"}}
    lines, _, _ = summarize(riders, stats, ["Vecāķi", "Bernati"])
    assert lines[0] == "🏄 Kristina — ↔️ 35,0 m · 📍 Ria de Alvor (PT)"


def test_summarize_names_a_home_spot_without_its_country():
    """Home sessions show the spot too, minus the country; Surfr's own
    spelling (no diacritics) must still count as the configured spot."""
    from kitebot.woo import summarize
    riders = [{"name": "Kristina", "record_height_m": 3.0, "ids": {"surfr": "9"}}]
    stats = {"surfr:9": {"height_m": 2.4, "spot_name": "Vecaki", "spot_country": "LV"}}
    lines, _, _ = summarize(riders, stats, ["Vecāķi"])
    assert lines[0] == "🏄 Kristina — ⬆️ 2,4 m · 📍 Vecaki"


def test_summarize_shows_the_woo_kite_and_spot():
    """WOO names the kite and spot of the best jump, never a country — and
    spells the spot its own way ("Engures Mols" for the configured "Engure")."""
    from kitebot.woo import summarize
    riders = [{"name": "Alfa", "record_height_m": 18.7, "ids": {"woo": "a"}}]
    stats = {"woo:a": {"distance_m": 1960.0, "height_m": 7.6,
                       "spot_name": "Engures Mols", "kite": "15m Flysurfer Sonic 5"}}
    lines, _, _ = summarize(riders, stats, ["Engure"])
    assert lines[0] == ("🏄 Alfa — ⬆️ 7,6 m · 🛣️ 2,0 km · "
                        "🪁 15m Flysurfer Sonic 5 · 📍 Engures Mols")


def test_summarize_merged_rider_takes_the_spot_of_the_higher_jump():
    """Both apps may name the spot; the line follows the jump it headlines,
    not whichever app the rider was added with first."""
    from kitebot.woo import summarize
    riders = [{"name": "Alfa", "record_height_m": 20.0, "ids": {"woo": "w", "surfr": "s"}}]
    stats = {"woo:w": {"height_m": 5.9, "spot_name": "Engures Mols", "kite": "12m Core XR PRO"},
             "surfr:s": {"height_m": 7.6, "spot_name": "Engure", "spot_country": "LV"}}
    lines, _, _ = summarize(riders, stats, ["Engure"])
    assert lines[0].endswith(" · 🪁 12m Core XR PRO · 📍 Engure")


def test_summarize_spot_comes_before_a_new_record():
    """The record marker is the loudest part of a line and stays last."""
    from kitebot.woo import summarize
    riders = [{"name": "Kristina", "record_height_m": 2.0, "ids": {"surfr": "9"}}]
    stats = {"surfr:9": {"height_m": 4.0, "spot_name": "Dakhla", "spot_country": "MA"}}
    lines, _, changed = summarize(riders, stats, ["Vecāķi"])
    assert changed
    assert lines[0].index("📍") < lines[0].index("REKORDS")


def test_summarize_without_known_spots_still_marks_surfr_spots():
    """Without a spot list every spot counts as abroad; passing none must not crash."""
    from kitebot.woo import summarize
    riders = [{"name": "Alfa", "record_height_m": 9.0, "ids": {"woo": "a", "surfr": "s"}}]
    stats = {"woo:a": {"distance_m": 12000, "height_m": 5.0},
             "surfr:s": {"height_m": 5.0, "spot_name": "Tarifa", "spot_country": "ES"}}
    lines, _, _ = summarize(riders, stats)
    assert "📍 Tarifa (ES)" in lines[0]


def test_woo_day_stats_takes_spot_and_kite_from_the_jump(monkeypatch):
    """Only WOO's jump board carries spot and gear; its distance board is a
    day total with "spot": null, and country_code is the rider's, not the spot's."""
    import asyncio

    from kitebot import woo

    boards = {
        "total_distance": [{"user": {"id": "a"}, "score": 1960.0, "spot": None,
                            "gear": None, "country_code": "LV"}],
        "height": [{"user": {"id": "a"}, "score": 7.6, "spot": "Engures Mols",
                    "gear": "15m Flysurfer Sonic 5", "country_code": "LV"},
                   {"user": {"id": "x"}, "score": 5.0, "spot": "Tarifa", "gear": "9m Rebel"}],
    }

    async def fake_page(client, token, feature, game_type, offset, start=None, end=None):
        return {"status": "ok", "size": len(boards[feature]), "items": boards[feature]}

    monkeypatch.setattr(woo, "_page", fake_page)
    stats = asyncio.run(woo.day_stats("token", 0, 86400, {"a"}))
    assert stats == {"a": {"distance_m": 1960.0, "height_m": 7.6,
                           "spot_name": "Engures Mols", "kite": "15m Flysurfer Sonic 5"}}


def test_surfr_day_stats_keeps_the_spot_of_the_highest_jump(monkeypatch):
    """Two sessions in a day: the spot shown is where the best jump was, even
    though the jump-distance board ranks the other session first."""
    import asyncio

    from kitebot import surfr

    boards = {
        "height": [{"user": {"id": 9}, "value": 4.0, "spotName": "Engure", "spotCountry": "LV"},
                   {"user": {"id": 9}, "value": 2.0, "spotName": "Bērzciems", "spotCountry": "LV"}],
        "distance": [{"user": {"id": 9}, "value": 30.0, "spotName": "Bērzciems",
                      "spotCountry": "LV"}],
    }

    async def fake_page(client, token, category, period, page, date_from=None, date_to=None):
        return boards[category] if page == 0 else []

    monkeypatch.setattr(surfr, "_page", fake_page)
    stats = asyncio.run(surfr.day_stats("token", "2026-09-28", {"9"}))
    assert stats == {"9": {"height_m": 4.0, "jump_distance_m": 30.0,
                           "spot_name": "Engure", "spot_country": "LV"}}


def test_daily_job_windless_morning_sends_only_yesterdays_riders(tmp_path, monkeypatch):
    """Nothing rideable today: no greeting and no empty forecast. Yesterday's
    riders still go out on their own, and with nobody to show the chat hears
    nothing. post_when_no_wind: true brings back the full digest."""
    import asyncio
    import json
    from datetime import timedelta

    from kitebot import config, handlers
    from kitebot.analysis import Window
    from kitebot.config import Settings
    from kitebot.messages import SpotResult

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "FORECAST_LOG_FILE", tmp_path / "forecast_log.json")
    sent: list = []

    class FakeBot:
        async def send_message(self, **kwargs):
            sent.append(kwargs["text"])

    class FakeContext:
        bot = FakeBot()

    async def no_wind(spots, settings, robust=False):
        return [SpotResult(spot=s) for s in spots]

    async def windy(spots, settings, robust=False):
        noon = datetime.now(ZoneInfo(settings.timezone)).replace(
            hour=12, minute=0, second=0, microsecond=0)
        w = Window(start=noon, end=noon + timedelta(hours=3), min_speed=8, max_speed=10,
                   max_gust=13, direction=45)
        return [SpotResult(spot=s, windows=[w]) for s in spots]

    async def someone_rode(settings, update_records):
        return (handlers.Recap(lines=["🏄 Alfa — ⬆️ 7,6 m"], quirk="Kam vakar nebija, ko darīt:"),
                "sadaļa iekļauta")

    async def nobody_rode(settings, update_records):
        return None, "neviens nebrauca"

    monkeypatch.setattr(config, "load_subscriptions", lambda: [config.Subscription(chat_id=1)])
    monkeypatch.setattr(config, "load_spots", lambda settings=None: [spot(name="Engure")])
    monkeypatch.setattr(config, "load_settings", lambda: Settings())

    def morning(gather, build) -> list:
        sent.clear()
        monkeypatch.setattr(handlers, "gather_results", gather)
        monkeypatch.setattr(handlers, "build_woo_section", build)
        asyncio.run(handlers.daily_job(FakeContext()))
        return list(sent)

    assert morning(no_wind, nobody_rode) == []
    # the verdict is saved even on a silent morning, for tomorrow's recap
    today = datetime.now(ZoneInfo("Europe/Riga")).date().isoformat()
    assert json.loads((tmp_path / "forecast_log.json").read_text()) == {today: {"Engure": False}}

    assert morning(no_wind, someone_rode) == ["<i>Kam vakar nebija, ko darīt:</i>\n🏄 Alfa — ⬆️ 7,6 m"]

    [text] = morning(windy, someone_rode)
    assert text.startswith("Labrīt, kaiteri!")
    assert text.endswith("🏆 <b>Vakardienas varoņi</b>\n<i>Kam vakar nebija, ko darīt:</i>\n"
                         "🏄 Alfa — ⬆️ 7,6 m")

    monkeypatch.setattr(config, "load_settings", lambda: Settings(post_when_no_wind=True))
    [text] = morning(no_wind, nobody_rode)
    assert "Nevienā spotā nav braucama vēja." in text


def test_recap_without_a_quirk_keeps_its_title_on_a_windless_morning():
    from kitebot.handlers import Recap
    assert Recap(lines=["🏄 Alfa — ⬆️ 7,6 m"]).on_its_own() == (
        "🏆 <b>Vakardienas varoņi</b>\n🏄 Alfa — ⬆️ 7,6 m")


def test_fooled_the_forecast_counts_only_home_spots_without_a_window():
    from kitebot.woo import fooled_the_forecast
    riders = [{"name": "Alfa", "record_height_m": 9.0, "ids": {"woo": "a"}}]
    verdict = {"Engure": False, "Bērzciems": True}

    def fooled(day, had_window=verdict):
        return fooled_the_forecast(riders, {"woo:a": day}, had_window)

    assert fooled({"height_m": 7.6, "spot_name": "Engures Mols"})      # calm promised there
    assert not fooled({"height_m": 7.6, "spot_name": "Berzciems"})     # the forecast was right
    assert not fooled({"height_m": 3.4, "spot_name": "Ria de Alvor",   # abroad
                       "spot_country": "PT"}, {"Engure": False, "Bērzciems": False})
    assert not fooled({"distance_m": 12000.0})                         # no spot, wind somewhere
    assert fooled({"distance_m": 12000.0}, {"Engure": False, "Bērzciems": False})
    assert not fooled({"height_m": 7.6, "spot_name": "Engures Mols"}, {})  # morning not recorded


def test_forecast_log_keeps_the_last_week(tmp_path, monkeypatch):
    from kitebot import config
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "FORECAST_LOG_FILE", tmp_path / "forecast_log.json")
    for day in range(1, 10):
        config.record_forecast(f"2026-09-{day:02d}", {"Bērzciems": day % 2 == 0})
    days = config.load_forecast_log()
    assert sorted(days) == [f"2026-09-{d:02d}" for d in range(3, 10)]
    assert days["2026-09-04"] == {"Bērzciems": True}


def test_recap_gets_a_quirk_when_someone_rode_where_calm_was_forecast(tmp_path, monkeypatch):
    """The Engure case: yesterday's digest showed no window at Engure, yet a
    rider jumped there — the recap says so. Where the forecast was right, no quirk."""
    import asyncio

    from kitebot import config, handlers, woo
    from kitebot.config import Settings

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "FORECAST_LOG_FILE", tmp_path / "forecast_log.json")
    settings = Settings()
    _, _, yesterday = handlers._yesterday_range(settings)
    config.record_forecast(yesterday, {"Engure": False, "Bērzciems": True})
    monkeypatch.setattr(config, "load_riders", lambda: [
        {"name": "Alfa", "record_height_m": 20.0, "ids": {"woo": "a"}}])
    monkeypatch.setattr(config, "load_spots", lambda settings=None: [
        spot(name="Engure"), spot(name="Bērzciems")])

    def recap_for(spot_name):
        async def day_stats(token, start, end, rider_ids):
            return {"a": {"height_m": 7.6, "spot_name": spot_name}}
        monkeypatch.setattr(woo, "day_stats", day_stats)
        recap, _ = asyncio.run(handlers.build_woo_section(settings, update_records=False))
        return recap

    assert recap_for("Engures Mols").quirk in handlers.FOOLED_FORECAST_LV
    assert recap_for("Bērzciems").quirk == ""


def test_digest_collapses_when_nothing_rideable():
    from kitebot.config import Settings
    from kitebot.messages import SpotResult, build_digest

    empty = [SpotResult(spot=spot(name="A")), SpotResult(spot=spot(name="B"))]
    text = build_digest(empty, Settings())
    assert "Nevienā spotā nav braucama vēja." in text
    assert "A" not in text.replace("Kaita", "")  # no per-spot blocks

    w = windows([hp(10), hp(11)], spot())[0]
    mixed = [SpotResult(spot=spot(name="A"), windows=[w]), SpotResult(spot=spot(name="B"))]
    text = build_digest(mixed, Settings())
    assert "braucama vēja nav" in text  # per-spot line kept for B
    assert "<b>A</b>" in text


def test_riders_legacy_migration_and_merge(tmp_path, monkeypatch):
    import json
    from kitebot import config
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "RIDERS_FILE", tmp_path / "riders.json")

    # both legacy formats migrate to the ids map
    (tmp_path / "riders.json").write_text(json.dumps({"riders": [
        {"woo_id": "abc-uuid", "name": "Vecais", "record_height_m": 5.0},
        {"provider": "surfr", "rider_id": "54321", "name": "Alfa", "record_height_m": 13.6},
    ]}))
    riders = config.load_riders()
    assert riders[0]["ids"] == {"woo": "abc-uuid"}
    assert riders[1]["ids"] == {"surfr": "54321"}

    # adding the same person's WOO id, then merging both entries into one
    assert config.add_rider("woo", "alfa-woo-uuid", "Alfa W", 12.0)
    merged = config.merge_riders("54321"[:8], "alfa-woo"[:8])
    assert merged["ids"] == {"surfr": "54321", "woo": "alfa-woo-uuid"}
    assert merged["record_height_m"] == 13.6
    assert merged["name"] == "Alfa"
    assert len(config.load_riders()) == 2

    # re-adding an app id of a merged person refreshes, never duplicates
    assert not config.add_rider("woo", "alfa-woo-uuid", "whatever", 14.0)
    riders = config.load_riders()
    assert len(riders) == 2
    jurgis = config.find_rider_by_prefix(riders, "54321")
    assert jurgis["record_height_m"] == 14.0 and jurgis["name"] == "Alfa"

    assert config.remove_rider("abc-uuid")
    assert len(config.load_riders()) == 1


def test_normalize_model():
    from kitebot.config import normalize_model
    assert normalize_model("gfs") == "gfs_seamless"
    assert normalize_model("GFS") == "gfs_seamless"
    assert normalize_model("best") == "best_match"
    assert normalize_model("icon_seamless") == "icon_seamless"
    assert normalize_model("harmonie") == "dmi_seamless"
    with pytest.raises(ValueError):
        normalize_model("wrf")


def _route_fixtures():
    from kitebot.analysis import Window

    def w(spot_hourly, hour_from, hour_to, speed=8.0):
        return Window(start=datetime(2026, 8, 22, hour_from, tzinfo=TZ),
                      end=datetime(2026, 8, 22, hour_to, tzinfo=TZ),
                      min_speed=speed, max_speed=speed + 2, max_gust=speed + 5,
                      direction=315.0)

    near_a = spot(name="Alfa", lat=57.00, lon=24.00)
    near_b = spot(name="Beta", lat=57.36, lon=24.00)   # ~40 km north
    far_c = spot(name="Cerija", lat=59.70, lon=24.00)  # ~300 km north
    return w, near_a, near_b, far_c


def test_route_chains_two_spots_with_travel_gap():
    from kitebot.routes import day_route
    w, a, b, _ = _route_fixtures()
    legs, total, single = day_route([(a, w(a, 10, 13)), (b, w(b, 15, 19))])
    assert [leg.spot.name for leg in legs] == ["Alfa", "Beta"]
    assert legs[1].travel_km > 30
    assert legs[1].start.hour == 15  # arrived before the window opened
    assert total == 7 and single == 4


def test_route_clips_second_leg_to_arrival():
    from kitebot.routes import day_route
    w, a, b, _ = _route_fixtures()
    legs, total, _ = day_route([(a, w(a, 10, 13)), (b, w(b, 13, 18))])
    assert len(legs) == 2
    assert legs[1].start > legs[1].window.start  # clipped: still driving at 13:00
    assert 6.5 < total < 7.5


def test_route_infeasible_when_too_far():
    from kitebot.routes import day_route
    w, a, _, c = _route_fixtures()
    legs, _, single = day_route([(a, w(a, 10, 13)), (c, w(c, 13, 17))])
    assert legs == []  # 300 km drive eats the whole second window
    assert single == 4


def test_route_wind_preference_breaks_ties():
    from kitebot.routes import day_route
    w, a, b, _ = _route_fixtures()
    c = spot(name="Ceta", lat=57.36, lon=24.01)  # right next to Beta
    items = [(a, w(a, 10, 13)), (b, w(b, 15, 19, speed=6.0)), (c, w(c, 15, 19, speed=12.0))]
    strong, _, _ = day_route(items, prefer="strong")
    light, _, _ = day_route(items, prefer="light")
    assert [leg.spot.name for leg in strong] == ["Alfa", "Ceta"]
    assert [leg.spot.name for leg in light] == ["Alfa", "Beta"]


def test_route_respects_total_drive_limit():
    from kitebot.routes import day_route
    w, a, b, _ = _route_fixtures()
    items = [(a, w(a, 10, 13)), (b, w(b, 15, 19))]  # ~52 road-km apart
    legs, _, _ = day_route(items, max_drive_km=30)
    assert legs == []
    legs, _, _ = day_route(items, max_drive_km=100)
    assert len(legs) == 2


def test_best_single_honours_origin_and_limit():
    from kitebot.routes import best_single
    w, a, b, far_c = _route_fixtures()
    items = [(a, w(a, 10, 13)), (far_c, w(far_c, 10, 19))]  # far spot has more wind
    origin = (57.00, 24.00)  # rider lives at Alfa

    # without constraints: the windiest spot wins
    name, hours, _, within = best_single(items)
    assert name == "Cerija" and within

    # with a 50 km limit: only Alfa is reachable, so Alfa it is
    name, hours, drive, within = best_single(items, origin=origin, max_drive_km=50)
    assert name == "Alfa" and within and drive < 5

    # nothing rideable within the limit: nearest windy spot, flagged as outside
    name, _, drive, within = best_single([(far_c, w(far_c, 10, 19))],
                                         origin=origin, max_drive_km=50)
    assert name == "Cerija" and not within and drive > 50


def test_window_rain_summed_and_shown():
    from kitebot.analysis import HourPoint, find_windows
    from kitebot.messages import format_window
    points = [
        HourPoint(datetime(2026, 8, 21, 10, tzinfo=TZ), 15, 20, 315, rain=1.2),
        HourPoint(datetime(2026, 8, 21, 11, tzinfo=TZ), 15, 20, 315, rain=1.3),
    ]
    w = find_windows(points, spot(), min_hours=2, day_start=8, day_end=20)[0]
    assert w.rain_mm == pytest.approx(2.5)
    assert "🌧 2,5 mm" in format_window(w, "m/s")
    # a dry window carries no rain note
    dry = find_windows([HourPoint(p.time, 15, 20, 315) for p in points],
                       spot(), min_hours=2, day_start=8, day_end=20)[0]
    assert "🌧" not in format_window(dry, "m/s")


def test_clip_past_windows():
    from kitebot.analysis import Window, clip_past
    def w(h1, h2, rain=0.0):
        return Window(start=datetime(2026, 8, 21, h1, tzinfo=TZ),
                      end=datetime(2026, 8, 21, h2, tzinfo=TZ),
                      min_speed=8, max_speed=10, max_gust=13, direction=315,
                      rain_mm=rain)
    now = datetime(2026, 8, 21, 15, 30, tzinfo=TZ)
    clipped = clip_past([w(10, 13), w(11, 17, rain=6.0), w(18, 20)], now)
    assert len(clipped) == 2                       # the ended 10-13 is gone
    running, future = clipped
    assert running.start == now                    # trimmed to now
    assert running.rain_mm == pytest.approx(1.5)   # 1.5h of 6h remain -> 6*0.25
    assert future.start.hour == 18                 # future window untouched


def test_dry_windows_filter():
    from kitebot.analysis import Window, dry_windows
    def w(rain, hours=3):
        return Window(start=datetime(2026, 8, 21, 10, tzinfo=TZ),
                      end=datetime(2026, 8, 21, 10 + hours, tzinfo=TZ),
                      min_speed=8, max_speed=10, max_gust=13, direction=315,
                      rain_mm=rain)
    kept = dry_windows([w(0.0), w(1.2), w(6.0)])  # 3h windows: 0, 0.4, 2.0 mm/h
    assert [win.rain_mm for win in kept] == [0.0, 1.2]


def test_pick_kite_heuristic():
    from kitebot.routes import pick_kite
    quiver = [12, 9, 7]
    # 85 kg, 10 m/s (~19.4 kn) -> ideal ~9.6 -> the 9
    assert pick_kite(10, "ms", 85, quiver) == 9
    # light 6 m/s -> ideal ~16 -> the biggest kite
    assert pick_kite(6, "ms", 85, quiver) == 12
    # nuking 15 m/s -> ideal ~6.4 -> the smallest
    assert pick_kite(15, "ms", 85, quiver) == 7
    # incomplete profile -> no recommendation
    assert pick_kite(10, "ms", None, quiver) is None
    assert pick_kite(10, "ms", 85, None) is None


def test_profile_roundtrip(tmp_path, monkeypatch):
    from kitebot import config
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "USERS_FILE", tmp_path / "users.json")
    assert config.get_profile(7) == {}
    config.update_profile(7, weight_kg=85.0)
    config.update_profile(7, quiver=[12, 9], home={"lat": 56.95, "lon": 24.1})
    profile = config.get_profile(7)
    assert profile["weight_kg"] == 85.0
    assert profile["quiver"] == [12, 9]
    assert profile["home"]["lat"] == 56.95


def test_route_origin_reported_and_clips_today():
    from kitebot.routes import day_route
    w, a, b, _ = _route_fixtures()
    origin = (57.36, 24.00)  # rider lives next to Beta, ~40 km from Alfa
    items = [(a, w(a, 10, 13)), (b, w(b, 15, 19))]
    legs, _, _ = day_route(items, origin=origin)
    assert legs[0].travel_km > 30  # drive from home to the first spot is shown
    # planning for today, leaving at 10:00: Alfa's 10-13 window loses its start
    depart = datetime(2026, 8, 22, 10, 0, tzinfo=TZ)
    legs, _, _ = day_route(items, origin=origin, depart_earliest=depart)
    assert legs[0].start > w(a, 10, 13).start


def test_normalize_wind_unit():
    assert normalize_wind_unit("m/s") == "ms"
    assert normalize_wind_unit("MS") == "ms"
    assert normalize_wind_unit("knots") == "kn"
    assert normalize_wind_unit("km/h") == "kmh"
    with pytest.raises(SystemExit):
        normalize_wind_unit("banana")


def test_spot_requests_are_staggered_not_simultaneous():
    """All spots used to hit Open-Meteo inside the same 100 ms and get 429/503."""
    import asyncio

    from kitebot import checker
    from kitebot.config import Settings

    starts: list = []

    async def fake_fetch(client, spot, days, unit, model="best_match", delays=()):
        starts.append(asyncio.get_running_loop().time())
        return []

    async def run():
        spots = [Spot(name=f"S{i}", lat=1.0, lon=2.0) for i in range(5)]
        return await checker.gather_results(spots, Settings(), stagger=0.02)

    original = checker.fetch_hours
    checker.fetch_hours = fake_fetch
    try:
        results = asyncio.run(run())
    finally:
        checker.fetch_hours = original

    assert len(results) == 5
    assert [r.spot.name for r in results] == [f"S{i}" for i in range(5)]
    # asserted on the total spread, not pairwise gaps: a loaded machine can
    # oversleep one launch and borrow the time from the next gap
    assert starts[-1] - starts[0] >= 0.02 * 4


def test_zero_stagger_keeps_requests_concurrent():
    import asyncio

    from kitebot import checker
    from kitebot.config import Settings

    running = concurrent = 0

    async def fake_fetch(client, spot, days, unit, model="best_match", delays=()):
        nonlocal running, concurrent
        running += 1
        concurrent = max(concurrent, running)
        await asyncio.sleep(0.01)
        running -= 1
        return []

    async def run():
        spots = [Spot(name=f"S{i}", lat=1.0, lon=2.0) for i in range(4)]
        return await checker.gather_results(spots, Settings(), stagger=0)

    original = checker.fetch_hours
    checker.fetch_hours = fake_fetch
    try:
        asyncio.run(run())
    finally:
        checker.fetch_hours = original
    assert concurrent == 4


def test_retry_delay_is_jittered():
    from kitebot.forecast import ROBUST_DELAYS, _jittered
    base = ROBUST_DELAYS[0]
    samples = {_jittered(base) for _ in range(50)}
    assert len(samples) > 1                      # not a fixed delay any more
    assert all(0.75 * base <= s <= 1.25 * base for s in samples)

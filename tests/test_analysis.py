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
        "surfr:54321": {"distance_m": 31000, "height_m": 13.1},
    }
    lines, updated, changed = summarize(riders, stats)
    assert len(lines) == 1
    # best of each metric, both jump readings shown, record from the max
    assert "31,0 km" in lines[0]
    assert "lēciens 14,2 m (WOO 14,2 / Surfr 13,1)" in lines[0]
    assert "JAUNS REKORDS (+0,6 m)" in lines[0]
    assert changed and updated[0]["record_height_m"] == 14.2


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


def test_route_sections_only_on_clear_gain():
    from kitebot.config import Settings
    from kitebot.messages import SpotResult
    from kitebot.routes import route_sections
    w, a, b, _ = _route_fixtures()
    results = [SpotResult(spot=a, windows=[w(a, 10, 13)]),
               SpotResult(spot=b, windows=[w(b, 15, 19)])]
    section = route_sections(results, Settings())
    assert section and "Maršruts" in section and "🚗" in section
    # a lone spot never produces a route section
    assert route_sections([SpotResult(spot=a, windows=[w(a, 10, 13)])], Settings()) is None


def test_normalize_wind_unit():
    assert normalize_wind_unit("m/s") == "ms"
    assert normalize_wind_unit("MS") == "ms"
    assert normalize_wind_unit("knots") == "kn"
    assert normalize_wind_unit("km/h") == "kmh"
    with pytest.raises(SystemExit):
        normalize_wind_unit("banana")

"""Building the Telegram messages (HTML parse mode).

All user-facing text is Latvian; wind directions are written out in words.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

from .analysis import DIRECTION_SECTORS, Window, toggles_from_sectors
from .config import UNIT_LABELS, Settings, Spot

TELEGRAM_LIMIT = 4000  # hard limit is 4096; keep headroom

WEEKDAYS_LV = ["Pr", "Ot", "Tr", "Ce", "Pk", "Se", "Sv"]

# genitive, for "… vējš" in forecast lines; index 0 = N, 1 = NE, …
DIRECTION_WORDS_LV = [
    "ziemeļu", "ziemeļaustrumu", "austrumu", "dienvidaustrumu",
    "dienvidu", "dienvidrietumu", "rietumu", "ziemeļrietumu",
]
# nominative, for buttons and spot descriptions
DIRECTION_LABELS_LV = [
    "Ziemeļi", "Ziemeļaustrumi", "Austrumi", "Dienvidaustrumi",
    "Dienvidi", "Dienvidrietumi", "Rietumi", "Ziemeļrietumi",
]


@dataclass
class SpotResult:
    spot: Spot
    windows: list = field(default_factory=list)
    error: "str | None" = None


def any_windows(results: list) -> bool:
    return any(r.windows for r in results)


def unit_label(unit: str) -> str:
    return UNIT_LABELS.get(unit, unit)


def direction_word(deg: float) -> str:
    return DIRECTION_WORDS_LV[int(deg / 45 + 0.5) % 8] + " vējš"


def _fmt(value: float) -> str:
    return str(round(value))


def _day_lv(dt) -> str:
    return f"{WEEKDAYS_LV[dt.weekday()]} {dt:%d.%m}"


def rain_note(rain_mm: float) -> str:
    """' · 🌧 2,4 mm' when meaningful rain falls inside the window, else ''."""
    if rain_mm < 0.2:
        return ""
    return " · 🌧 " + f"{rain_mm:.1f}".replace(".", ",") + " mm"


def format_window(w: Window, label: str) -> str:
    if round(w.min_speed) == round(w.max_speed):
        speed = f"{_fmt(w.min_speed)} {label}"
    else:
        speed = f"{_fmt(w.min_speed)}–{_fmt(w.max_speed)} {label}"
    return (
        f"✅ {_day_lv(w.start)} · {w.start:%H:%M}–{w.end:%H:%M} · "
        f"{speed} (brāzmas {_fmt(w.max_gust)}) · {direction_word(w.direction)}"
        f"{rain_note(w.rain_mm)}"
    )


def describe_directions(sectors: list) -> str:
    if not sectors:
        return "jebkurš virziens"
    blocks = [list(b) for b in DIRECTION_SECTORS]
    if all(list(s) in blocks for s in sectors):
        toggles = toggles_from_sectors(sectors)
        return ", ".join(DIRECTION_LABELS_LV[i] for i, on in enumerate(toggles) if on)
    return ", ".join(f"{round(lo)}°–{round(hi)}°" for lo, hi in sectors)


def describe_spot(spot: Spot, label: str) -> str:
    extra = "" if spot.cell_selection == "land" else f" · {spot.cell_selection}"
    if spot.model:
        extra += f" · {spot.model.split('_')[0]}"
    return (
        f"<b>{html.escape(spot.name)}</b> · {spot.lat:.4f}, {spot.lon:.4f} · "
        f"{_fmt(spot.min_wind)}–{_fmt(spot.max_wind)} {label} · "
        f"{describe_directions(spot.good_directions)}{extra}"
    )


def _days_lv(days: int) -> str:
    return "šodienai" if days == 1 else f"nākamās {days} dienas"


def build_digest(results: list, settings: Settings, title: str = "Kaita prognoze") -> str:
    label = unit_label(settings.wind_unit)
    header = f"🪁 <b>{html.escape(title)}</b> · {_days_lv(settings.forecast_days)}"
    if results and not any(r.windows or r.error for r in results):
        return header + "\n\nNevienā spotā nav braucama vēja."
    blocks = [header]
    for r in results:
        lines = [f"<b>{html.escape(r.spot.name)}</b>"]
        if r.error:
            lines.append("⚠️ prognozi šobrīd neizdevās iegūt")
        elif r.windows:
            lines.extend(format_window(w, label) for w in r.windows)
        else:
            lines.append("— braucama vēja nav")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def split_message(text: str, limit: int = TELEGRAM_LIMIT) -> list:
    """Split on blank lines so each Telegram message stays under the length cap."""
    if len(text) <= limit:
        return [text]
    parts: list = []
    current = ""
    for block in text.split("\n\n"):
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) > limit and current:
            parts.append(current)
            current = block
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts


def to_plain(html_text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", html_text))

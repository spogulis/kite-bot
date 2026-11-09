"""The morning digest as a picture: a map of the spots that have wind today,
each with one row per rideable period — its hours and wind range, coloured by
wind strength from green (light) to violet (strong) — drawn with Pillow over
a bundled coastline.

The coastline (assets/baltic_land.json) is Natural Earth 1:10m land and minor
islands — public domain — cut to the eastern Baltic and simplified. Spots
outside it get no map; the digest then goes out as text, as it always did.
"""
from __future__ import annotations

import io
import json
import logging
from functools import lru_cache
from math import cos, radians
from pathlib import Path

from .config import UNIT_LABELS, to_knots

log = logging.getLogger(__name__)

ASSET = Path(__file__).resolve().parent / "assets" / "baltic_land.json"
WIDTH, HEIGHT = 1280, 800
SCALE = 2  # drawn at twice the size, then shrunk: Pillow polygons have no anti-aliasing

SEA = (207, 227, 236)
LAND = (231, 235, 221)
SHORE = (217, 205, 170)
NAVY = (16, 32, 46)
WHITE = (251, 251, 248)

KNOT_MS = 0.514444
# Wind bands in m/s (upper edge, fill, text), light to strong: green -> violet.
BANDS = [
    (6.0, (46, 158, 79), WHITE),
    (8.0, (141, 191, 46), NAVY),
    (10.0, (227, 182, 32), NAVY),
    (12.0, (232, 116, 44), NAVY),
    (14.0, (214, 58, 58), WHITE),
    (None, (142, 68, 173), WHITE),
]

FONTS = {
    False: ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "DejaVuSans.ttf",
            "C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/arial.ttf",
            "/Library/Fonts/Arial.ttf"],
    True: ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "DejaVuSans-Bold.ttf",
           "C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/arialbd.ttf",
           "/Library/Fonts/Arial Bold.ttf"],
}


@lru_cache(maxsize=1)
def _coast() -> dict:
    return json.loads(ASSET.read_text(encoding="utf-8"))


@lru_cache(maxsize=16)
def _font(size: int, bold: bool = False):
    from PIL import ImageFont
    for path in FONTS[bold]:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def band(mean_speed: float, unit: str) -> tuple:
    """(fill, text colour) for a wind speed given in the configured unit."""
    ms = to_knots(mean_speed, unit) * KNOT_MS
    for top, fill, ink in BANDS:
        if top is None or ms < top:
            return fill, ink
    return BANDS[-1][1:]


def legend_labels(unit: str) -> list:
    """The band edges in the configured unit, e.g. ["<6", "6–8", …, "14+"]."""
    def conv(ms: float) -> str:
        if unit == "ms":
            return f"{ms:g}"
        return str(round(ms / KNOT_MS / to_knots(1.0, unit)))  # m/s -> knots -> unit
    edges = [top for top, _, _ in BANDS if top is not None]
    labels = [f"<{conv(edges[0])}"]
    labels += [f"{conv(a)}–{conv(b)}" for a, b in zip(edges, edges[1:])]
    labels.append(f"{conv(edges[-1])}+")
    return labels


def period_rows(result, unit: str) -> list:
    """[(text, fill, ink)] — one row per rideable period of the day, in time
    order. Windows are already split where the wind changes (wind_band), so
    each row is one kite size's worth of wind."""
    label = UNIT_LABELS.get(unit, unit)
    rows = []
    for w in sorted(result.windows, key=lambda w: w.start):
        lo, hi = round(w.min_speed), round(w.max_speed)
        wind = f"{lo} {label}" if lo == hi else f"{lo}–{hi} {label}"
        text = f"{w.start:%H}–{w.end:%H}  {wind}" + ("  matracis" if w.foil_only else "")
        fill, ink = band((w.min_speed + w.max_speed) / 2, unit)
        rows.append((text, fill, ink))
    return rows


def covered(spots: list) -> bool:
    """True when every spot lies inside the bundled coastline."""
    lon0, lat0, lon1, lat1 = _coast()["bbox"]
    return bool(spots) and all(lon0 <= s.lon <= lon1 and lat0 <= s.lat <= lat1 for s in spots)


class _View:
    """Equirectangular projection of a box around the spots, 16:10."""

    def __init__(self, spots: list):
        lats = [s.lat for s in spots]
        lons = [s.lon for s in spots]
        self.lat_c = (min(lats) + max(lats)) / 2
        self.lon_c = (min(lons) + max(lons)) / 2
        self.k = cos(radians(self.lat_c))
        aspect = WIDTH / HEIGHT
        half_h = max((max(lats) - min(lats)) / 2 * 1.5, 0.3)
        half_w = max((max(lons) - min(lons)) * self.k / 2 * 1.5 + 0.3, half_h * aspect)
        self.half_w, self.half_h = half_w, max(half_h, half_w / aspect)

    def xy(self, lon: float, lat: float) -> tuple:
        x = ((lon - self.lon_c) * self.k + self.half_w) / (2 * self.half_w) * WIDTH * SCALE
        y = (self.half_h - (lat - self.lat_c)) / (2 * self.half_h) * HEIGHT * SCALE
        return x, y


def _overlaps(box, boxes) -> bool:
    x0, y0, x1, y1 = box
    return any(x0 < b[2] and b[0] < x1 and y0 < b[3] and b[1] < y1 for b in boxes)


def render(results: list, day_label: str, unit: str = "ms") -> "bytes | None":
    """PNG of today's map with only the spots that have wind, or None when
    none has or they fall outside the coastline."""
    windy = [r for r in results if r.windows and not r.error]
    if not windy or not covered([r.spot for r in windy]):
        return None
    from PIL import Image, ImageDraw

    s = SCALE
    view = _View([r.spot for r in windy])
    image = Image.new("RGB", (WIDTH * s, HEIGHT * s), SEA)
    draw = ImageDraw.Draw(image)
    for ring in _coast()["rings"]:
        draw.polygon([view.xy(lon, lat) for lon, lat in ring], fill=LAND, outline=SHORE, width=3 * s)

    title_font = _font(30 * s, bold=True)
    row_font = _font(26 * s, bold=True)
    pad, row_gap, row_h = 12 * s, 6 * s, 40 * s
    pins = [view.xy(r.spot.lon, r.spot.lat) for r in windy]
    taken = [(x - 16 * s, y - 16 * s, x + 16 * s, y + 16 * s) for x, y in pins]
    margin = 12 * s
    legend_h = 64 * s  # keep labels off the legend strip at the bottom
    for result, (x, y) in zip(windy, pins):
        rows = period_rows(result, unit)
        t_left, t_top, t_right, t_bottom = draw.textbbox((0, 0), result.spot.name, font=title_font)
        widths = [draw.textbbox((0, 0), text, font=row_font)[2] for text, _, _ in rows]
        w = max(t_right - t_left, *(rw + 2 * pad for rw in widths)) + 2 * pad
        h = (t_bottom - t_top) + pad * 2 + len(rows) * (row_h + row_gap)
        gap = 24 * s
        candidates = [(x + gap, y - h / 2), (x - gap - w, y - h / 2),
                      (x - w / 2, y - gap - h), (x - w / 2, y + gap)]
        box = None
        for bx, by in candidates:
            trial = (bx, by, bx + w, by + h)
            inside = (margin <= bx and bx + w <= WIDTH * s - margin
                      and margin <= by and by + h <= HEIGHT * s - margin - legend_h)
            if inside and not _overlaps(trial, taken):
                box = trial
                break
        if box is None:  # crowded: take the first spot inside the picture
            bx, by = candidates[0]
            bx = min(max(bx, margin), WIDTH * s - margin - w)
            by = min(max(by, margin), HEIGHT * s - margin - legend_h - h)
            box = (bx, by, bx + w, by + h)
        taken.append(box)
        draw.rounded_rectangle(box, radius=14 * s, fill=WHITE, outline=NAVY, width=2 * s)
        draw.text((box[0] + pad - t_left, box[1] + pad - t_top), result.spot.name,
                  font=title_font, fill=NAVY)
        ry = box[1] + pad + (t_bottom - t_top) + pad
        for text, fill, ink in rows:
            row = (box[0] + pad, ry, box[2] - pad, ry + row_h)
            draw.rounded_rectangle(row, radius=10 * s, fill=fill)
            r_left, r_top, _, r_bottom = draw.textbbox((0, 0), text, font=row_font)
            draw.text((row[0] + pad - r_left, ry + (row_h - (r_bottom - r_top)) / 2 - r_top),
                      text, font=row_font, fill=ink)
            ry += row_h + row_gap
    for x, y in pins:
        r = 11 * s
        draw.ellipse((x - r, y - r, x + r, y + r), fill=NAVY, outline=WHITE, width=4 * s)

    _pill(draw, (20 * s, 20 * s), day_label, _font(32 * s, bold=True))
    _legend(draw, (20 * s, HEIGHT * s - 20 * s), _font(24 * s, bold=True), unit)
    out = io.BytesIO()
    image.resize((WIDTH, HEIGHT), Image.LANCZOS).save(out, format="PNG", optimize=True)
    return out.getvalue()


def _pill(draw, at: tuple, text: str, font) -> None:
    s = SCALE
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    box = (at[0], at[1], at[0] + right - left + 32 * s, at[1] + bottom - top + 20 * s)
    draw.rounded_rectangle(box, radius=(box[3] - box[1]) // 2, fill=WHITE)
    draw.text((box[0] + 16 * s - left, box[1] + 10 * s - top), text, font=font, fill=NAVY)


def _legend(draw, bottom_left: tuple, font, unit: str) -> None:
    """A strip of the wind bands, each swatch labelled with its range."""
    s = SCALE
    label = UNIT_LABELS.get(unit, unit)
    items = list(zip(legend_labels(unit), BANDS))
    _, top, _, bottom = draw.textbbox((0, 0), "Ag", font=font)
    text_h = bottom - top
    sw_pad = 12 * s
    widths = [draw.textbbox((0, 0), text, font=font)[2] + 2 * sw_pad for text, _ in items]
    unit_w = draw.textbbox((0, 0), label, font=font)[2]
    height = text_h + 2 * sw_pad
    width = sum(widths) + unit_w + 4 * sw_pad
    x0, y1 = bottom_left
    box = (x0, y1 - height - 12 * s, x0 + width, y1)
    draw.rounded_rectangle(box, radius=14 * s, fill=WHITE)
    x = x0 + 6 * s
    for (text, (_, fill, ink)), w in zip(items, widths):
        cell = (x, box[1] + 6 * s, x + w, box[3] - 6 * s)
        draw.rounded_rectangle(cell, radius=8 * s, fill=fill)
        draw.text((x + sw_pad, cell[1] + (cell[3] - cell[1] - text_h) / 2 - top), text,
                  font=font, fill=ink)
        x += w + 2 * s
    draw.text((x + sw_pad, box[1] + (box[3] - box[1] - text_h) / 2 - top), label,
              font=font, fill=NAVY)

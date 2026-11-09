"""The morning digest as a picture: a map of the spots, each labelled with
today's rideable window, drawn with Pillow over a bundled coastline.

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

log = logging.getLogger(__name__)

ASSET = Path(__file__).resolve().parent / "assets" / "baltic_land.json"
WIDTH, HEIGHT = 1280, 800
SCALE = 2  # drawn at twice the size, then shrunk: Pillow polygons have no anti-aliasing

SEA = (207, 227, 236)
LAND = (231, 235, 221)
SHORE = (217, 205, 170)
NAVY = (16, 32, 46)
WHITE = (251, 251, 248)
KITE = ((34, 112, 154), WHITE)          # badge fill, text
FOIL = ((156, 201, 222), NAVY)
NONE = ((227, 233, 229), (74, 87, 99))

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


def spot_label(result) -> tuple:
    """(badge text, colours) for one spot's day: the first kite window,
    else the first foil-only one, with "+n" when there are more."""
    if result.error:
        return f"{result.spot.name} ?", NONE
    windows = sorted(result.windows, key=lambda w: w.start)
    kite = [w for w in windows if not w.foil_only]
    first = (kite or windows or [None])[0]
    if first is None:
        return f"{result.spot.name} —", NONE
    more = f" +{len(windows) - 1}" if len(windows) > 1 else ""
    hours = f"{first.start:%H}–{first.end:%H}{more}"
    if first.foil_only:
        return f"{result.spot.name} {hours} matracis", FOIL
    return f"{result.spot.name} {hours}", KITE


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
        half_h = max((max(lats) - min(lats)) / 2 * 1.45, 0.3)
        half_w = max((max(lons) - min(lons)) * self.k / 2 * 1.45 + 0.25, half_h * aspect)
        self.half_w, self.half_h = half_w, max(half_h, half_w / aspect)

    def xy(self, lon: float, lat: float) -> tuple:
        x = ((lon - self.lon_c) * self.k + self.half_w) / (2 * self.half_w) * WIDTH * SCALE
        y = (self.half_h - (lat - self.lat_c)) / (2 * self.half_h) * HEIGHT * SCALE
        return x, y


def _overlaps(box, boxes) -> bool:
    x0, y0, x1, y1 = box
    return any(x0 < b[2] and b[0] < x1 and y0 < b[3] and b[1] < y1 for b in boxes)


def render(results: list, day_label: str) -> "bytes | None":
    """PNG of today's map, or None when the spots fall outside the coastline."""
    spots = [r.spot for r in results]
    if not covered(spots):
        return None
    from PIL import Image, ImageDraw

    s = SCALE
    view = _View(spots)
    image = Image.new("RGB", (WIDTH * s, HEIGHT * s), SEA)
    draw = ImageDraw.Draw(image)
    for ring in _coast()["rings"]:
        points = [view.xy(lon, lat) for lon, lat in ring]
        draw.polygon(points, fill=LAND, outline=SHORE, width=3 * s)

    font = _font(30 * s, bold=True)
    pins = [view.xy(r.spot.lon, r.spot.lat) for r in results]
    taken = [(x - 16 * s, y - 16 * s, x + 16 * s, y + 16 * s) for x, y in pins]
    margin = 12 * s
    for result, (x, y) in zip(results, pins):
        text, (fill, ink) = spot_label(result)
        left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
        w, h = right - left + 24 * s, bottom - top + 16 * s
        gap = 24 * s
        candidates = [(x + gap, y - h / 2), (x - gap - w, y - h / 2),
                      (x - w / 2, y - gap - h), (x - w / 2, y + gap)]
        box = None
        for bx, by in candidates:
            trial = (bx, by, bx + w, by + h)
            inside = (margin <= bx and bx + w <= WIDTH * s - margin
                      and margin <= by and by + h <= HEIGHT * s - margin)
            if inside and not _overlaps(trial, taken):
                box = trial
                break
        if box is None:  # crowded: take the first spot inside the picture
            bx, by = candidates[0]
            bx = min(max(bx, margin), WIDTH * s - margin - w)
            by = min(max(by, margin), HEIGHT * s - margin - h)
            box = (bx, by, bx + w, by + h)
        taken.append(box)
        draw.rounded_rectangle(box, radius=12 * s, fill=fill, outline=WHITE, width=2 * s)
        draw.text((box[0] + 12 * s - left, box[1] + 8 * s - top), text, font=font, fill=ink)
    for (x, y), result in zip(pins, results):
        fill = spot_label(result)[1][0]
        r = 13 * s
        draw.ellipse((x - r, y - r, x + r, y + r), fill=fill, outline=WHITE, width=4 * s)
        draw.ellipse((x - r - 2 * s, y - r - 2 * s, x + r + 2 * s, y + r + 2 * s),
                     outline=NAVY, width=2 * s)

    _pill(draw, (20 * s, 20 * s), day_label, _font(32 * s, bold=True))
    _legend(draw, (20 * s, HEIGHT * s - 20 * s), _font(26 * s))
    out = io.BytesIO()
    image.resize((WIDTH, HEIGHT), Image.LANCZOS).save(out, format="PNG", optimize=True)
    return out.getvalue()


def _pill(draw, at: tuple, text: str, font) -> None:
    s = SCALE
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    box = (at[0], at[1], at[0] + right - left + 32 * s, at[1] + bottom - top + 20 * s)
    draw.rounded_rectangle(box, radius=(box[3] - box[1]) // 2, fill=WHITE)
    draw.text((box[0] + 16 * s - left, box[1] + 10 * s - top), text, font=font, fill=NAVY)


def _legend(draw, bottom_left: tuple, font) -> None:
    s = SCALE
    items = [("kaits", KITE[0]), ("matracis", FOIL[0]), ("nav vēja", NONE[0])]
    widths = [draw.textbbox((0, 0), text, font=font)[2] for text, _ in items]
    _, top, _, bottom = draw.textbbox((0, 0), "Ag", font=font)
    height = bottom - top + 20 * s
    width = sum(widths) + len(items) * (24 + 12 + 20) * s + 12 * s
    x0, y1 = bottom_left
    box = (x0, y1 - height, x0 + width, y1)
    draw.rounded_rectangle(box, radius=height // 2, fill=WHITE)
    x = x0 + 18 * s
    for (text, colour), w in zip(items, widths):
        cy = (box[1] + box[3]) / 2
        draw.rounded_rectangle((x, cy - 12 * s, x + 24 * s, cy + 12 * s), radius=6 * s,
                               fill=colour, outline=NAVY, width=s)
        draw.text((x + 36 * s, box[1] + 10 * s - top), text, font=font, fill=NAVY)
        x += (24 + 12 + 20) * s + w

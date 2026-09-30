#!/usr/bin/env python
"""Render the trip video's mode icons (assets/video/icons/) from Material
Symbols, the icon set the app uses for the same modes.

Each icon is the mode's Material Symbols glyph (Outlined, filled — the look
of Flutter's ``Icons.*``) rendered as a white glyph on transparent, 512 px
square. ``src/video/overlay.py`` tints it with the mode's colour and scales it
down once per size, so the video's icons stay sharp at any resolution.

Dev-only: the SVGs are fetched from a pinned commit of
google/material-design-icons and rasterised with CairoSVG, which is neither in
requirements.txt nor in the app image. Run it in a throwaway container:

    docker run --rm -v "$PWD":/w -w /w python:3.14-slim sh -c \\
        "apt-get update -qq && apt-get install -y -qq libcairo2 >/dev/null \\
         && pip install -q cairosvg && python scripts/make_video_icons.py"

and commit the PNGs it writes.
"""
from __future__ import annotations

import io
import urllib.request
from pathlib import Path

import cairosvg
from PIL import Image

OUT_DIR = Path(__file__).resolve().parents[1] / "assets" / "video" / "icons"
SIZE = 512

# google/material-design-icons, master on 2026-09-25.
COMMIT = "bd8cb85bd4bad964fe6918f79665bb40c3a8efef"
URL = ("https://raw.githubusercontent.com/google/material-design-icons/{commit}"
       "/symbols/web/{name}/materialsymbolsoutlined/{name}_fill1_24px.svg")

# Mode → the symbol the app shows for it: activity_panel.dart
# (_ActivityIconBox._icon, and the segment icons) and map_panel.dart.
SYMBOLS = {
    "ride": "directions_bike",
    "run": "directions_run",
    "hike": "hiking",
    "other": "map",
    "flight": "flight",
    "train": "train",
    "bus": "directions_bus",
    "boat": "directions_boat",
}


def render(name: str) -> Image.Image:
    with urllib.request.urlopen(URL.format(commit=COMMIT, name=name), timeout=30) as resp:
        svg = resp.read().decode("utf-8")
    svg = svg.replace("<path ", '<path fill="#FFFFFF" ')
    png = cairosvg.svg2png(bytestring=svg.encode("utf-8"),
                           output_width=SIZE, output_height=SIZE)
    img = Image.open(io.BytesIO(png)).convert("RGBA")
    # Pure white everywhere: only the alpha carries the glyph.
    white = Image.new("RGBA", img.size, (255, 255, 255, 0))
    white.putalpha(img.getchannel("A"))
    return white


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for mode, name in SYMBOLS.items():
        path = OUT_DIR / f"{mode}.png"
        render(name).save(path, optimize=True)
        print(f"{path.name:12} {name}")


if __name__ == "__main__":
    main()

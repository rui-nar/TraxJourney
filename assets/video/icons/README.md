# Trip video mode icons

One 512×512 PNG per travel mode, a white glyph on transparent, drawn in the
trip video's marker, speed badge, counters and end card
(`src/video/overlay.py`). The overlay tints each icon at runtime (the mode
colour on the dark panels and cards, a darker shade of it on the marker's
white disc, as the app does) and scales it down once per size. A mode without
a file falls back to `other.png`.

They are the Material Symbols glyphs the app shows for the same modes
(Outlined style, filled), from
[google/material-design-icons](https://github.com/google/material-design-icons)
at commit `bd8cb85bd4bad964fe6918f79665bb40c3a8efef`, licensed under the
Apache License 2.0 (see `LICENSE_MATERIAL_SYMBOLS.txt`). The only change is
rendering them white at 512 px.

| File | Mode | Symbol |
|---|---|---|
| ride.png | ride | directions_bike |
| run.png | run | directions_run |
| hike.png | walk / hike | hiking |
| other.png | other activities | map |
| flight.png | flight | flight |
| train.png | train | train |
| bus.png | bus | directions_bus |
| boat.png | boat | directions_boat |

To regenerate them, run `scripts/make_video_icons.py` in Docker as its
docstring shows. The SVG tooling it needs stays out of the app image.

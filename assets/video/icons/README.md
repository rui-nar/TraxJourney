# Trip video mode icons

One 128×128 RGBA PNG per travel mode, drawn in the trip video's marker,
speed badge, counters and end card (`src/video/overlay.py`). A mode without
a file falls back to `other.png`.

They are glyphs of Noto Color Emoji (`assets/fonts/NotoColorEmoji.ttf`, SIL
Open Font License 1.1, see `assets/fonts/LICENSE_NOTO_COLOR_EMOJI.txt`),
rendered at the font's 109 px strike, trimmed, centred on a square and
resized to 128 px with Pillow:

| File | Mode | Code point |
|---|---|---|
| ride.png | ride | U+1F6B4 bicyclist |
| run.png | run | U+1F3C3 runner |
| hike.png | walk / hike | U+1F97E hiking boot |
| other.png | other activities | U+1F9ED compass |
| flight.png | flight | U+2708 U+FE0F airplane |
| train.png | train | U+1F686 train |
| bus.png | bus | U+1F68C bus |
| boat.png | boat | U+26F4 U+FE0F ferry |

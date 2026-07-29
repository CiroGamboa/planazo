# Test data fixtures

Small binary fixtures used by tests that need real file input. Every
fixture in this directory ships with the exact `ffmpeg` (or equivalent)
recipe that generated it — regenerate any file by running the recipe
from the repo root.

## `sample_5s.mp4`

A 5-second synthetic test clip (`ffmpeg` `testsrc` pattern, 320x240 at
30 fps, H.264 / `yuv420p`) used by `tests/test_extraction_frames.py` to
exercise `planazo.extraction.frames.extract_reel_frames`. Roughly 26 KB.

Regenerate:

```
ffmpeg -y -f lavfi -i "testsrc=duration=5:size=320x240:rate=30" \
       -c:v libx264 -pix_fmt yuv420p \
       tests/data/sample_5s.mp4
```

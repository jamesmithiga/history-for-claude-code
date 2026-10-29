"""Assemble frames captured by tools/capture_media.mjs into docs/media/demo.gif.

Usage: python tools/make_gif.py FRAMES_DIR docs/media/demo.gif [--width 1088]
Requires Pillow.
"""
import argparse
from pathlib import Path

from PIL import Image

ap = argparse.ArgumentParser()
ap.add_argument("frames", type=Path)
ap.add_argument("out", type=Path)
ap.add_argument("--width", type=int, default=1088)
ap.add_argument("--ms", type=int, default=140, help="milliseconds per frame")
args = ap.parse_args()

paths = sorted(args.frames.glob("f*.png"))
if not paths:
    raise SystemExit(f"no frames in {args.frames}")
first = Image.open(paths[0])
size = (args.width, round(first.height * args.width / first.width))
# One shared palette (from a representative frame) keeps colours stable between frames.
palette_src = Image.open(paths[len(paths) // 2]).convert("RGB").resize(size, Image.LANCZOS)
palette = palette_src.quantize(colors=128, method=Image.Quantize.MEDIANCUT)
frames = [Image.open(p).convert("RGB").resize(size, Image.LANCZOS).quantize(palette=palette, dither=Image.Dither.NONE)
          for p in paths]
durations = [args.ms] * len(frames)
durations[-1] = 2500  # linger on the last frame before looping
frames[0].save(args.out, save_all=True, append_images=frames[1:], duration=durations, loop=0, optimize=True,
               disposal=1)
print(f"wrote {args.out} ({len(frames)} frames, {args.out.stat().st_size / 1e6:.2f} MB)")

"""Render media/icon.png (256x256): a history glyph (clock with a counter-clockwise
arrow) on a rounded square. Drawn at 4x and downsampled for clean anti-aliasing.
Requires Pillow. Run: python scripts/make-icon.py
"""
import math
from pathlib import Path

from PIL import Image, ImageDraw

S = 1024
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# Background: rounded square with a vertical warm gradient.
grad = Image.new("RGBA", (S, S))
top, bottom = (222, 112, 72), (176, 72, 38)
for y in range(S):
    t = y / (S - 1)
    grad.paste(tuple(int(a + (b - a) * t) for a, b in zip(top, bottom)) + (255,), (0, y, S, y + 1))
mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle((32, 32, S - 32, S - 32), radius=200, fill=255)
img.paste(grad, (0, 0), mask)

white = (255, 250, 245, 255)
cx, cy, r, w = S // 2, S // 2 + 10, 300, 64

# Clock ring: open arc leaving a gap at upper-left for the arrow head.
d.arc((cx - r, cy - r, cx + r, cy + r), start=-150 + 28, end=210 - 10, fill=white, width=w)

# Arrow head at the arc's start, pointing counter-clockwise.
a = math.radians(-150 + 22)
tip = (cx + r * math.cos(a), cy + r * math.sin(a))
tangent = a - math.pi / 2  # counter-clockwise direction
size = 120
p1 = (tip[0] + size * math.cos(tangent), tip[1] + size * math.sin(tangent))
p2 = (tip[0] + size * 0.75 * math.cos(tangent + 2.1), tip[1] + size * 0.75 * math.sin(tangent + 2.1))
p3 = (tip[0] + size * 0.75 * math.cos(tangent - 2.1), tip[1] + size * 0.75 * math.sin(tangent - 2.1))
d.polygon([p1, p2, p3], fill=white)

# Hands.
d.line((cx, cy, cx, cy - 175), fill=white, width=w)
d.line((cx, cy, cx + 130, cy + 85), fill=white, width=w)
d.ellipse((cx - 44, cy - 44, cx + 44, cy + 44), fill=white)

out = Path(__file__).resolve().parent.parent / "media" / "icon.png"
out.parent.mkdir(exist_ok=True)
img.resize((256, 256), Image.LANCZOS).save(out, optimize=True)
print(f"wrote {out}")

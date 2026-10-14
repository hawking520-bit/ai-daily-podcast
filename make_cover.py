#!/usr/bin/env python3
"""Generate the square show artwork from show.json (3000x3000 RGB JPEG, < 512 KB). Deterministic."""
import math, os, sys
from PIL import Image, ImageDraw, ImageFont, ImageFilter
HERE = os.path.dirname(os.path.abspath(__file__))
from show_config import SHOW
words = SHOW["title"].upper().split()
LINE1, LINE2 = (" ".join(words[:-1]), words[-1]) if len(words) > 1 else ("", words[0])
from show_config import cover_path
OUT = cover_path()
S = 3000
B = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"
R = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
img = Image.new("RGB", (S, S))
px = img.load()
# vertical gradient: deep navy -> indigo
top, bot = (10, 16, 40), (36, 22, 84)
d = ImageDraw.Draw(img)
for y in range(S):
    t = y / S
    d.line([(0, y), (S, y)], fill=tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(3)))
# signal waves (concentric arcs) bottom-right, glow
glow = Image.new("RGBA", (S, S), (0, 0, 0, 0)); g = ImageDraw.Draw(glow)
cx, cy = 2350, 2350
for k, r in enumerate(range(260, 1700, 230)):
    a = max(40, 230 - k * 30)
    g.arc([cx - r, cy - r, cx + r, cy + r], 180, 270, fill=(0, 220, 200, a), width=34)
g.ellipse([cx - 110, cy - 110, cx + 110, cy + 110], fill=(0, 230, 210, 255))
img.paste(Image.alpha_composite(img.convert("RGBA"), glow.filter(ImageFilter.GaussianBlur(6))).convert("RGB"))
d = ImageDraw.Draw(img)
# waveform bar
x0, ybase = 300, 1640
for i in range(46):
    h = 40 + 220 * abs(math.sin(i * 0.55) * math.cos(i * 0.17))
    d.rounded_rectangle([x0 + i * 42, ybase - h, x0 + i * 42 + 22, ybase + h], radius=11, fill=(255, 196, 61))
def fit(t, path, size, maxw=2450):
    while size > 60 and d.textlength(t, font=ImageFont.truetype(path, size)) > maxw: size -= 10
    return ImageFont.truetype(path, size)
d.text((300, 430), LINE1, font=fit(LINE1, B, 330), fill=(255, 255, 255))
d.text((290, 780), LINE2, font=fit(LINE2, B, 520), fill=(0, 226, 205))
d.text((305, 2020), SHOW.get("tagline", ""), font=fit(SHOW.get("tagline", ""), R, 150), fill=(220, 225, 245))
if SHOW.get("cover_subtitle"): d.text((305, 2230), SHOW["cover_subtitle"], font=ImageFont.truetype(B, 170), fill=(255, 196, 61))
os.makedirs(os.path.dirname(OUT), exist_ok=True)
q = 90
while True:
    img.save(OUT, "JPEG", quality=q, optimize=True, progressive=False)
    if os.path.getsize(OUT) < 500_000 or q <= 50: break
    q -= 8
print(OUT, img.size, os.path.getsize(OUT))

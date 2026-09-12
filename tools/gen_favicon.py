# -*- coding: utf-8 -*-
"""Значок вкладки из фирменного знака.

Знак «rt» вытянут по горизонтали, и при сжатии в квадрат 32x32 он превращается
в кашу. Поэтому значок собирается заново: фирменный жёлтый фон со срезанным
углом, как в логотипе, и чёрные буквы по центру во всю ширину.

Запуск:  python tools/gen_favicon.py
"""
from pathlib import Path
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "app" / "backend" / "assets" / "logos_card" / "00_remtech.png"
OUT = ROOT / "app" / "frontend"

logo = Image.open(SRC).convert("RGBA")

# фирменные цвета берём из самого знака, а не на глаз
yellow = logo.getpixel((logo.width // 2, 6))[:3]
ink = (24, 24, 24)
for x in range(logo.width // 2, logo.width):
    px = logo.getpixel((x, logo.height // 2))
    if sum(px[:3]) < 200:
        ink = px[:3]
        break


def build(size: int) -> Image.Image:
    im = Image.new("RGBA", (size, size), yellow + (255,))
    # срез верхнего левого угла — узнаваемая деталь знака
    cut = max(2, size // 5)
    for y in range(cut):
        for x in range(cut - y):
            im.putpixel((x, y), (0, 0, 0, 0))
    # буквы: берём только их, без жёлтой подложки исходника
    letters = logo.crop(logo.split()[3].getbbox())
    mask = Image.new("L", letters.size, 0)
    px = letters.load()
    mp = mask.load()
    for y in range(letters.height):
        for x in range(letters.width):
            r, g, b, a = px[x, y]
            mp[x, y] = 255 if (a > 40 and r + g + b < 330) else 0
    mask = mask.crop(mask.getbbox())
    w = int(size * 0.74)
    h = max(1, int(mask.height * w / mask.width))
    mask = mask.resize((w, h), Image.LANCZOS)
    glyph = Image.new("RGBA", mask.size, ink + (255,))
    im.paste(glyph, ((size - w) // 2, (size - h) // 2), mask)
    return im


build(180).save(OUT / "apple-touch-icon.png")
build(32).save(OUT / "favicon-32.png")
build(64).resize((64, 64)).save(
    OUT / "favicon.ico", format="ICO",
    sizes=[(16, 16), (32, 32), (48, 48), (64, 64)])
print("жёлтый", yellow, "| буквы", ink)
for f in ("favicon.ico", "favicon-32.png", "apple-touch-icon.png"):
    print(" ", f, (OUT / f).stat().st_size, "байт")

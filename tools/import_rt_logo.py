"""Чистый PNG фирменного знака из растрового оригинала (Desktop/RemTech Форум/rt.jpg).

Исходник — JPEG: жёлтая плашка с чёрными буквами на тёмном фоне. Просто вырезать
фон по цвету нельзя: буквы того же цвета, что и фон, и стали бы дырами — на белой
карточке вместо букв было бы белое. Поэтому:

  1) находим жёлтые пикселы;
  2) построчно заливаем промежуток между крайними жёлтыми — получается силуэт
     плашки целиком, вместе с буквами (плашка выпуклая, этого достаточно);
  3) внутри силуэта красим в два фирменных цвета: жёлтый и чёрный — заодно
     уходят артефакты JPEG по краям букв;
  4) снаружи силуэта — прозрачность.

Запуск:  python tools/import_rt_logo.py
"""
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
SRC = Path.home() / "Desktop" / "RemTech Форум" / "rt.jpg"
LOGOS = ROOT / "app" / "backend" / "assets" / "logos"
CARD = ROOT / "app" / "backend" / "assets" / "logos_card"

YELLOW = (255, 194, 14)
INK = (13, 13, 15)


def build() -> Image.Image:
    rgb = np.asarray(Image.open(SRC).convert("RGB")).astype(np.int16)
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    # «жёлтый» = красный и зелёный высокие, синий заметно ниже
    yellow = (r > 120) & (g > 90) & (b < r - 60)

    mask = np.zeros(yellow.shape, dtype=bool)
    for y in range(yellow.shape[0]):
        xs = np.flatnonzero(yellow[y])
        if xs.size:
            mask[y, xs[0]:xs[-1] + 1] = True

    out = np.zeros((*mask.shape, 4), dtype=np.uint8)
    out[mask] = (*INK, 255)                 # внутри силуэта по умолчанию буквы
    out[mask & yellow] = (*YELLOW, 255)     # и жёлтая плашка вокруг них
    im = Image.fromarray(out, "RGBA")
    return im.crop(im.getbbox())


def letters(mark: Image.Image, color: tuple) -> Image.Image:
    """Только буквы «rt», без плашки, в заданном цвете.

    Внутри знака буквы — это НЕ жёлтые пиксели; берём их и красим. Плашка
    отбрасывается: в ряду логотипов она читалась как заливка и спорила
    с партнёрскими знаками, у которых фона нет.
    """
    a = np.asarray(mark).astype(np.int16)
    rgb, alpha = a[..., :3], a[..., 3]
    yellow = (rgb[..., 0] > 120) & (rgb[..., 1] > 90) & (rgb[..., 2] < rgb[..., 0] - 60)
    mask = (alpha > 0) & ~yellow
    out = np.zeros_like(a)
    out[..., 0], out[..., 1], out[..., 2] = color
    out[..., 3] = np.where(mask, 255, 0)
    im = Image.fromarray(out.astype(np.uint8), "RGBA")
    return im.crop(im.getbbox())


def _sans(size: int):
    """Наборный гротеск для подписи под знаком."""
    for path in ("C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/arial.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _tracked(draw, xy, text, font, fill, tracking):
    """Текст с разрядкой: PIL сам её не умеет, ведём по буквам."""
    x, y = xy
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill)
        x += draw.textlength(ch, font=font) + tracking


def _tracked_width(draw, text, font, tracking):
    return sum(draw.textlength(c, font=font) for c in text) + tracking * (len(text) - 1)


def signature(mark: Image.Image, color: tuple, word: str = "РЕМТЕХНИКА") -> Image.Image:
    """Лок-ап для ряда логотипов: буквы «rt», под ними название капсом.

    Набор строгий и в том же ключе, что у партнёров (у iNT под знаком стоит
    «ИНТЕЛЛЕКТУАЛЬНЫЕ ТЕРМИНАЛЫ» разряженным капсом). Рукописный вариант рядом
    с ними выглядел чужеродно.
    """
    glyph = letters(mark, color)
    W = glyph.height * 2.0                      # целевая ширина подписи — от роста знака
    tmp = ImageDraw.Draw(Image.new("RGBA", (1, 1)))

    size = max(int(glyph.height * 0.30), 10)    # подбираем кегль под нужную ширину
    for _ in range(40):
        font = _sans(size)
        tracking = size * 0.20                  # разрядка ~0.2 кегля, как у партнёров
        if _tracked_width(tmp, word, font, tracking) <= W:
            break
        size -= 1
    font = _sans(size)
    tracking = size * 0.20
    tw = _tracked_width(tmp, word, font, tracking)
    box = tmp.textbbox((0, 0), word, font=font)
    th = box[3]

    gap = int(glyph.height * 0.22)
    out = Image.new("RGBA", (int(max(glyph.width, tw)) + 2, glyph.height + gap + th + 2), (0, 0, 0, 0))
    out.alpha_composite(glyph, (int((out.width - glyph.width) / 2), 0))
    _tracked(ImageDraw.Draw(out), ((out.width - tw) / 2, glyph.height + gap - box[1]),
             word, font, color + (255,), tracking)
    return out.crop(out.getbbox())


def main():
    mark = build()
    # В ряду логотипов на карточке — ОРИГИНАЛЬНЫЙ знак: жёлтая плашка с чёрными
    # буквами. Именно это изображение зарегистрировано в Роспатенте, поэтому
    # перекрашивать и упрощать его нельзя. 00 — чтобы стоял первым в ряду.
    mark.save(CARD / "00_remtech.png")
    # знак в топбаре и знак для мерча (уходит в генерацию отдельным изображением)
    mark.save(LOGOS / "_rt_badge.png")
    mark.save(LOGOS / "_brand_cap.png")
    print("ok:", mark.size)


if __name__ == "__main__":
    main()

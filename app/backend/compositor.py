"""Композитинг фото-карточки в стиле макета (полароид).

Layout сверху вниз:
  • белая рамка-полароид;
  • фото гостя;
  • рукописная подпись поверх нижней части фото (2 строки);
  • ряд из 4 логотипов партнёров (assets/logos/01..04);
  • курсивный футер.

Подпись и футер берутся из локации (locations.json → card_caption / card_footer),
логотипы — детерминированно из assets/logos (ADR-1, ИИ их не рисует).
"""
from __future__ import annotations

import io
from pathlib import Path
from typing import List, Optional

from PIL import Image, ImageOps, ImageDraw, ImageFont, ImageFilter, ImageStat

import config

FONTS = config.BASE_DIR / "assets" / "fonts"

# --- размеры карточки (портрет, ~ полароид) ---
CARD_W = 1200
MARGIN = 56                       # белое поле полароида по бокам/сверху
PHOTO_RATIO = 1.30                # высота фото = ширина * ratio
LOGO_H = 96
FOOTER_GAP = 64

CARD_BG = (255, 255, 255)         # белое поле полароида
INK = (18, 18, 22)                # графит — основной текст карточки
FOOTER_COL = (110, 110, 120)      # приглушённый серый футера
BORDER_COL = (214, 220, 226)      # тонкая рамка вокруг фото


def _font(kind: str, size: int):
    """Надёжный подбор шрифта: бандл в assets/fonts → системный Linux(DejaVu) → Windows(Arial)."""
    table = {
        # подпись — рукописный с кириллицей (Caveat, как согласовано)
        "script": [FONTS / "Caveat.ttf", FONTS / "MarckScript.ttf",
                   "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Italic.ttf",
                   "C:/Windows/Fonts/segoesc.ttf", "C:/Windows/Fonts/ariali.ttf"],
        # футер — классический наборный курсив с засечками
        "italic": [FONTS / "PTSerif-Italic.ttf", FONTS / "PlayfairItalic.ttf",
                   "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Italic.ttf",
                   "C:/Windows/Fonts/ariali.ttf", "C:/Windows/Fonts/timesi.ttf"],
        "regular": [FONTS / "DejaVuSans.ttf",
                    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                    "C:/Windows/Fonts/arial.ttf"],
        "bold": [FONTS / "DejaVuSans-Bold.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                 "C:/Windows/Fonts/arialbd.ttf"],
    }
    # страховка от «квадратиков»: если специфичный шрифт не нашёлся — берём
    # бандл DejaVuSans (кириллица есть), и лишь в самом конце load_default.
    for p in list(table.get(kind, table["regular"])) + [FONTS / "DejaVuSans.ttf"]:
        try:
            return ImageFont.truetype(str(p), size)
        except Exception:
            continue
    return ImageFont.load_default()


def _load_logos() -> List[Image.Image]:
    """Логотипы слева направо, порядок задан именами файлов (00, 02, 03…).

    Берём ЦВЕТНЫЕ версии из logos_card: в assets/logos лежат белые — они для
    тёмного интерфейса киоска и на белой карточке были бы не видны.
    """
    logos: List[Image.Image] = []
    if config.CARD_LOGOS.exists():
        for p in sorted(config.CARD_LOGOS.glob("0*.png")):
            try:
                logos.append(Image.open(p).convert("RGBA"))
            except Exception:
                pass
    return logos


def _text_w(draw, text, font):
    b = draw.textbbox((0, 0), text, font=font)
    return b[2] - b[0]


def build_card(generated_png: bytes,
               caption_lines: Optional[List[str]] = None,
               footer: str = "") -> bytes:
    caption_lines = caption_lines or []

    photo_w = CARD_W - 2 * MARGIN
    photo_h = int(photo_w * PHOTO_RATIO)
    photo_top = MARGIN
    logos_top = photo_top + photo_h + 54
    footer_y = logos_top + LOGO_H + FOOTER_GAP
    # Нижнее поле подобрано так, чтобы карточка была ровно 1200x1800: это 4x6" (10x15 см)
    # при 300 dpi, родной формат принтера DNP QW410 на форуме. При 1780 драйвер
    # растягивал картинку или оставлял белую полоску.
    card_h = footer_y + 116

    card = Image.new("RGB", (CARD_W, card_h), CARD_BG)
    draw = ImageDraw.Draw(card)

    # --- фото ---
    photo = ImageOps.exif_transpose(Image.open(io.BytesIO(generated_png))).convert("RGB")
    photo = ImageOps.fit(photo, (photo_w, photo_h), Image.LANCZOS)

    # рукописная подпись поверх нижней части фото (белым, с мягкой тенью)
    if caption_lines:
        ov = Image.new("RGBA", photo.size, (0, 0, 0, 0))
        od = ImageDraw.Draw(ov)
        x = 46
        avail = photo.width - 2 * x          # ширина, в которую обязана влезть строка
        # размер шрифта подбираем под самую длинную строку: у локаций разной длины
        # подписи, и при фиксированном кегле длинные обрезались по краю карточки
        size = 74
        while size > 34:
            f_cap = _font("script", size)
            if max(_text_w(od, ln, f_cap) for ln in caption_lines) <= avail:
                break
            size -= 2
        line_h = int(size * 1.19)
        y = photo.height - 40 - line_h * len(caption_lines)

        # Подложка под подпись: мягкое затемнение снизу вверх. Без неё белый
        # рукописный текст тонул на светлых кадрах — на «Столбах» подпись
        # ложится на небо и долину и не читалась вовсе.
        band = int(min(photo.height, (photo.height - y) + line_h))

        # Сила подложки — ПО ЯРКОСТИ кадра под подписью. Фиксированные 165
        # хватало тёмному лесу, но не светлой долине Столбов: там текст всё
        # равно сливался. Меряем и добавляем ровно столько, сколько нужно.
        strip = photo.crop((0, photo.height - band, photo.width, photo.height))
        lum = ImageStat.Stat(strip.convert("L")).mean[0]
        peak = int(min(240, max(120, 120 + (lum - 60) * 1.15)))
        grad = Image.new("L", (1, band))
        for i in range(band):
            k = i / max(band - 1, 1)                 # 0 сверху → 1 снизу
            grad.putpixel((0, i), int(peak * (k ** 1.6)))
        scrim = Image.new("RGBA", (photo.width, band), (0, 0, 0, 0))
        scrim.putalpha(grad.resize((photo.width, band)))
        ov.alpha_composite(scrim, (0, photo.height - band))

        shadow_a = 130 if lum < 120 else 190      # на светлом фоне тень плотнее
        for ln in caption_lines:
            od.text((x + 2, y + 2), ln, font=f_cap, fill=(0, 0, 0, shadow_a))   # тень
            od.text((x, y), ln, font=f_cap, fill=(255, 255, 255, 235))
            y += line_h
        photo = Image.alpha_composite(photo.convert("RGBA"), ov).convert("RGB")

    card.paste(photo, (MARGIN, photo_top))
    draw.rectangle([MARGIN, photo_top, MARGIN + photo_w, photo_top + photo_h],
                   outline=BORDER_COL, width=2)

    # --- ряд логотипов ---
    # Выравнивание как на референсе: не по одной высоте, а по визуальному «весу»
    # (равная площадь). Квадратные (ТПП, САХАЛИН) выходят выше, широкие
    # (СМНМ ВИКО, DeltaЛизинг) — ниже, ряд смотрится ровным.
    logos = _load_logos()
    if logos:
        target_area = LOGO_H * LOGO_H * 2.4      # подобрано под макет
        scaled = []
        for lg in logos:
            a = lg.width / lg.height
            h = int((target_area / a) ** 0.5)
            h = max(72, min(int(LOGO_H * 1.55), h))   # квадратные ↑, широкие ↓
            scaled.append(lg.resize((max(1, int(h * a)), h), Image.LANCZOS))
        gap = 72
        total = sum(s.width for s in scaled) + gap * (len(scaled) - 1)
        # не вылезаем за поля
        if total > photo_w:
            k = photo_w / total
            scaled = [s.resize((max(1, int(s.width * k)), max(1, int(s.height * k))), Image.LANCZOS) for s in scaled]
            gap = int(gap * k)
            total = sum(s.width for s in scaled) + gap * (len(scaled) - 1)
        x = (CARD_W - total) // 2
        row_h = max(s.height for s in scaled)
        for s in scaled:
            y = logos_top + (row_h - s.height) // 2
            card.paste(s, (x, y), s)
            x += s.width + gap

    # --- футер --- (рукописный Caveat, как было согласовано)
    if footer:
        f_foot = _font("script", 44)
        fw = _text_w(draw, footer, f_foot)
        draw.text(((CARD_W - fw) // 2, footer_y), footer, font=f_foot, fill=FOOTER_COL)

    out = io.BytesIO()
    card.save(out, format="PNG")
    return out.getvalue()

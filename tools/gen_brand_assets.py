"""Генерация фирменных ассетов «Ремтехники» из векторных контуров.

ВНИМАНИЕ: контуры знака «rt» здесь — ТРАССИРОВКА по растровому макету, а не
оригинальный вектор заказчика. Как только придёт исходник (SVG/AI/EPS) —
заменить файлы в assets/logos и assets/logos_card напрямую, скрипт не нужен.

Запуск:  python tools/gen_brand_assets.py
"""
from pathlib import Path
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
LOGOS = ROOT / "app" / "backend" / "assets" / "logos"
CARD = ROOT / "app" / "backend" / "assets" / "logos_card"
FRONT = ROOT / "app" / "frontend" / "media"

YELLOW = (255, 194, 14, 255)
BLACK = (13, 13, 15, 255)
WHITE = (255, 255, 255, 255)

W, H = 800, 460          # логическая система координат знака
SS = 4                   # суперсэмплинг: рисуем крупно, потом уменьшаем

# Плашка: срезаны верхний левый и нижний правый углы (45°)
BADGE = [(0, 120), (120, 0), (W, 0), (W, 340), (680, H), (0, H)]

# Буква «r»: стойка + плечо со скосом сверху
R_GLYPH = [(142, 137), (276, 110), (395, 110), (395, 211),
           (232, 211), (232, 387), (142, 387)]

# Буква «t» — три перекрывающихся контура: стойка со скосом сверху, перекладина, лапка
T_STEM = [(488, 103), (535, 54), (592, 54), (592, 387), (488, 387)]
T_BAR = [(430, 123), (658, 123), (658, 211), (430, 211)]
T_FOOT = [(592, 303), (647, 303), (647, 387), (592, 387)]


def _draw(img_size, shapes, scale=1.0, offset=(0, 0)):
    """Рисует список (контур, цвет) с суперсэмплингом и возвращает RGBA."""
    w, h = img_size
    im = Image.new("RGBA", (w * SS, h * SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    ox, oy = offset
    for pts, col in shapes:
        d.polygon([((x * scale + ox) * SS, (y * scale + oy) * SS) for x, y in pts], fill=col)
    return im.resize((w, h), Image.LANCZOS)


def mark(width=800, badge=YELLOW, ink=BLACK):
    """Знак целиком: плашка + буквы."""
    s = width / W
    return _draw((width, round(H * s)),
                 [(BADGE, badge), (R_GLYPH, ink), (T_STEM, ink), (T_BAR, ink), (T_FOOT, ink)],
                 scale=s)


def lettering(width=800, ink=WHITE):
    """Только буквы, без плашки — для тёмных полос интерфейса."""
    s = width / W
    return _draw((width, round(H * s)),
                 [(R_GLYPH, ink), (T_STEM, ink), (T_BAR, ink), (T_FOOT, ink)],
                 scale=s)


# ─── Плашки-заглушки вместо эталонных кадров площадок ───────────────────────
# Нужны только чтобы флоу проходился целиком до приезда реальных съёмок.
# Как только придут кадры — положить их в assets/references под теми же именами
# (cabin.jpg / site.jpg / service.jpg) и удалить эту функцию из прогона.
REFS = ROOT / "app" / "backend" / "assets" / "references"
PLACEHOLDERS = {
    "cabin.jpg": "КАБИНА",
    "site.jpg": "ОБЪЕКТ",
    "service.jpg": "СЕРВИС",
}


def _font(size):
    for path in ("C:/Windows/Fonts/arialbd.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
        try:
            from PIL import ImageFont
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    from PIL import ImageFont
    return ImageFont.load_default()


def placeholders(w=1200, h=1600):
    REFS.mkdir(parents=True, exist_ok=True)
    for name, label in PLACEHOLDERS.items():
        im = Image.new("RGB", (w, h), (20, 20, 23))
        d = ImageDraw.Draw(im)
        for x in range(-h, w, 90):                      # косая «дорожная» штриховка
            d.polygon([(x, h), (x + 34, h), (x + 34 + h, 0), (x + h, 0)], fill=(28, 28, 32))
        badge = mark(360)
        im.paste(badge, ((w - badge.width) // 2, h // 2 - 300), badge)
        f1, f2 = _font(96), _font(40)
        for text, font, y, col in ((label, f1, h // 2 + 20, YELLOW[:3]),
                                   ("ЗАМЕНИТЬ НА ФОТО ПЛОЩАДКИ", f2, h // 2 + 150, (150, 150, 158))):
            tw = d.textbbox((0, 0), text, font=font)[2]
            d.text(((w - tw) // 2, y), text, font=font, fill=col)
        im.save(REFS / name, quality=92)


def main():
    for d in (LOGOS, CARD, FRONT):
        d.mkdir(parents=True, exist_ok=True)

    # Веб-интерфейс: знак стоит в топбаре напрямую, поэтому имя с подчёркивания —
    # так он не попадает в /api/logos (та полоса для логотипов партнёров)
    mark(900).save(LOGOS / "_rt_badge.png")
    # Печатная карточка (белый фон) — тот же знак, буквы чёрные
    mark(900).save(CARD / "01_remtech.png")
    # Знак для мерча: уходит в генерацию ОТДЕЛЬНЫМ изображением (config.BRAND_LOGO_FILE).
    # С подчёркивания — чтобы не попасть ни в /api/logos, ни в ряд на карточке.
    mark(1200).save(LOGOS / "_brand_cap.png")
    # Монохромная версия на случай тёмной плашки
    lettering(900).save(LOGOS / "_rt_white.png")
    placeholders()
    print("ok:", LOGOS, CARD)


if __name__ == "__main__":
    main()

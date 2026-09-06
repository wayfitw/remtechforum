"""Импорт логотипов партнёров в оба набора: белый для интерфейса, цветной для карточки.

Исходники приходят растром на светлой подложке (jpg из презентаций и с сайтов).
Скрипт вырезает фон по цвету углов, обрезает поля и раскладывает результат:

  assets/logos/NN_name.png       — белая монохромная версия (тёмный интерфейс киоска)
  assets/logos_card/NN_name.png  — исходные цвета (белая печатная карточка)

Прозрачность считается по расстоянию до цвета фона, а не порогом: слабый
водяной знак (пила у ExpoDrev) остаётся полупрозрачным, а не превращается
в сплошное белое пятно.

Запуск:  python tools/import_partner_logos.py
"""
from pathlib import Path

from PIL import Image, ImageOps
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
LOGOS = ROOT / "app" / "backend" / "assets" / "logos"
CARD = ROOT / "app" / "backend" / "assets" / "logos_card"
SRC = Path.home() / "Desktop" / "RemTech Форум"

# Порядок в ряду задаётся номером в имени: логотипы читаются отсортированными.
# 01 у карточки — сама «Ремтехника», партнёры идут следом.
SOURCES = [
    ("02_expodrev", SRC / "Expo.jpg"),
    ("03_segezha", SRC / "segezha.jpg"),
    ("04_ratibor", SRC / "Ратибор.jpg"),
]
# «Интеллектуальные терминалы» уже есть готовой белой версией в проекте «Сахалин» —
# берём её как есть, для карточки перекрашиваем в графит.
INT_SRC = (ROOT.parent / "mashina-vremeni-sakhalin" / "app" / "backend" /
           "assets" / "logos" / "06_iqter_int.png")
INT_NAME = "05_int"

CARD_INK = (26, 26, 30)     # графит: белый знак на белой карточке не виден
FULL_AT = 110.0             # разница с фоном, начиная с которой пиксель непрозрачен
NOISE_FLOOR = 14            # ниже этой разницы считаем, что это фон, а не знак


def cut_background(im: Image.Image) -> Image.Image:
    """Вырезает однородный фон и обрезает поля. Возвращает RGBA."""
    rgb = np.asarray(im.convert("RGB")).astype(np.float32)
    h, w, _ = rgb.shape
    # цвет фона — медиана по четырём углам, чтобы не поймать пиксель самого знака
    k = max(2, min(h, w) // 40)
    corners = np.concatenate([rgb[:k, :k].reshape(-1, 3), rgb[:k, -k:].reshape(-1, 3),
                              rgb[-k:, :k].reshape(-1, 3), rgb[-k:, -k:].reshape(-1, 3)])
    bg = np.median(corners, axis=0)

    dist = np.abs(rgb - bg).max(axis=2)
    # шум JPEG на белом поле даёт разницу 3-10 — без порога он остаётся еле
    # видимой мутью и мешает обрезке полей по bbox
    dist[dist < NOISE_FLOOR] = 0
    alpha = np.clip(dist / FULL_AT * 255.0, 0, 255).astype(np.uint8)
    out = Image.fromarray(np.dstack([np.asarray(im.convert("RGB")), alpha]), "RGBA")
    return out.crop(out.getbbox() or (0, 0, w, h))


def to_white(im: Image.Image) -> Image.Image:
    a = im.getchannel("A")
    white = Image.new("RGBA", im.size, (255, 255, 255, 0))
    white.putalpha(a)
    return white


def to_ink(im: Image.Image, col=CARD_INK) -> Image.Image:
    a = im.getchannel("A")
    ink = Image.new("RGBA", im.size, col + (0,))
    ink.putalpha(a)
    return ink


def main():
    LOGOS.mkdir(parents=True, exist_ok=True)
    CARD.mkdir(parents=True, exist_ok=True)

    for name, path in SOURCES:
        if not path.exists():
            print("SKIP (нет файла):", path)
            continue
        cut = cut_background(ImageOps.exif_transpose(Image.open(path)))
        # в интерфейсе — белая версия, на карточке — исходные цвета
        to_white(cut).save(LOGOS / f"{int(name[:2]) - 1:02d}_{name[3:]}.png")
        cut.save(CARD / f"{name}.png")
        print("ok:", name, cut.size)

    if INT_SRC.exists():
        white = Image.open(INT_SRC).convert("RGBA")
        white = white.crop(white.getbbox() or (0, 0, *white.size))
        white.save(LOGOS / f"{int(INT_NAME[:2]) - 1:02d}_{INT_NAME[3:]}.png")
        to_ink(white).save(CARD / f"{INT_NAME}.png")
        print("ok:", INT_NAME, white.size)
    else:
        print("SKIP (нет файла):", INT_SRC)


if __name__ == "__main__":
    main()

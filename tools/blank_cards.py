"""Карточки-макеты без гостя: только площадка, подпись и логотипы.

Кадр режется не по центру, а со смещением к технике: иначе у лесосеки форвардер
уезжает за правый край, а в цеху машина обрезается наполовину.
"""
import io, json, os, sys
from pathlib import Path

BACKEND = Path(r"C:/Users/On My Way/source/repos/remtech-photo/app/backend")
sys.path.insert(0, str(BACKEND)); os.chdir(BACKEND)
from PIL import Image, ImageDraw, ImageFont      # noqa: E402
import config, compositor                        # noqa: E402

SP = Path(sys.argv[1])
OUT = SP / "blank_cards"; OUT.mkdir(parents=True, exist_ok=True)
LOCS = json.loads((BACKEND / "locations.json").read_text(encoding="utf-8"))

# Куда смещать кадр (доля кадра, 0.5 — центр): по горизонтали и вертикали.
FOCUS = {
    "cabin":   (0.50, 0.55),   # кресло и джойстики
    "site":    (0.72, 0.55),   # форвардер справа
    "service": (0.40, 0.50),   # машина слева от центра
    "stolby":  (0.70, 0.55),   # правый останец крупно
    "bear":    (0.50, 0.50),   # кадр и так вертикальный
}
RATIO = 1 / compositor.PHOTO_RATIO      # ширина/высота фото на карточке


def crop_to_card(im: Image.Image, fx: float, fy: float) -> bytes:
    W, H = im.size
    h = min(H, W / RATIO)
    w = h * RATIO
    x0 = min(max(fx * W - w / 2, 0), W - w)
    y0 = min(max(fy * H - h / 2, 0), H - h)
    im = im.crop((round(x0), round(y0), round(x0 + w), round(y0 + h)))
    buf = io.BytesIO(); im.save(buf, format="PNG")
    return buf.getvalue()


cards = []
for lid, v in LOCS.items():
    fx, fy = FOCUS.get(lid, (0.5, 0.5))
    photo = crop_to_card(Image.open(config.REFERENCES / v["reference"]).convert("RGB"), fx, fy)
    png = compositor.build_card(photo, v["card_caption"], v["card_footer"])
    (OUT / f"{lid}.png").write_bytes(png)
    cards.append((v["title"], Image.open(io.BytesIO(png)).convert("RGB")))
    print("ok:", lid)

H, PAD, TOP = 1180, 30, 70
ims = [(t, c.resize((int(H * c.width / c.height), H), Image.LANCZOS)) for t, c in cards]
W = sum(i.width for _, i in ims) + PAD * (len(ims) + 1)
sheet = Image.new("RGB", (W, H + TOP + PAD), (24, 24, 28))
d = ImageDraw.Draw(sheet)
try:
    F = ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", 30)
except Exception:
    F = ImageFont.load_default()
x = PAD
for title, im in ims:
    sheet.paste(im, (x, TOP))
    d.text((x, 24), title, font=F, fill=(255, 194, 14))
    x += im.width + PAD
out = SP / "blank_cards.jpg"; sheet.save(out, quality=90); print(out)

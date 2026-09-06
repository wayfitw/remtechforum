"""Кроп ближе + спокойная цветокоррекция. Без генерации: лицо и знак не трогаются."""
import sys
from pathlib import Path
import numpy as np
from PIL import Image, ImageFilter

SRC = Path(sys.argv[1])
DST = Path(sys.argv[2])
ZOOM = float(sys.argv[3]) if len(sys.argv) > 3 else 1.25
CX, CY = (float(sys.argv[4]), float(sys.argv[5])) if len(sys.argv) > 5 else (0.52, 0.60)

im = Image.open(SRC).convert("RGB")
W, H = im.size

# --- кроп с сохранением пропорций ---
w, h = W / ZOOM, H / ZOOM
x0 = min(max(CX * W - w / 2, 0), W - w)
y0 = min(max(CY * H - h / 2, 0), H - h)
im = im.crop((round(x0), round(y0), round(x0 + w), round(y0 + h))).resize((W, H), Image.LANCZOS)

a = np.asarray(im).astype(np.float32) / 255.0

# --- насыщенность: гасим общий «отпуск в инстаграме», зелень отдельно ---
lum = a @ np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)
lum3 = lum[..., None]
a = lum3 + (a - lum3) * 0.90                       # общая насыщенность -10%
green = np.clip((a[..., 1] - (a[..., 0] + a[..., 2]) / 2) * 2.2, 0, 1)[..., None]
a = a * (1 - green * 0.13) + lum3 * (green * 0.13)  # зелень ещё мягче: она и была кислотной

# --- плёночная кривая: подняли тени, придержали света, лёгкий контраст ---
a = np.clip(a, 0, 1)
a = 0.022 + a * 0.965                               # чёрный поднят чуть-чуть, без мути
a = a + 0.22 * (a - 0.5) * (1 - np.abs(a - 0.5) * 2)  # мягкая S-кривая

# --- баланс: теплее в светах, холоднее в тенях ---
shadow = np.clip(1 - lum, 0, 1)[..., None]
a[..., 0] += 0.022 * (1 - shadow[..., 0])
a[..., 2] += 0.018 * shadow[..., 0]

# --- виньетка ---
yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
r = np.sqrt(((xx / W - 0.5) * 2) ** 2 + ((yy / H - 0.5) * 2) ** 2) / 1.414
a *= (1 - 0.10 * np.clip(r, 0, 1) ** 2)[..., None]

out = Image.fromarray((np.clip(a, 0, 1) * 255).astype(np.uint8), "RGB")
# лёгкая резкость после ресайза
out = out.filter(ImageFilter.UnsharpMask(radius=1.6, percent=55, threshold=3))
out.save(DST, quality=95)
print("OK", DST, out.size)

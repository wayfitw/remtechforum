"""Авто-обрезка фото гостя до «голова + плечи».

Убирает лишнее из кадра (комната, предметы, посторонние), даже если гость стоял
далеко — модель получает чистый крупный портрет и точно берёт лицо человека.
Если лицо не найдено — центральная вертикальная обрезка (fallback).
"""
from __future__ import annotations

import io

from PIL import Image, ImageDraw, ImageFilter, ImageOps

# opencv нужен для выравнивания света (deshadow). Детектор Хаара — отдельно:
# в OpenCV 5 класса CascadeClassifier и xml-файлов каскадов больше нет, и раньше
# его падение обнуляло весь cv2. Итог (13.09.2026): deshadow молча не работал, а
# кроп лица всегда уходил в запасной вариант «верхняя половина кадра» и резал
# лицо по нос — модель не видела рот, щёки и челюсть и дорисовывала их опухшими.
try:
    import numpy as np
    import cv2
except Exception:  # noqa: BLE001
    np = None
    cv2 = None

_CASCADE = None
if cv2 is not None and hasattr(cv2, "CascadeClassifier"):
    try:
        _CASCADE = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        if _CASCADE.empty():
            _CASCADE = None
    except Exception:  # noqa: BLE001
        _CASCADE = None


def _all_faces(img: Image.Image):
    """(рамка гостя, [рамки остальных лиц]) в виде (x1, y1, x2, y2), либо (None, []).
    Гость — самое крупное лицо, как и в гейте."""
    try:
        import face_metric  # ленивый импорт: модуль тяжёлый и тянет config
        buf = io.BytesIO(); img.save(buf, format="JPEG", quality=95)
        faces = face_metric._faces(buf.getvalue())
        if faces:
            guest = face_metric._largest(faces)
            others = [tuple(float(v) for v in f.bbox) for f in faces if f is not guest]
            return tuple(float(v) for v in guest.bbox), others
    except Exception as exc:  # noqa: BLE001
        print(f"[facecrop] insightface недоступен: {exc}")
    return None, []


def _face_box(img: Image.Image):
    """Рамка самого крупного лица (x, y, w, h) или None.

    Основной детектор — insightface: он уже загружен для входного контроля и
    работает на любой версии OpenCV. Хаар — только запасной, если insightface
    недоступен."""
    guest, _ = _all_faces(img)
    if guest is not None:
        x1, y1, x2, y2 = guest
        return x1, y1, x2 - x1, y2 - y1
    if _CASCADE is not None:
        W, H = img.size
        gray = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2GRAY)
        faces = _CASCADE.detectMultiScale(gray, scaleFactor=1.15, minNeighbors=5,
                                          minSize=(int(min(W, H) * 0.06), int(min(W, H) * 0.06)))
        if len(faces):
            return tuple(float(v) for v in max(faces, key=lambda f: f[2] * f[3]))
    return None


DESHADOW_DARK_FACE = 95      # средняя яркость лица (L, 0–255), ниже — лицо тёмное
DESHADOW_SIDE_DIFF = 22      # разница яркости левой и правой половины лица — боковая тень
DESHADOW_STRENGTH = 0.6      # доля CLAHE в итоге: полная сила давала «пережаренный» кадр


def deshadow(image_bytes: bytes, clip: float = 2.0, bbox=None) -> bytes | None:
    """Выравнивает освещение кадра с вебки: поднимает тени (CLAHE по яркости L).
    Черты лица НЕ меняются — правится только свет.

    Работает, только если на лице есть что исправлять: лицо тёмное или одна
    половина заметно темнее другой. Без этой проверки CLAHE шёл по каждому
    кадру и на нормальном свете только добавлял зерно и жёсткий контраст
    (прогон 16.09.2026). None — кадр оставлен как есть."""
    if cv2 is None:
        return None
    try:
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(image_bytes))).convert("RGB")
        lab = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2LAB)
        l, a, b = cv2.split(lab)
        if bbox:
            # Мерим по щекам и средней части лица: края рамки захватывают волосы,
            # и прядь с одной стороны давала ложный «перепад света».
            x1, y1, x2, y2 = (float(v) for v in bbox)
            fw, fh = x2 - x1, y2 - y1
            face = l[max(0, int(y1 + fh * 0.35)):max(0, int(y1 + fh * 0.80)),
                     max(0, int(x1 + fw * 0.15)):max(0, int(x2 - fw * 0.15))]
            if face.size:
                mid = face.shape[1] // 2
                mean = float(face.mean())
                side = abs(float(face[:, :mid].mean()) - float(face[:, mid:].mean()))
                if mean >= DESHADOW_DARK_FACE and side < DESHADOW_SIDE_DIFF:
                    print(f"[deshadow] пропущен — свет на лице ровный (яркость {mean:.0f}, перепад {side:.0f})")
                    return None
        eq = cv2.createCLAHE(clipLimit=clip, tileGridSize=(8, 8)).apply(l)
        l = cv2.addWeighted(eq, DESHADOW_STRENGTH, l, 1 - DESHADOW_STRENGTH, 0)
        out = cv2.cvtColor(cv2.merge((l, a, b)), cv2.COLOR_LAB2RGB)
        buf = io.BytesIO(); Image.fromarray(out).save(buf, format="PNG")
        return buf.getvalue()
    except Exception as exc:  # noqa: BLE001
        print(f"[deshadow] failed: {exc}")
        return None


def paste_face(base_bytes: bytes, enhanced_bytes: bytes, bbox) -> bytes | None:
    """Вклеивает лицо из результата GFPGAN в исходный кадр того же размера.

    GFPGAN возвращает кадр в 2 раза крупнее, а фон при этом перерисовывает
    апскейлером: он «мылится» и выглядит нарисованным. Нужна только его работа
    над лицом, поэтому результат уменьшается до размера исходника и
    переносится мягкой овальной маской вокруг лица. None — не получилось."""
    if not bbox or not enhanced_bytes:
        return None
    try:
        base = ImageOps.exif_transpose(Image.open(io.BytesIO(base_bytes))).convert("RGB")
        enh = Image.open(io.BytesIO(enhanced_bytes)).convert("RGB").resize(base.size, Image.LANCZOS)
        x1, y1, x2, y2 = (float(v) for v in bbox)
        fw, fh = x2 - x1, y2 - y1
        cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
        mask = Image.new("L", base.size, 0)
        ImageDraw.Draw(mask).ellipse([cx - fw * 0.72, cy - fh * 0.78, cx + fw * 0.72, cy + fh * 0.68], fill=255)
        mask = mask.filter(ImageFilter.GaussianBlur(max(4, fw * 0.1)))
        out = base.copy()
        out.paste(enh, (0, 0), mask)
        buf = io.BytesIO(); out.save(buf, format="PNG")
        return buf.getvalue()
    except Exception as exc:  # noqa: BLE001
        print(f"[facecrop] не удалось вклеить лицо GFPGAN: {exc}")
        return None


def hide_others(img: Image.Image, guest, others) -> Image.Image:
    """Стирает посторонних людей с фото гостя перед вырезками для модели.

    Зачем (прогон 16.09.2026 на кадрах с людьми за спиной): гейт и кроп под
    свап выбирали гостя верно, но вырезка «корпус по пояс» шириной 4 лица
    захватывала соседа — его лицо, волосы, плечо. Модель видит их на эталоне
    гостя и может дорисовать в сцену второго человека.

    Голова и корпус каждого постороннего заливаются окружающим фоном (inpaint),
    голова и плечи гостя закрыты защитной маской и не трогаются. Работает на
    уменьшенной копии — заливке нужна только грубая форма."""
    if not others or cv2 is None or np is None or guest is None:
        return img
    try:
        W, H = img.size
        k = min(1.0, 900 / max(W, H))
        sw, sh = max(1, int(W * k)), max(1, int(H * k))

        mask = Image.new("L", (sw, sh), 0)
        d = ImageDraw.Draw(mask)
        for ox1, oy1, ox2, oy2 in others:
            ow, oh = ox2 - ox1, oy2 - oy1
            ocx = (ox1 + ox2) / 2
            d.ellipse([(ox1 - ow * 0.5) * k, (oy1 - oh * 0.6) * k,
                       (ox2 + ow * 0.5) * k, (oy2 + oh * 0.35) * k], fill=255)       # голова и волосы
            d.rectangle([(ocx - ow * 1.5) * k, (oy2 - oh * 0.1) * k,
                         (ocx + ow * 1.5) * k, sh], fill=255)                         # плечи и корпус

        gx1, gy1, gx2, gy2 = guest
        gw, gh = gx2 - gx1, gy2 - gy1
        gcx = (gx1 + gx2) / 2
        protect = Image.new("L", (sw, sh), 0)
        p = ImageDraw.Draw(protect)
        p.ellipse([(gx1 - gw * 0.35) * k, (gy1 - gh * 0.5) * k,
                   (gx2 + gw * 0.35) * k, (gy2 + gh * 0.25) * k], fill=255)
        p.polygon([((gcx - gw * 0.6) * k, (gy2 - gh * 0.2) * k), ((gcx + gw * 0.6) * k, (gy2 - gh * 0.2) * k),
                   ((gcx + gw * 2.0) * k, (gy2 + gh * 0.9) * k), ((gcx + gw * 2.0) * k, sh),
                   ((gcx - gw * 2.0) * k, sh), ((gcx - gw * 2.0) * k, (gy2 + gh * 0.9) * k)], fill=255)

        m = (np.array(mask) > 0) & (np.array(protect) == 0)
        if not m.any():
            return img
        m8 = (m * 255).astype(np.uint8)
        small = cv2.cvtColor(np.array(img.resize((sw, sh), Image.LANCZOS)), cv2.COLOR_RGB2BGR)
        filled = cv2.inpaint(small, m8, 9, cv2.INPAINT_TELEA)
        filled = Image.fromarray(cv2.cvtColor(filled, cv2.COLOR_BGR2RGB)).resize((W, H), Image.LANCZOS)
        soft = Image.fromarray(m8).resize((W, H), Image.BILINEAR).filter(ImageFilter.GaussianBlur(max(2, 4 / k)))
        out = img.copy()
        out.paste(filled.filter(ImageFilter.GaussianBlur(max(1, 2 / k))), (0, 0), soft)
        return out
    except Exception as exc:  # noqa: BLE001
        print(f"[facecrop] не удалось скрыть посторонних: {exc}")
        return img


def crop_for_swap(image_bytes: bytes, bbox, others=None) -> bytes | None:
    """Область вокруг лица ГОСТЯ для face-swap.

    Зачем: свапу нужен контекст вокруг лица, иначе его детектор его не находит
    (проверено 27.07.2026 — тесный кроп давал «No face found»). Но на форуме в
    кадр попадают прохожие, и свап может перенести чужое лицо. Поэтому берём
    щедрую рамку и ЗАЖИМАЕМ её по рамкам посторонних лиц — они уже известны от
    insightface, так что второй проход детекции не нужен.

    bbox   — рамка лица гостя (insightface уже выбрал самое крупное);
    others — рамки остальных найденных лиц, если их было больше одного.
    """
    try:
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(image_bytes))).convert("RGB")
        W, H = img.size
        x1, y1, x2, y2 = (float(v) for v in bbox)
        cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
        fw, fh = x2 - x1, y2 - y1

        # желаемая рамка — 2.6x лица: детектору свапа контекста хватает с запасом
        left, top = cx - fw * 1.3, cy - fh * 1.3
        right, bottom = cx + fw * 1.3, cy + fh * 1.3

        # отступаем от каждого посторонного лица, чтобы оно не попало в кадр
        M = 8  # небольшой зазор, чтобы край чужого лица не задевало
        for o in (others or []):
            ox1, oy1, ox2, oy2 = (float(v) for v in o)
            if ox2 <= x1:                      # посторонний слева
                left = max(left, ox2 + M)
            elif ox1 >= x2:                    # справа
                right = min(right, ox1 - M)
            if oy2 <= y1:                      # выше
                top = max(top, oy2 + M)
            elif oy1 >= y2:                    # ниже
                bottom = min(bottom, oy1 - M)

        # рамка не должна стать теснее самого лица с небольшим полем
        pad = 0.12
        left = min(left, x1 - fw * pad); right = max(right, x2 + fw * pad)
        top = min(top, y1 - fh * pad);  bottom = max(bottom, y2 + fh * pad)

        box = (max(0, int(left)), max(0, int(top)),
               min(W, int(right)), min(H, int(bottom)))
        if box[2] - box[0] < 96 or box[3] - box[1] < 96:
            return None
        buf = io.BytesIO(); img.crop(box).save(buf, format="PNG")
        return buf.getvalue()
    except Exception as exc:  # noqa: BLE001
        print(f"[crop_for_swap] failed: {exc}")
        return None


def crops(image_bytes: bytes) -> tuple[bytes, bytes]:
    """Возвращает (крупное_лицо, корпус_по_пояс) как два PNG.
    Лицо отдельно — чтобы модель точно скопировала черты; корпус — чтобы видела
    реальное телосложение. Оба уходят в модель вместе с эталоном сцены."""
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(image_bytes))).convert("RGB")
    W, H = img.size

    guest, others = _all_faces(img)
    if guest is not None and others:
        img = hide_others(img, guest, others)
        print(f"[facecrop] посторонних на фото гостя: {len(others)}, скрыты перед вырезками")
    box = (guest[0], guest[1], guest[2] - guest[0], guest[3] - guest[1]) if guest else _face_box(img)
    if box is not None:
        x, y, w, h = box
        cx, cy = x + w / 2, y + h / 2
        # 1) крупное лицо: теснее кадрируем (лицо крупнее в референсе → выше сходство)
        fw = max(w * 1.7, 512 if W >= 512 else W)
        fh = fw * 1.25
        face_box = (max(0, int(cx - fw / 2)), max(0, int(cy - fh * 0.45)),
                    min(W, int(cx + fw / 2)), min(H, int(cy + fh * 0.55)))
        face_img = img.crop(face_box)
    else:
        # лицо не нашли — верхняя центральная треть
        face_img = img.crop((int(W * 0.25), 0, int(W * 0.75), int(H * 0.5)))

    # крупный резкий эталон лица: апскейл до мин. 1024 px по длинной стороне
    # (рек. ⑤ — реф от 1024 px заметно улучшает сходство)
    _MIN_FACE = 1024
    if max(face_img.size) < _MIN_FACE:
        _s = _MIN_FACE / max(face_img.size)
        face_img = face_img.resize((int(face_img.width * _s), int(face_img.height * _s)), Image.LANCZOS)

    body_png = _body_crop(img, box)               # корпус по пояс — с того же очищенного фото
    fbuf = io.BytesIO(); face_img.save(fbuf, format="PNG")
    return fbuf.getvalue(), body_png


def crop_to_face(image_bytes: bytes) -> bytes:
    """Возвращает PNG с обрезкой до головы и плеч. Ориентация — вертикальная 3:4."""
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(image_bytes))).convert("RGB")
    return _body_crop(img, _face_box(img))


def _body_crop(img: Image.Image, box) -> bytes:
    """Вырезка «по пояс» вокруг рамки лица box (x, y, w, h); None — запасная область."""
    W, H = img.size
    if box is not None:
        # самое крупное лицо
        x, y, w, h = box
        cx, cy = x + w / 2, y + h / 2
        # рамка «по пояс»: модель должна ВИДЕТЬ телосложение гостя, иначе выдумает его.
        # Лицо — в верхней четверти кадра, ниже — плечи/грудь/талия.
        box_w = w * 4.2
        box_h = box_w * 4 / 3            # вертикаль 3:4
        left = cx - box_w / 2
        top = cy - box_h * 0.22          # чуть места над головой, основное — корпус ниже
        # защита от «мыла»: если вырезка получается слишком мелкой (< 640 px по ширине),
        # расширяем её до минимума — лучше больше фона, чем размазанное лицо
        min_w = 640
        if box_w < min_w and W >= min_w:
            grow = min_w / box_w
            box_w *= grow; box_h *= grow
            left = cx - box_w / 2
            top = cy - box_h * 0.22
    else:
        # fallback: центральная вертикальная область, верхняя часть кадра
        box_w = min(W, H * 3 / 4)
        box_h = box_w * 4 / 3
        left = (W - box_w) / 2
        top = max(0, H * 0.05)

    # клампим в границы изображения
    left = max(0, min(left, W - 1))
    top = max(0, min(top, H - 1))
    right = min(W, left + box_w)
    bottom = min(H, top + box_h)
    crop = img.crop((int(left), int(top), int(right), int(bottom)))

    buf = io.BytesIO()
    crop.save(buf, format="PNG")
    return buf.getvalue()

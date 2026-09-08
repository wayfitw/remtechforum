"""Детерминированный композитинг: фон НЕ генерируется вообще.

Схема (как в настоящих фотобудках, аналог green screen):
  1) nano-banana-2 генерит ТОЛЬКО человека — нужная поза/одежда/свет, чистый фон;
  2) background-remover вырезает человека (RGBA);
  3) Pillow вклеивает его в эталонный кадр локации по анкеру (позиция/масштаб
     фиксированы в locations.json) + мягкая тень под ногами + цветоподгонка.

Фон при этом байт-в-байт равен эталону — «уплыть» не может в принципе.
"""
from __future__ import annotations

import io

from PIL import Image, ImageOps, ImageFilter, ImageEnhance, ImageStat

import config
import replicate_client

BG_REMOVER = "851-labs/background-remover:a029dff38972b5fda4ec5d75d7d1cd25aeff621d2cf4946a41055d7db66b80bc"

# Промпт генерации ТОЛЬКО человека (фон нейтральный, вырежется).
# Замок идентичности выверен A/B-прогоном 06.09.2026 на имитациях кадров с вебки:
# среднее сходство по ArcFace 0.647 → 0.697, на хорошем кадре 0.637 → 0.727.
# Ключевое: модели прямо сказано, что на входе СНИМОК С ВЕБКИ — чинить качество,
# но не «чинить» лицо в сторону обобщённо-красивого.
PERSON_PROMPT = (
    "Photorealistic full-length photo of this exact person standing, captured head to shoes. The person "
    "is {WHO}. Keep their REAL AGE and build exactly as in the photos — if the photo shows a child, draw "
    "a child with a child's proportions, never an adult; if an older person, keep their age. Never make "
    "them look older or younger than they are. IMAGE 1 IS A WEBCAM SNAPSHOT of their face: it may be "
    "soft, noisy, dim or low in detail. Restore photographic QUALITY — sharpness, clean skin texture, "
    "correct exposure — but NEVER restore it into a different person: every feature you cannot see "
    "clearly must be reconstructed as the closest match to image 1, not as a generic attractive face. "
    "FACE LOCK — CRITICAL: the face must match image 1 EXACTLY in shape and proportions — same face "
    "width, same jawline, same chin shape, same cheekbones, same nose shape, width and bridge, same eye "
    "shape, size and spacing, same eyelids, same eyebrow shape and thickness, same mouth width and lip "
    "fullness, same hairline, same haircut and hair colour, same facial hair. Keep every personal mark: "
    "moles, freckles, scars, glasses, asymmetries — a real face is never symmetric, do not straighten it. "
    "Do NOT beautify, do NOT slim or widen, do NOT smooth away features, do NOT enlarge the eyes, do NOT "
    "change the skin tone. The result must be recognisable as the SAME person at a glance, by a "
    "colleague, not merely similar. Image 2 shows their true BODY BUILD — match it exactly, not heavier "
    "and not slimmer. Pose: relaxed and natural, body turned slightly at an angle, weight on one leg, one "
    "hand casually in a pocket, light genuine smile — not a stiff frontal passport pose. The head is held "
    "straight and the face is turned TOWARDS the camera, fully visible, nothing covering it. Outfit: "
    "{OUTFIT}. Lighting: {LIGHT} The face is lit evenly and softly, with no hard shadow across it. "
    "SHARPNESS: the FACE is the sharpest element of the whole photograph — tack-sharp eyes with clear "
    "irises and catchlights, defined eyelashes, crisp lip edges, natural skin pores. Render the head with "
    "the highest level of detail in the frame, as if shot on a portrait lens. Background: plain "
    "light-gray seamless studio backdrop, nothing else. Full body fully visible with clear margin around; "
    "feet firmly on the ground. No text, no logos, no props, no extra people, no anatomical distortions. "
)



# Свет по умолчанию — ровный дневной: подходит и пасмурному лесу, и летнему гребню.
# Сцену со своим характерным светом (закат, цех) перебивает поле "light" в locations.json:
# без этого человек с нейтральным светом выглядит наклейкой на золотом контровом кадре.
DEFAULT_LIGHT = ("neutral outdoor daylight from above and slightly to the left, soft and even, "
                 "no warm colour cast.")


def _remove_bg(image_bytes: bytes) -> bytes | None:
    """Вырезает человека: RGBA PNG с прозрачным фоном."""
    if not replicate_client._client:
        return None
    import concurrent.futures
    def _run():
        out = replicate_client._client.run(
            BG_REMOVER, input={"image": replicate_client._data_uri(image_bytes), "format": "png"})
        return replicate_client._read_output(out)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        try:
            return pool.submit(_run).result(timeout=120)
        except Exception as exc:  # noqa: BLE001
            print(f"[composite] remove-bg failed: {exc}")
            return None


def _clean_edge(person: Image.Image, erode: int = 2, feather: float = 1.2) -> Image.Image:
    """Убирает кайму вокруг вырезки.

    Background-remover оставляет по контуру полупрозрачные пиксели, подкрашенные
    СЕРЫМ студийным фоном. На тёмном лесу эта кайма читается как обводка из
    фотошопа. Лечится в два шага: срезаем крайние пиксели (вместе с грязью) и
    растушёвываем оставшийся край, чтобы он не был бритвенно резким.
    """
    a = person.split()[3]
    if erode:
        a = a.filter(ImageFilter.MinFilter(2 * erode + 1))
    if feather:
        a = a.filter(ImageFilter.GaussianBlur(feather))
    out = person.copy(); out.putalpha(a)
    return out


def _match_colors(person: Image.Image, scene: Image.Image, strength: float = 0.28) -> Image.Image:
    """Подгоняет человека под сцену по КАЖДОМУ каналу: среднее и контраст.

    Раньше правилась только общая яркость, поэтому студийный нейтральный свет
    оставался холоднее и контрастнее пасмурного леса — глаз читал наклейку.
    Тянем и цвет, и разброс, но лишь наполовину: полное выравнивание убивает
    объём лица.
    """
    import numpy as np
    ref = scene.crop((0, scene.height // 3, scene.width, scene.height))
    ref_a = np.asarray(ref.convert("RGB")).astype(np.float32).reshape(-1, 3)
    rgb = np.asarray(person.convert("RGB")).astype(np.float32)
    alpha = np.asarray(person.split()[3]).astype(np.float32) / 255.0
    mask = alpha > 0.5
    if mask.sum() < 100:
        return person
    out = rgb.copy()
    for c in range(3):
        p_vals = rgb[..., c][mask]
        p_mean, p_std = p_vals.mean(), max(p_vals.std(), 1.0)
        s_mean, s_std = ref_a[:, c].mean(), max(ref_a[:, c].std(), 1.0)
        # тянем к сцене только на strength, и не даём контрасту уехать больше чем на 25%
        # контраст правим слабо, цвет — ещё слабее: при сильной тяге чёрная
        # спецовка уходила в коричневый под цвет леса
        gain = 1 + (min(max(s_std / p_std, 0.85), 1.15) - 1) * strength
        shift = (s_mean - p_mean) * strength * 0.30
        out[..., c] = (rgb[..., c] - p_mean) * gain + p_mean + shift
    res = Image.fromarray(np.clip(out, 0, 255).astype(np.uint8), "RGB")
    res.putalpha(person.split()[3])
    return res


def _match_texture(person: Image.Image, scene: Image.Image) -> Image.Image:
    """Приводит резкость и зерно человека к сцене.

    Фон снят камерой: у него есть шум и лёгкая нерезкость от глубины кадра.
    Сгенерированный человек идеально чистый и звенящий — именно этот контраст
    и выдаёт монтаж. Слегка размываем и подсыпаем шум под уровень сцены.
    """
    import numpy as np
    band = np.asarray(scene.crop((0, scene.height // 3, scene.width,
                                  scene.height)).convert("L")).astype(np.float32)
    # оценка шума сцены: разница с медианно сглаженной версией
    smooth = np.asarray(Image.fromarray(band.astype(np.uint8)).filter(
        ImageFilter.GaussianBlur(1.2))).astype(np.float32)
    sigma = float(np.clip(np.std(band - smooth), 0.6, 6.0))

    out = person.filter(ImageFilter.GaussianBlur(0.6))          # снимаем «звон»
    arr = np.asarray(out.convert("RGB")).astype(np.float32)
    noise = np.random.default_rng(7).normal(0.0, sigma, arr.shape[:2])[..., None]
    arr = np.clip(arr + noise, 0, 255)
    res = Image.fromarray(arr.astype(np.uint8), "RGB")
    res.putalpha(out.split()[3])
    return res


def _face_height(person: Image.Image) -> float | None:
    """Высота лица на вырезке, px. None — если лица не видно."""
    import io as _io
    flat = Image.new("RGB", person.size, (255, 255, 255))
    flat.paste(person, mask=person.split()[3])
    buf = _io.BytesIO(); flat.save(buf, format="JPEG", quality=92)
    try:
        import face_metric
        faces = face_metric._faces(buf.getvalue())
        if not faces:
            return None
        f = face_metric._largest(faces)
        return float(f.bbox[3] - f.bbox[1])
    except Exception:  # noqa: BLE001 — детектор недоступен, работаем по росту
        return None


def _frame_on_person(scene: Image.Image, box: tuple[int, int, int, int],
                     fill: float | None = None, frame_cx: float | None = None) -> Image.Image:
    """Кадрирует сцену вокруг вклеенной фигуры — как если бы фотограф подошёл ближе.

    Зачем: у фигуры в полный рост голова занимает ~1/7 кадра, и лицо на карточке
    выходило мелким (замеры 05.09.2026: доля лица 0.077-0.093 при пороге отбраковки
    0.10 — семь кадров из десяти ушли бы в брак). Ровно это и обещает промпт:
    поясной портрет, нижний край режет фигуру между бёдрами и коленом.

    Поэтому кадр строится от ЧАСТИ фигуры: сверху голова, снизу срез по бедру.
    Эта часть занимает CARD_FILL высоты кадра, пропорции — как у печатной карточки.
    Если сцены не хватает, берём максимум и не выходим за края.
    """
    px, py, pw, ph = box
    W, H = scene.size
    visible = ph * config.CARD_PERSON_PART          # от макушки до среза по бедру
    fill = config.CARD_FILL if fill is None else fill
    if fill <= 0:
        # «Камера дальше»: кадр берётся во всю высоту эталона и не зависит от
        # фигуры. Размер гостя задаёт анкер — так техника за спиной остаётся
        # такой же крупной, как на самом эталоне.
        crop_h = H
    else:
        crop_h = min(H, visible / max(fill, 0.1))
    crop_w = crop_h * config.CARD_ASPECT
    if crop_w > W:                                  # сцена уже нужного — упираемся в ширину
        crop_w = W
        crop_h = min(H, crop_w / config.CARD_ASPECT)

    # воздух над головой больше, чем под срезом: так кадр не выглядит обрубленным
    above = (crop_h - visible) * 0.8
    # По умолчанию кадр центрируется на госте. frame_cx сдвигает его к технике:
    # иначе машина, стоящая сбоку от гостя, уезжает за край.
    center = (px + pw / 2) if frame_cx is None else (W * frame_cx)
    x0 = min(max(center - crop_w / 2, 0), max(W - crop_w, 0))
    y0 = 0 if fill <= 0 else min(max(py - above, 0), max(H - crop_h, 0))
    return scene.crop((round(x0), round(y0), round(x0 + crop_w), round(y0 + crop_h)))


def compose(person_rgba: bytes, reference_bytes: bytes, anchor: dict,
            fill: float | None = None, frame_cx: float | None = None) -> bytes:
    """Вклеивает вырезанного человека в эталон по анкеру.
    anchor: cx (0..1 центр по X), bottom (0..1 низ ног), height (0..1 рост от высоты кадра)."""
    scene = ImageOps.exif_transpose(Image.open(io.BytesIO(reference_bytes))).convert("RGB")
    person = Image.open(io.BytesIO(person_rgba)).convert("RGBA")

    # обрезаем прозрачные поля вокруг человека
    alpha_bbox = person.split()[3].getbbox()
    # Модель примерно в половине случаев обрезает ноги (замер 08.09.2026: 9 из 21
    # вырезок упирались в нижний край). Такую фигуру нельзя ставить «ногами» на
    # землю: срез оказывается на уровне земли, и человек выглядит обрубленным.
    legs_cropped = bool(alpha_bbox) and alpha_bbox[3] >= person.height - 2
    if alpha_bbox:
        person = person.crop(alpha_bbox)

    # Масштаб задаём по ЛИЦУ, а не по росту вырезки. Иначе он скачет: модель то
    # рисует фигуру целиком, то обрезает по бедро, и один и тот же анкер даёт
    # разное лицо — замер 08.09.2026: 0.077 у целой фигуры против 0.113 у
    # обрезанной, то есть половина кадров уходила бы в брак по размеру лица.
    top = anchor.get("bottom", 0.96) - anchor.get("height", 0.55)   # где стоит макушка
    face_h = _face_height(person)
    if face_h:
        ratio = (scene.height * anchor.get("face", config.TARGET_FACE)) / face_h
    else:
        ratio = (scene.height * anchor.get("height", 0.55)) / person.height
    person = person.resize((max(1, int(person.width * ratio)), max(1, int(person.height * ratio))),
                           Image.LANCZOS)
    person = _clean_edge(person)
    person = _match_colors(person, scene)
    person = _match_texture(person, scene)

    px = int(scene.width * anchor.get("cx", 0.35) - person.width / 2)
    # Голова стоит там, где её ждёт анкер; низ уходит куда придётся — так кадр
    # выглядит одинаково независимо от того, целую фигуру нарисовала модель или
    # обрезанную. Обрезанную дополнительно уводим за нижний край, чтобы срез не
    # оказался посреди земли.
    py = int(scene.height * top)
    if legs_cropped:
        py = max(py, int(scene.height * 1.04) - person.height)

    out = scene.convert("RGBA")
    if not legs_cropped:
        # мягкая контактная тень под ногами — только если ноги действительно есть
        shadow = Image.new("RGBA", scene.size, (0, 0, 0, 0))
        sw, sh = int(person.width * 0.85), max(14, int(person.height * 0.05))
        ell = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
        from PIL import ImageDraw
        ImageDraw.Draw(ell).ellipse([0, 0, sw, sh], fill=(10, 10, 10, 110))
        ell = ell.filter(ImageFilter.GaussianBlur(6))
        shadow.paste(ell, (px + (person.width - sw) // 2, py + person.height - sh // 2), ell)
        out.alpha_composite(shadow)
    out.alpha_composite(person, (px, py))

    out = _frame_on_person(out.convert("RGB"), (px, py, person.width, person.height), fill, frame_cx)
    buf = io.BytesIO()
    out.save(buf, format="JPEG", quality=93)
    return buf.getvalue()


def generate_composite(face_png: bytes, body_png: bytes, reference_bytes: bytes,
                       outfit: str, anchor: dict, light: str = "",
                       who: str = "", scale: float = 1.0,
                       fill: float | None = None, frame_cx: float | None = None) -> bytes | None:
    """Полный цикл одного варианта: человек → вырезка → вклейка в эталон.

    light — описание света сцены из locations.json; пусто → DEFAULT_LIGHT.
    who   — кого рисуем («a boy about 10 years old»), из face_metric.describe_guest.
    scale — поправка роста: ребёнок в анкере взрослого выглядел бы со взрослого."""
    prompt = (PERSON_PROMPT.replace("{OUTFIT}", outfit)
              .replace("{LIGHT}", light or DEFAULT_LIGHT)
              .replace("{WHO}", who or "the guest"))
    if scale != 1.0:
        anchor = dict(anchor)
        anchor["height"] = anchor.get("height", 0.55) * scale
    person = replicate_client.nano_banana([face_png, body_png], prompt)
    if not person:
        return None
    cut = _remove_bg(person)
    if not cut:
        return None
    return compose(cut, reference_bytes, anchor, fill, frame_cx)

"""ArcFace-метрика сходства лиц (insightface buffalo_l).

Рекомендация старшего разработчика, п.1: объективная «линейка» сходства вместо
оценки на глаз. Локально, на CPU, ~сотни мс на кадр. Используется для:
  • гейта входного фото (размер лица, один в кадре, резкость) до генерации;
  • ранжирования сгенерированных вариантов по сходству с гостем;
  • отбраковки вариантов ниже порога.

Один и тот же детектор (SCRFD из buffalo_l) заодно точнее Haar-каскада в facecrop.py.
"""
from __future__ import annotations

import io
import threading
from typing import Optional

import numpy as np
from PIL import Image, ImageOps

import config

_app = None
_lock = threading.Lock()


def available() -> bool:
    """Пытается лениво инициализировать модель. False, если insightface/модель недоступны."""
    return _get_app() is not None


def _get_app():
    global _app
    if _app is None:
        with _lock:
            if _app is None:
                try:
                    from insightface.app import FaceAnalysis
                    # грузим только нужные модули (экономия RAM на малых VPS):
                    # detection — bbox/скор, recognition — эмбеддинг, landmark_3d_68 — поза (yaw).
                    # genderage и 2d106det не используются вовсе.
                    app = FaceAnalysis(name=config.FACE_MODEL, providers=["CPUExecutionProvider"],
                                       allowed_modules=config.FACE_MODULES)
                    app.prepare(ctx_id=-1, det_size=config.FACE_DET_SIZE)
                    _app = app
                except Exception as exc:  # noqa: BLE001 — метрика не должна ронять сервис
                    print(f"[face_metric] инициализация не удалась: {exc}")
                    _app = False  # помечаем как «пробовали и не смогли»
    return _app or None


def _to_bgr(image_bytes: bytes) -> np.ndarray:
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(image_bytes))).convert("RGB")
    return np.array(img)[:, :, ::-1]  # RGB -> BGR для insightface/cv2


def _faces(image_bytes: bytes):
    app = _get_app()
    if app is None:
        return []
    try:
        return app.get(_to_bgr(image_bytes))
    except Exception as exc:  # noqa: BLE001
        print(f"[face_metric] detect error: {exc}")
        return []


def _largest(faces):
    return max(faces, key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1])) if faces else None


def embedding(image_bytes: bytes) -> Optional[np.ndarray]:
    """512-мерный нормированный эмбеддинг крупнейшего лица (или None)."""
    f = _largest(_faces(image_bytes))
    return None if f is None else f.normed_embedding


def frame_ratio(image_bytes: bytes) -> Optional[float]:
    """Доля высоты кадра, занятая лицом (высота bbox / высота изображения).

    Детерминированная проверка кадрирования: по замерам на реальных генерациях
    поясной портрет даёт 0.11–0.18, полный рост — 0.05–0.10. Позволяет
    отбраковывать кадры, где модель проигнорировала FRAMING LOCK."""
    f = _largest(_faces(image_bytes))
    if f is None:
        return None
    try:
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(image_bytes)))
        return float(f.bbox[3] - f.bbox[1]) / float(img.height)
    except Exception:  # noqa: BLE001
        return None


def similarity(emb_a: Optional[np.ndarray], emb_b: Optional[np.ndarray]) -> float:
    """Косинусное сходство (оба эмбеддинга нормированы) в диапазоне ~[-1..1]."""
    if emb_a is None or emb_b is None:
        return 0.0
    return float(np.dot(emb_a, emb_b))


def _blur_var(image_bytes: bytes) -> float:
    """Дисперсия лапласиана по всему кадру. Оставлена только для логов.

    Как гейт не годится: величина падает не от смаза, а от разрешения съёмки.
    Замер 26.07.2026 — тот же кадр, снятый в 640×480 и растянутый до 2560×1920,
    даёт 14 против 357 у оригинала, хотя лицо на нём вполне рабочее."""
    try:
        import cv2
        arr = _to_bgr(image_bytes)
        gray = cv2.cvtColor(arr, cv2.COLOR_BGR2GRAY)
        return float(cv2.Laplacian(gray, cv2.CV_64F).var())
    except Exception:  # noqa: BLE001
        return 1e9  # не смогли посчитать — не блокируем по резкости


def _face_sharpness(image_bytes: bytes, face) -> float:
    """Резкость самого лица, приведённая к единому масштабу (кроп → 256×256).

    Именно это нас интересует: смазано ли лицо, а не сколько мегапикселей у
    вебки. Ориентиры (замер 26.07.2026): нормальный кадр 77–121, слабая вебка
    с апскейлом ~51, заметный смаз ~22, сильный ~10."""
    try:
        import cv2
        gray = cv2.cvtColor(_to_bgr(image_bytes), cv2.COLOR_BGR2GRAY)
        x1, y1, x2, y2 = (int(v) for v in face.bbox)
        x1, y1 = max(0, x1), max(0, y1)
        crop = gray[y1:y2, x1:x2]
        if crop.size == 0:
            return 1e9
        norm = cv2.resize(crop, (256, 256), interpolation=cv2.INTER_AREA)
        return float(cv2.Laplacian(norm, cv2.CV_64F).var())
    except Exception:  # noqa: BLE001
        return 1e9  # не смогли посчитать — не блокируем по резкости


def check_input(image_bytes: bytes) -> tuple[bool, str, dict]:
    """Гейт входного фото гостя. (ok, причина-для-показа, инфо-для-логов)."""
    app = _get_app()
    if app is None:
        return True, "ok", {"gate": "disabled"}  # метрика недоступна — не мешаем флоу
    faces = _faces(image_bytes)
    if not faces:
        return False, "Лицо не распознано — встаньте прямо перед камерой.", {}

    # Несколько лиц — берём САМОЕ КРУПНОЕ, а не отбиваем кадр. Овал на экране это
    # лишь подсказка поверх видео: гейт видит весь кадр целиком, и на форуме в него
    # попадают проходящие за спиной люди. 30.07.2026 из-за этого гостям приходил
    # отказ «в кадре несколько лиц». Гость всегда ближе всех к камере, поэтому его
    # лицо заведомо самое большое — его и берём.
    f = _largest(faces)
    w = int(f.bbox[2] - f.bbox[0])
    info: dict = {"face_px": w, "det_score": round(float(f.det_score), 3)}
    info["bbox"] = [round(float(v), 1) for v in f.bbox]  # нужен для кропа под свап
    if len(faces) > 1:
        info["faces"] = len(faces)   # для логов: сколько лиц было в кадре
        # рамки посторонних — чтобы кроп под свап их гарантированно исключил
        info["others"] = [[round(float(v), 1) for v in o.bbox] for o in faces if o is not f]
    if w < config.FACE_MIN_PX:
        return False, "Подойдите ближе — лицо слишком мелкое в кадре.", info
    pose = getattr(f, "pose", None)
    if pose is not None:
        yaw = abs(float(pose[1]))
        info["yaw_deg"] = round(yaw, 1)
        if yaw > config.FACE_MAX_YAW:
            return False, "Смотрите прямо в камеру — голова слишком повёрнута.", info
    sharp = _face_sharpness(image_bytes, f)
    info["face_sharp"] = round(sharp, 1)
    info["blur_var"] = round(_blur_var(image_bytes), 1)  # только для диагностики
    try:
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(image_bytes)))
        info["src"] = f"{img.width}x{img.height}"
    except Exception:  # noqa: BLE001
        pass
    if sharp < config.FACE_MIN_SHARP:
        return False, ("Лицо получилось нечётким. Добавьте света и переснимите — "
                       "если не помогает, загрузите фото с телефона по кнопке ниже."), info
    return True, "ok", info


# Возрастные группы. Границы намеренно грубые: genderage ошибается на 3-5 лет,
# и точный возраст в промпте не нужен — нужен верный «типаж».
def describe_guest(image_bytes: bytes) -> dict:
    """Кого видит киоск: пол, возраст и готовая фраза для промпта.

    Возвращает {"who": "a boy about 10 years old", "age": 10, "sex": "male",
    "scale": 0.86} либо {} — если детектор недоступен или лица нет. Пустой
    словарь означает «не знаем», и промпт остаётся прежним, без подсказки.
    """
    faces = _faces(image_bytes)
    if not faces:
        return {}
    f = _largest(faces)
    age = getattr(f, "age", None)
    sex = getattr(f, "sex", None)          # 'M' / 'F' у insightface
    if age is None:
        return {}
    age = int(age)
    male = (sex or "M").upper().startswith("M")
    if age <= 12:
        who = f"a {'boy' if male else 'girl'} about {max(age, 4)} years old"
        scale = config.CHILD_SCALE
    elif age <= 17:
        who = f"a teenage {'boy' if male else 'girl'} about {age} years old"
        scale = config.TEEN_SCALE
    elif age <= 30:
        who = f"a young {'man' if male else 'woman'} in their twenties"
        scale = 1.0
    elif age <= 50:
        who = f"a {'man' if male else 'woman'} in their {'thirties' if age < 40 else 'forties'}"
        scale = 1.0
    else:
        who = f"an older {'man' if male else 'woman'} about {age} years old"
        scale = 1.0
    return {"who": who, "age": age, "sex": "male" if male else "female", "scale": scale}


def rank_variants(guest_bytes: bytes, variants: list[bytes]) -> list[tuple[int, float]]:
    """[(индекс_варианта, сходство_с_гостем)] по убыванию сходства. Пустой список,
    если метрика недоступна (тогда вызывающий сохраняет исходный порядок)."""
    if _get_app() is None:
        return []
    ref = embedding(guest_bytes)
    if ref is None:
        return []
    scored = [(i, similarity(ref, embedding(v))) for i, v in enumerate(variants)]
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored

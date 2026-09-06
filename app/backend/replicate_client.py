"""Резервный провайдер генерации через Replicate — FLUX Kontext Pro (image-to-image).

Адаптировано из паттерна remtechnika-ai/backend/services/replicate_svc.py:
внешний вызов с таймаутом, универсальное чтение результата, graceful None при
отсутствии токена (ADR-6: Replicate — альтернатива/резерв к Gemini «Nano Banana»).
"""
from __future__ import annotations

import base64
import concurrent.futures
import time

import httpx

import config

_IMAGE_TIMEOUT = 180  # сек — не даём внешнему вызову зависнуть

# Nano Banana (Gemini image) через Replicate — multi-image (гость + эталон сцены).
# Pro — заметно лучше держит сходство лица (конфигурируется через .env).
NANO_BANANA_MODEL = config.NANO_BANANA_MODEL
# FLUX Kontext Pro — резерв, одна картинка (только гость)
FLUX_MODEL = "black-forest-labs/flux-kontext-pro"
# Точный перенос лица на готовый кадр (InsightFace-подход, как в roop/facefusion)
FACE_SWAP_MODEL = "cdingram/face-swap:d1d6ea8c8be89d664a07a457526f7128109dee7030fdac424788d762c71ed111"

# Таймауты httpx по умолчанию — 5 с на подключение. На выставочном Wi-Fi этого
# мало: 05.09.2026 запросы к Replicate падали с ConnectTimeout на TLS-рукопожатии,
# хотя сеть работала. Даём подключению 60 с, чтению — 180 с.
_HTTP_TIMEOUT = httpx.Timeout(float(_IMAGE_TIMEOUT), connect=60.0)

try:
    import replicate
    _client = (replicate.Client(api_token=config.REPLICATE_API_TOKEN, timeout=_HTTP_TIMEOUT)
               if config.REPLICATE_API_TOKEN else None)
except Exception:  # noqa: BLE001 — пакет/токен недоступны → провайдер просто выключен
    _client = None


def available() -> bool:
    return _client is not None


# ---- Состояние оплаты, как его видит сам провайдер ----
# Баланс через API Replicate недоступен (токен отдаёт 403), поэтому ловим то, что
# он сообщает в ошибках: при остатке ниже $5 включается жёсткий лимит, при нуле —
# «Insufficient credit». 31.07.2026 на форуме это заметили только по остановке
# киоска — теперь состояние видно на странице очереди печати.
BILLING: dict = {
    "started_at": time.time(),
    "low_credit_at": 0.0,   # когда последний раз сработал лимит «меньше $5»
    "no_credit_at": 0.0,    # когда последний раз отказ «деньги кончились»
    "images": 0,            # успешно сгенерированных кадров с момента запуска
    "swaps": 0,             # успешных переносов лица
}

# Ориентиры для оценки расхода: кадр nano-banana ~$0.134, свап ~$0.0075,
# улучшение лица ~$0.0095. Точные цифры смотреть в личном кабинете Replicate.
PRICE_IMAGE = 0.134
PRICE_SWAP = 0.0075


def _note_billing(msg: str) -> None:
    """Отмечает в состоянии, что провайдер пожаловался на деньги."""
    m = msg.lower()
    if "insufficient credit" in m:
        BILLING["no_credit_at"] = time.time()
    elif "less than $5" in m or ("throttled" in m and "credit" in m):
        BILLING["low_credit_at"] = time.time()


def billing_state() -> dict:
    """Сводка для страницы очереди печати."""
    now = time.time()
    b = dict(BILLING)
    b["spent"] = b["images"] * PRICE_IMAGE + b["swaps"] * PRICE_SWAP
    b["low_ago"] = (now - b["low_credit_at"]) if b["low_credit_at"] else None
    b["no_ago"] = (now - b["no_credit_at"]) if b["no_credit_at"] else None
    if b["no_ago"] is not None and b["no_ago"] < 900:
        b["status"] = "empty"      # деньги кончились
    elif b["low_ago"] is not None and b["low_ago"] < 900:
        b["status"] = "low"        # баланс ниже $5, включён лимит
    else:
        b["status"] = "ok"
    return b


def _read_output(output) -> bytes | None:
    """Универсальное чтение результата Replicate (file-like / url / список).

    Список распаковываем ЯВНО по типу. Прежняя проверка `hasattr(output,
    "__getitem__")` ломалась на строках: у строки индексация тоже есть, поэтому
    когда модель отдавала ссылку простой строкой, `output[0]` брал первый символ
    и URL превращался в "h". Скачивание падало с UnsupportedProtocol, свап
    отменялся, и гость получал кадр с лицом, нарисованным моделью — мягким и
    непохожим (сходство ~0.6 вместо 0.75+). Именно так и было 27.07.2026."""
    import time

    if isinstance(output, (list, tuple)):
        if not output:
            print("[replicate] read error: модель вернула пустой список")
            return None
        output = output[0]

    # FileOutput и подобные объекты умеют read() — самый прямой путь.
    if hasattr(output, "read"):
        try:
            return output.read()
        except Exception as exc:  # noqa: BLE001
            print(f"[replicate] read error (read): {type(exc).__name__}: {exc}")
            return None

    url = str(getattr(output, "url", output)).strip()
    if not url.startswith(("http://", "https://")):
        print(f"[replicate] read error: не похоже на ссылку "
              f"(тип {type(output).__name__}, значение {url[:60]!r})")
        return None

    for attempt in (1, 2, 3):
        try:
            r = httpx.get(url, timeout=120)
            r.raise_for_status()
            return r.content
        except Exception as exc:  # noqa: BLE001
            print(f"[replicate] read error (скачивание, попытка {attempt}/3): "
                  f"{type(exc).__name__}: {exc}")
            if attempt < 3:
                time.sleep(3 * attempt)
    return None


def _shrink(image_bytes: bytes, max_side: int = 1600, quality: int = 88) -> bytes:
    """Сжимает картинку до JPEG ≤max_side px: полезная нагрузка меньше в разы,
    запросы быстрее и не рвутся (connection reset на ~5 МБ base64)."""
    import io
    from PIL import Image, ImageOps
    try:
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(image_bytes))).convert("RGB")
        if max(img.size) > max_side:
            img.thumbnail((max_side, max_side), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        return buf.getvalue()
    except Exception:  # noqa: BLE001 — не смогли сжать, шлём как есть
        return image_bytes


def _data_uri(image_bytes: bytes, mime: str = "image/jpeg") -> str:
    return f"data:{mime};base64,{base64.b64encode(_shrink(image_bytes)).decode()}"


def _apply_model_args(inp: dict, model: str) -> dict:
    """Дополняет вход под семейство модели:
      - Seedream (bytedance/seedream-*): параметр `size`, без `output_format`;
      - Nano Banana (google/nano-banana*): `output_format` + `resolution`."""
    if "seedream" in model:
        inp["size"] = config.NANO_BANANA_RESOLUTION  # 2K
    else:
        inp["output_format"] = "jpg"
        if "nano-banana" in model:
            inp["resolution"] = config.NANO_BANANA_RESOLUTION
    return inp


def _build_gen_input(images: list[bytes], prompt: str, model: str) -> dict:
    """Собирает вход под семейство модели (разные имена параметров у провайдеров)."""
    if "gpt-image" in model:
        # OpenAI GPT Image: input_images (не image_input), портрет 2:3, свой ключ не нужен.
        # quality=high очень медленный (>6 мин на кадр) — для киоска по умолчанию medium.
        return {
            "prompt": prompt,
            "input_images": [_data_uri(b) for b in images],
            "aspect_ratio": "2:3",
            "quality": config.GPT_IMAGE_QUALITY,
            "output_format": "jpeg",
            "moderation": "low",
        }
    return _apply_model_args({
        "prompt": prompt,
        "image_input": [_data_uri(b) for b in images],
        "aspect_ratio": "3:4",
    }, model)


def _nano_banana_sync(images: list[bytes], prompt: str, model: str) -> bytes | None:
    """Генерация (multi-image): гость + эталон сцены → фотореалистичная вставка
    с сохранением лица и телосложения, узнаваемым фоном, нейтральной одеждой."""
    if not _client:
        return None
    output = _client.run(model, input=_build_gen_input(images, prompt, model))
    return _read_output(output)


def _is_unavailable(msg: str) -> bool:
    """Временная недоступность модели у провайдера (Google E004 и родственные).
    Лечится ожиданием — в отличие от ошибок входа, которые повторять бессмысленно."""
    m = msg.lower()
    return ("temporarily unavailable" in m or "e004" in m
            or "modelerror" in m or "503" in m or "502" in m)


def _try_model(model: str, images: list[bytes], prompt: str, retries: int) -> bytes | None:
    """Повторы на одной модели. Паузы зависят от вида сбоя:
      429 (лимит) и E004 (сервис недоступен) — ждём долго и с нарастанием,
      сетевые обрывы — короткий повтор."""
    for attempt in range(retries):
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            fut = pool.submit(_nano_banana_sync, images, prompt, model)
            try:
                out = fut.result(timeout=_IMAGE_TIMEOUT)
                if out:
                    BILLING["images"] += 1
                return out
            except Exception as exc:  # noqa: BLE001
                msg = str(exc)
                _note_billing(msg)
                if attempt < retries - 1:
                    if "429" in msg:
                        wait = 25 * (attempt + 1)
                    elif _is_unavailable(msg):
                        wait = 12 * (attempt + 1)   # провайдер «моргает» — даём отлежаться
                    else:
                        wait = 5
                    print(f"[replicate] {model}: сбой ({msg[:70]}…), жду {wait}с "
                          f"и повторяю ({attempt + 1}/{retries})")
                    time.sleep(wait)
                    continue
                print(f"[replicate] {model} не отдал результат: {msg[:160]}")
                return None
    return None


def nano_banana(images: list[bytes], prompt: str, retries: int = 4) -> bytes | None:
    """Основной путь генерации с запасной моделью.

    У google/nano-banana-pro бывают всплески ошибок «Service is temporarily
    unavailable (E004)» — это сбой на стороне провайдера. Чтобы гость не получал
    отказ, после исчерпания повторов пробуем резервную модель (по статистике
    отказов у seedream-4.5 заметно меньше). None — только если не сработала ни одна."""
    if not _client or not images:
        return None
    out = _try_model(NANO_BANANA_MODEL, images, prompt, retries)
    if out:
        return out
    fallback = config.FALLBACK_MODEL
    if fallback and fallback != NANO_BANANA_MODEL:
        print(f"[replicate] основная модель недоступна → резервная {fallback}")
        return _try_model(fallback, images, prompt, 2)
    return None


# Второй проход nano-banana = диффузионный face-swap в ПОЛНОМ разрешении (рек. №2):
# без 128px-бутылочного горлышка inswapper → без «восковости», выше детализация.
NANO_SWAP_PROMPT = (
    "Image 1 is a photo of a person in a scene. Image 2 is a close-up of the SAME person's real face. "
    "Replace ONLY the face in image 1 with the exact face from image 2: same identity, same facial "
    "features, same eyes, nose, mouth, face shape and skin tone. Keep EVERYTHING else in image 1 "
    "exactly as is — pose, body, hair, clothing, background, framing and lighting must not change. "
    "Match the face lighting and skin tone to image 1. Photorealistic, seamless, natural skin texture, "
    "sharp facial detail. Do not beautify or alter the identity."
)


def _nano_face_swap_sync(frame: bytes, face_png: bytes) -> bytes | None:
    if not _client:
        return None
    inp = _apply_model_args({
        "prompt": NANO_SWAP_PROMPT,
        "image_input": [_data_uri(frame), _data_uri(face_png)],
        "aspect_ratio": "3:4",
    }, NANO_BANANA_MODEL)
    output = _client.run(NANO_BANANA_MODEL, input=inp)
    return _read_output(output)


def nano_face_swap(frame: bytes, face_png: bytes, retries: int = 2) -> bytes | None:
    """Диффузионный face-swap вторым проходом nano-banana (рек. №2): переносит
    реальное лицо гостя на выбранный кадр в полном разрешении. None при неудаче —
    вызывающий тогда отдаёт исходный кадр (паттерн `swapped or out`)."""
    if not _client or not frame or not face_png:
        return None
    import time
    for attempt in range(retries):
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            fut = pool.submit(_nano_face_swap_sync, frame, face_png)
            try:
                return fut.result(timeout=_IMAGE_TIMEOUT)
            except Exception as exc:  # noqa: BLE001
                if attempt < retries - 1:
                    wait = 25 if "429" in str(exc) else 5
                    print(f"[replicate] nano-swap сбой ({str(exc)[:50]}…), жду {wait}с")
                    time.sleep(wait); continue
                print(f"[replicate] nano-swap failed/timeout: {exc}")
                return None
    return None


# GFPGAN — реставрация/лёгкая бьютификация лица (для входа с вебкамеры: шум, блюр, низкое разрешение)
GFPGAN_MODEL = "tencentarc/gfpgan:0fbacf7afc6c144e5be9767cff80f25aff23e52b0708f17e20f9879b2f21516c"


def _enhance_face_sync(image_bytes: bytes) -> bytes | None:
    if not _client:
        return None
    output = _client.run(GFPGAN_MODEL, input={"img": _data_uri(image_bytes), "scale": 2, "version": "v1.4"})
    return _read_output(output)


def enhance_face(image_bytes: bytes, retries: int = 2) -> bytes | None:
    """GFPGAN: чистит и слегка улучшает лицо с вебкам-кадра (шум/блюр/низкое разрешение).
    None при ошибке — вызывающий тогда использует исходное фото."""
    if not _client or not image_bytes:
        return None
    import time
    for attempt in range(retries):
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            fut = pool.submit(_enhance_face_sync, image_bytes)
            try:
                return fut.result(timeout=120)
            except Exception as exc:  # noqa: BLE001
                if attempt < retries - 1:
                    time.sleep(5); continue
                print(f"[replicate] gfpgan failed/timeout: {exc}")
                return None
    return None


def sharpen_result(image_bytes: bytes, percent: int = 70) -> bytes | None:
    """Локальная резкость (unsharp mask) после свапа: чётче контуры лица/губ.
    Лицо НЕ перерисовывается → идентичность сохраняется 1:1 и нет «двоения»,
    которое давал блендинг с GFPGAN."""
    try:
        import io
        from PIL import Image, ImageFilter
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        img = img.filter(ImageFilter.UnsharpMask(radius=2, percent=max(0, percent), threshold=3))
        buf = io.BytesIO(); img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception as exc:  # noqa: BLE001
        print(f"[sharpen] failed: {exc}")
        return None


def refine_swap(image_bytes: bytes, alpha: float = 0.3) -> bytes | None:
    """Доработка после свапа: GFPGAN чистит/красивит лицо, но «перерисовывает» →
    блендим его лишь на alpha (30%) с оригиналом свапа — чистим кожу, сохраняя
    сходство (ArcFace: чистый свап 0.835 → бленд 30% 0.808 при заметно красивее)."""
    gf = enhance_face(image_bytes)
    if not gf:
        return None
    try:
        import io
        from PIL import Image
        base = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        enh = Image.open(io.BytesIO(gf)).convert("RGB").resize(base.size, Image.LANCZOS)
        out = Image.blend(base, enh, max(0.0, min(1.0, alpha)))
        buf = io.BytesIO(); out.save(buf, format="PNG")
        return buf.getvalue()
    except Exception as exc:  # noqa: BLE001
        print(f"[refine] blend failed: {exc}")
        return None


# Резервная модель свапа. 27.07.2026 cdingram/face-swap начал завершаться со
# статусом succeeded, но с output=None — каждый запуск подряд, при исправно
# работающих nano-banana и GFPGAN рядом. Это сбой на стороне модели.
# codeplugtech/face-swap — тот же inswapper с теми же входами
# (input_image, swap_image), 2.5 млн запусков.
# Версия закреплена обязательно: run() по одному имени работает только для
# официальных моделей, для комьюнити-моделей Replicate отвечает 404 — ровно так
# резервная и не сработала в ночь на 28.07 (в логах face-swap failed ... 404).
FACE_SWAP_FALLBACK = ("codeplugtech/face-swap:"
                      "278a81e7ebb22db98bcba54de985d22cc1abeead2754eb1f2af717247be69b34")


def _face_swap_sync(target: bytes, face: bytes, model: str) -> bytes | None:
    if not _client:
        return None
    output = _client.run(model, input={
        "input_image": _data_uri(target),
        "swap_image": _data_uri(face),
    })
    if output is None:
        print(f"[replicate] {model.split(':')[0]}: succeeded, но output пуст")
        return None
    return _read_output(output)


def face_swap(target: bytes, face: bytes) -> bytes | None:
    """Финальный шаг: переносит НАСТОЯЩЕЕ лицо гостя на сгенерированный кадр.
    Nano-banana ставит сцену/тело/одежду, swap гарантирует сходство 1:1.
    None при ошибке — тогда отдаём кадр без свапа (лучше, чем ничего)."""
    if not _client:
        return None
    for model in (FACE_SWAP_MODEL, FACE_SWAP_FALLBACK):
        for attempt in range(2):
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                fut = pool.submit(_face_swap_sync, target, face, model)
                try:
                    out = fut.result(timeout=120)
                except Exception as exc:  # noqa: BLE001
                    _note_billing(str(exc))
                    if "429" in str(exc) and attempt == 0:
                        print("[replicate] face-swap 429, жду 25с")
                        time.sleep(25)
                        continue
                    print(f"[replicate] face-swap failed ({model.split(':')[0]}): {exc}")
                    out = None
                if out:
                    BILLING["swaps"] += 1
                    return out
                break   # пустой результат повторять на той же модели бессмысленно
        if model == FACE_SWAP_MODEL:
            print(f"[replicate] свап через {model.split(':')[0]} без результата → резервная")
    return None


def _edit_sync(image_bytes: bytes, prompt: str) -> bytes | None:
    if not _client:
        return None
    output = _client.run(
        FLUX_MODEL,
        input={
            "prompt": prompt,
            "input_image": _data_uri(image_bytes),
            "output_format": "jpg",
            "output_quality": 92,
            "safety_tolerance": 2,
        },
    )
    return _read_output(output)


def edit_image(image_bytes: bytes, prompt: str) -> bytes | None:
    """image-to-image: вставить гостя (image_bytes) в сцену по инструкции prompt.
    Возвращает None при недоступности/ошибке/таймауте — не роняет пайплайн."""
    if not _client:
        return None
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        fut = pool.submit(_edit_sync, image_bytes, prompt)
        try:
            return fut.result(timeout=_IMAGE_TIMEOUT)
        except Exception as exc:  # noqa: BLE001
            print(f"[replicate] edit failed/timeout: {exc}")
            return None

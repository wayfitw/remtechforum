"""Шлюз генерации изображений.

Основной провайдер — Google Gemini «Nano Banana» (image-to-image).
Если ключ не задан (STUB_MODE) — возвращает заглушки из фото гостя, чтобы
сквозной флоу можно было протестировать без обращения к платному API.
"""
from __future__ import annotations

import io
import concurrent.futures
from typing import List, Optional

from PIL import Image, ImageOps, ImageDraw, ImageFont, ImageEnhance, ImageFilter

import config
import replicate_client


class GenerationError(Exception):
    pass


# ---------- Реальная генерация через Gemini ----------

def _gemini_once(prompt: str, guest_png: bytes, reference: Optional[bytes]) -> bytes:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=config.GEMINI_API_KEY)
    contents = [prompt, types.Part.from_bytes(data=guest_png, mime_type="image/png")]
    if reference:
        contents.append(types.Part.from_bytes(data=reference, mime_type="image/png"))

    resp = client.models.generate_content(model=config.GEMINI_IMAGE_MODEL, contents=contents)
    for cand in (resp.candidates or []):
        for part in (cand.content.parts or []):
            inline = getattr(part, "inline_data", None)
            if inline and inline.data:
                return inline.data
    raise GenerationError("Модель не вернула изображение (возможно, сработал фильтр безопасности).")


# ---------- Stub-режим (без ключа) ----------

def _stub_variant(guest_png: bytes, idx: int) -> bytes:
    """Демо-заглушка: стилизует фото гостя, чтобы показать работу флоу."""
    img = Image.open(io.BytesIO(guest_png)).convert("RGB")
    img = ImageOps.exif_transpose(img)
    # вертикальный кадр 3:4
    img = ImageOps.fit(img, (900, 1200), Image.LANCZOS)
    # лёгкие вариации, чтобы 3 «варианта» отличались
    tints = [(1.05, 1.0), (0.95, 1.1), (1.1, 0.95)]
    b, c = tints[idx % len(tints)]
    img = ImageEnhance.Brightness(img).enhance(b)
    img = ImageEnhance.Color(img).enhance(c)
    # лёгкая дымка сверху
    overlay = Image.new("RGB", img.size, (120, 150, 165))
    img = Image.blend(img, overlay, 0.12)
    draw = ImageDraw.Draw(img)
    label = f"DEMO / STUB · вариант {idx + 1}"
    draw.rectangle([0, img.height - 46, img.width, img.height], fill=(11, 85, 99))
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 22)
    except Exception:
        font = ImageFont.load_default()
    draw.text((16, img.height - 38), label, fill=(255, 255, 255), font=font)
    draw.text((16, 14), "Маяк Анива (демо без API-ключа)", fill=(255, 255, 255), font=font)
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


# ---------- Публичный интерфейс ----------

def _one_variant(prompt: str, guest_png: bytes, reference: Optional[bytes],
                 body_png: Optional[bytes] = None, swap_face: Optional[bytes] = None,
                 brand_logo: Optional[bytes] = None) -> bytes:
    """Один вариант по цепочке провайдеров (ADR-6):
      1) Nano Banana via Replicate (multi-image: гость + эталон сцены) — основной;
      2) Gemini напрямую (если задан GEMINI_API_KEY);
      3) FLUX Kontext (только гость) — последний резерв.
    Не глотаем первопричину: если всё упало, пробрасываем ошибку основного пути."""
    primary_err: Optional[Exception] = None

    # 1) Nano Banana через Replicate — порядок: лицо, корпус, эталон сцены
    if replicate_client.available():
        images = [guest_png]
        if body_png:
            images.append(body_png)
        if reference:
            images.append(reference)
        if brand_logo:
            images.append(brand_logo)   # image 4 — фирменный знак для мерча
        out = replicate_client.nano_banana(images, prompt)
        if out:
            # face-swap: переносим лицо. swap_face — СЫРОЙ кроп (без GFPGAN),
            # чтобы свап опирался на истинную идентичность → выше сходство.
            if config.FACE_SWAP_ENABLED:
                swapped = replicate_client.face_swap(out, swap_face or guest_png)
                if not swapped:
                    # Кадр без переноса лица — сходство будет низким. Пишем громко:
                    # 26.07.2026 такие кадры уходили гостю молча, и по логам было
                    # не понять, почему «лицо не то».
                    print("[swap] НЕ применён — кадр уходит без переноса лица")
                    return out
                print("[swap] применён")
                # доработка: лёгкий GFPGAN-блендинг — красивее кожа, но мылит рот
                # (двоение двух по-разному нарисованных лиц), поэтому по умолчанию выкл
                if config.SWAP_REFINE_ENABLED:
                    refined = replicate_client.refine_swap(swapped, config.SWAP_REFINE_ALPHA)
                    swapped = refined or swapped
                # резкость: чётче контуры губ/лица, лицо не перерисовывается
                if config.SWAP_SHARPEN_ENABLED:
                    sharp = replicate_client.sharpen_result(swapped, config.SWAP_SHARPEN_PERCENT)
                    swapped = sharp or swapped
                return swapped
            return out
        primary_err = GenerationError("Nano Banana (Replicate) не вернул изображение.")

    # 2) Gemini напрямую (если есть ключ)
    if config.GEMINI_API_KEY:
        try:
            gen = _gemini_once(prompt, guest_png, reference)
            swapped = replicate_client.face_swap(gen, swap_face or guest_png) if replicate_client.available() else None
            return swapped or gen
        except Exception as exc:  # noqa: BLE001
            primary_err = primary_err or exc
            print(f"[gen] Gemini не сработал: {exc}")

    # FLUX-резерв из цепочки убран: он не видит эталон и рисует «не тот» маяк —
    # это хуже честной ошибки. Лучше ретраи основного пути (внутри nano_banana).
    raise primary_err or GenerationError("Ни один провайдер генерации недоступен.")


def generate_variants(prompts: List[str], guest_png: bytes, reference: Optional[bytes],
                      body_png: Optional[bytes] = None, swap_face: Optional[bytes] = None,
                      brand_logo: Optional[bytes] = None) -> List[bytes]:
    """Генерит по одному кадру на каждый промпт из списка (разные наряды/сиды).
    Ошибочные варианты пропускаются. swap_face — сырой кроп лица для face-swap."""
    if config.STUB_MODE:
        return [_stub_variant(guest_png, i) for i in range(len(prompts))]

    # ПОСЛЕДОВАТЕЛЬНО, не параллельно: на аккаунте лимит запросов/мин, параллель
    # душит сама себя (сгенерился «левый маяк» именно из-за этого)
    results: List[bytes] = []
    for p in prompts:
        try:
            results.append(_one_variant(p, guest_png, reference, body_png, swap_face, brand_logo))
        except Exception as exc:  # noqa: BLE001 — единичный сбой не должен рушить запрос
            print(f"[gen] вариант не удался: {exc}")

    if not results:
        raise GenerationError("Ни один вариант не сгенерировался. Проверьте ключ/модель/лимиты.")
    return results

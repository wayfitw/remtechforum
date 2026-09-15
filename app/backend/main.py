"""AI-фотоинсталляция «Ремтехника» — бэкенд киоска.

Сквозной флоу: фото гостя → генерация 2–3 вариантов (Gemini Nano Banana) →
выбор → композитинг карточки с логотипами → печать (CUPS/lpr) + QR.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
import time

# Windows: консоль по умолчанию cp1252, а логи/print содержат кириллицу —
# без этого обработчик ошибки сам падает с UnicodeEncodeError (500 вместо чистого 502).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001 — не критично, если поток не поддерживает
        pass
import uuid
from pathlib import Path
from typing import Optional

import qrcode
from fastapi import FastAPI, Request, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

import config
import gemini_client
import compositor
import facecrop
import face_metric
import replicate_client
import email_client

app = FastAPI(title="Ремтехника — AI-фотоинсталляция")

# Сессии загрузки фото с телефона гостя (in-memory, сбрасываются при рестарте)
_upload_sessions: dict = {}  # session_id → {status, path, created_at}

# Фоновые задачи генерации (in-memory). Клиент забирает результат опросом,
# поэтому запрос не висит открытым все ~2 минуты работы моделей.
_jobs: dict = {}  # job_id → {status, result, detail, created_at}
JOB_TTL_SEC = 30 * 60  # столько храним исход после завершения


def _output_file(card_id: str):
    """Путь к файлу карточки внутри assets/output — с защитой от выхода за папку.

    Сайт публичный, а card_id приходит из запроса: без проверки строка вида
    '../../.env' указала бы на любой файл на сервере (и уехала бы гостю письмом
    или в печать). Разрешаем только простое имя файла из самой папки output."""
    name = os.path.basename(str(card_id))
    if not name or name in (".", ".."):
        raise HTTPException(404, "Карточка не найдена")
    path = (config.OUTPUT / name).resolve()
    if path.parent != config.OUTPUT.resolve() or not path.is_file():
        raise HTTPException(404, "Карточка не найдена")
    return path


@app.on_event("startup")
def _warm_face_metric():
    # прогрев ArcFace-модели, чтобы первый гость не ждал загрузку onnx
    if config.FACE_GATE_ENABLED or config.FACE_RANK_ENABLED:
        face_metric.available()


@app.on_event("startup")
def _start_output_cleanup():
    """Автоочистка assets/output по DIGITAL_TTL_HOURS (на киоске диск не резиновый:
    каждый гость оставляет ~5 МБ — кадры, карточка, QR и отладочные dbg_*)."""

    def _cleanup_loop():
        ttl = config.DIGITAL_TTL_HOURS * 3600
        # Отладочные dbg_* (исходник + два кропа, ~4 МБ на гостя) живут отдельно и
        # заметно меньше: 28.07.2026 они занимали 1.5 ГБ из 1.8 ГБ всей папки.
        # Карточки гостю нужны все 72ч — по QR он забирает их позже, а отладка
        # нужна только чтобы разобрать свежую жалобу.
        dbg_ttl = config.DEBUG_TTL_HOURS * 3600
        while True:
            try:
                now = time.time()
                # заодно выкидываем отработавшие задачи, иначе реестр растёт вечно
                for jid in [k for k, v in _jobs.items()
                            if v["status"] != "running" and now - v["created_at"] > JOB_TTL_SEC]:
                    _jobs.pop(jid, None)
                removed = dbg_removed = 0
                for p in config.OUTPUT.iterdir():
                    if p.name == ".gitkeep" or not p.is_file():
                        continue
                    age = now - p.stat().st_mtime
                    if p.name.startswith(("dbg_", "rej_")):
                        if age > dbg_ttl:
                            p.unlink(missing_ok=True)
                            dbg_removed += 1
                    elif age > ttl:
                        p.unlink(missing_ok=True)
                        removed += 1
                if removed or dbg_removed:
                    print(f"[cleanup] удалено: карточек и кадров старше "
                          f"{config.DIGITAL_TTL_HOURS}ч — {removed}, "
                          f"отладочных старше {config.DEBUG_TTL_HOURS}ч — {dbg_removed}")
            except Exception as exc:  # noqa: BLE001 — уборка не должна ронять сервис
                print(f"[cleanup] ошибка: {exc}")
            time.sleep(3600)

    threading.Thread(target=_cleanup_loop, daemon=True).start()

LOCATIONS = json.loads((config.BASE_DIR / "locations.json").read_text(encoding="utf-8"))
FRONTEND = config.BASE_DIR.parent / "frontend"


def _save(data: bytes, name: str) -> str:
    (config.OUTPUT / name).write_bytes(data)
    return name


# Превью для экрана. Кадр с модели весит около 3 МБ, карточка 2.4 МБ: на Wi-Fi
# выставки они грузились по несколько секунд и прорисовывались построчно
# (замер 14.09.2026). Экрану хватает JPEG до 1100 px, это в разы легче.
# Полноразмерные PNG остаются для печати, почты и цифровой версии по QR.
# Префикс view_ — чтобы превью не попадали под маски card_* и gen_*.
PREVIEW_MAX_SIDE = 1100


def _save_preview(data: bytes, name: str) -> str:
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(data)).convert("RGB")
        img.thumbnail((PREVIEW_MAX_SIDE, PREVIEW_MAX_SIDE), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=84, optimize=True, progressive=True)
        return _save(buf.getvalue(), "view_" + name.rsplit(".", 1)[0] + ".jpg")
    except Exception as exc:  # noqa: BLE001 — без превью экран покажет полный файл
        print(f"[preview] не удалось для {name}: {exc!r}")
        return name


# ---------------- API ----------------

@app.get("/api/health")
def health():
    # Показываем ту модель, которая реально генерит (Replicate), а не Gemini:
    # на проверке перед тестом расхождение сбивало с толку.
    return {"ok": True, "stub_mode": config.STUB_MODE,
            "model": config.NANO_BANANA_MODEL if replicate_client.available() else config.GEMINI_IMAGE_MODEL,
            "provider": "replicate" if replicate_client.available() else "gemini",
            "mode": config.GEN_MODE, "variants": config.VARIANTS,
            "face_min_px": config.FACE_MIN_PX, "print_enabled": config.PRINT_ENABLED,
            "public_base_url": config.PUBLIC_BASE_URL}


@app.get("/api/locations")
def locations():
    return [
        {"id": v["id"], "title": v["title"], "subtitle": v["subtitle"],
         "enabled": v["enabled"], "outfit": v.get("outfit", "workwear")}
        for v in LOCATIONS.values()
    ]


@app.get("/api/logos")
def logos():
    # показываем только реальные логотипы партнёров (не заглушки и не служебные)
    skip = {"01_partner.png", "02_partner.png"}
    files = sorted(
        p.name for p in config.LOGOS.glob("*.png")
        if not p.name.startswith("_") and p.name not in skip
    )
    return [{"url": f"/logos/{f}"} for f in files]


@app.post("/api/check-photo")
async def check_photo(photo: UploadFile = File(...)):
    """Живая проверка кадра до генерации (для экрана съёмки): ok + причина.
    Детекция лица — CPU-bound, поэтому в отдельном потоке."""
    data = await photo.read()
    ok, reason, info = await run_in_threadpool(face_metric.check_input, data)
    return {"ok": ok, "reason": reason, "info": info}


@app.post("/api/generate")
async def generate(location: str = Form(...), photo: UploadFile = File(...),
                   outfit: str = Form("male")):
    """Запускает генерацию фоном и сразу отдаёт job_id — клиент опрашивает статус.

    Раньше ответ ждали в этом же запросе, и он висел ~2 минуты. Сеть гостя такое
    переживает не всегда: 26.07.2026 у тестировщика соединение рвалось ровно на
    60-й секунде (nginx писал 499, генерация на сервере доходила до конца, но
    отдавать результат было уже некому — браузер показывал «Failed to fetch»).
    Короткие опросы в таймаут не упираются, и заодно перезагрузка страницы больше
    не теряет результат."""
    loc = LOCATIONS.get(location)
    if not loc or not loc["enabled"]:
        raise HTTPException(400, "Локация недоступна")

    guest_bytes = await photo.read()
    job_id = uuid.uuid4().hex[:12]
    _jobs[job_id] = {"status": "running", "result": None, "detail": None,
                     "created_at": time.time()}
    threading.Thread(target=_run_job, args=(job_id, loc, guest_bytes, outfit),
                     daemon=True).start()
    return {"job_id": job_id}


def _run_job(job_id: str, loc: dict, guest_bytes: bytes, outfit: str):
    """Выполняет генерацию и складывает исход в реестр задач."""
    job = _jobs.get(job_id)
    try:
        result = _generate_sync(loc, guest_bytes, outfit)
        if job is not None:
            job["result"] = result
            job["status"] = "done"
    except HTTPException as exc:
        if job is not None:
            job["detail"] = str(exc.detail)
            job["status"] = "error"
    except Exception as exc:  # noqa: BLE001
        print(f"[job {job_id}] неожиданная ошибка: {exc!r}")
        if job is not None:
            job["detail"] = "Не удалось сгенерировать. Попробуйте ещё раз."
            job["status"] = "error"


@app.get("/api/generate-status/{job_id}")
def generate_status(job_id: str):
    """Опрос результата. Готовый ответ отдаётся в том же виде, что и раньше."""
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "Задача не найдена или истекла")
    if job["status"] == "done":
        return {"status": "done", **job["result"]}
    if job["status"] == "error":
        return {"status": "error", "detail": job["detail"]}
    return {"status": "running", "elapsed": round(time.time() - job["created_at"], 1)}


def _generate_sync(loc: dict, guest_bytes: bytes, outfit: str):
    info: dict = {}
    # гейт входного фото: плохой кадр → просьба переснять, а не плохая карточка
    if config.FACE_GATE_ENABLED:
        ok, reason, info = face_metric.check_input(guest_bytes)
        print(f"[gate] ok={ok} {info}")
        if not ok:
            # сохраняем отклонённый кадр: иначе разбор жалобы «камеру держу ровно»
            # упирается в догадки — по логам не видно, что именно увидел гейт.
            # Удалится автоочисткой вместе с остальными по DIGITAL_TTL_HOURS.
            _save(guest_bytes, f"rej_{uuid.uuid4().hex[:6]}.jpg")
            raise HTTPException(422, reason)

    # Тени с лица с вебки выравниваем ДО кропов и генерации — но только когда они
    # есть. Раньше CLAHE шёл по каждому кадру: усиливал местный контраст и шум,
    # фон становился зернистым, лицо «пережаренным», и это лицо уходило в свап
    # (замечание 16.09.2026: «блюр и засветка на фото с камеры»).
    if config.FACE_DESHADOW_ENABLED:
        dz = facecrop.deshadow(guest_bytes, config.FACE_DESHADOW_CLIP, (info or {}).get("bbox"))
        if dz:
            guest_bytes = dz
            print("[deshadow] свет на лице выровнен")

    # Для face-swap — СЫРОЙ кадр (до GFPGAN: он «причёсывает» лицо и снижает
    # сходство), обрезанный щедрой рамкой вокруг лица гостя. Тесный кроп не
    # годится — 27.07.2026 свап отвечал «No face found»; полный кадр тоже опасен —
    # на форуме в него попадают прохожие, и свап может взять чужое лицо.
    face_raw = guest_bytes
    bbox = (info or {}).get("bbox")
    if bbox:
        cropped = facecrop.crop_for_swap(guest_bytes, bbox, (info or {}).get("others"))
        if cropped:
            face_raw = cropped
            n = (info or {}).get("faces", 1)
            if n > 1:
                print(f"[swap] кроп вокруг лица гостя — в кадре было {n} лиц(а)")

    # GFPGAN — только для СЛАБОГО входа (шумная вебка, мелкое лицо).
    #
    # Он не «подчищает», а заново синтезирует лицо. На кадре с вебки это выигрыш,
    # но на резком фото с телефона даёт свой фирменный вид: гладкая пластиковая
    # кожа и раздутые черты. А его результат уходит в nano-banana как эталон
    # внешности — отсюда жалобы «мультяшное и опухшее лицо» (28.07.2026).
    # Замер того же дня: у всех кадров лицо 630–780 px и резкость 212–649,
    # то есть улучшать было нечего, а GFPGAN отрабатывал на каждом.
    if config.FACE_ENHANCE_ENABLED:
        face_px = (info or {}).get("face_px", 0) if config.FACE_GATE_ENABLED else 0
        sharp = (info or {}).get("face_sharp", 0) if config.FACE_GATE_ENABLED else 0
        нужен = (face_px < config.FACE_ENHANCE_MAX_PX
                 or sharp < config.FACE_ENHANCE_MAX_SHARP)
        if нужен:
            enhanced = replicate_client.enhance_face(guest_bytes)
            if enhanced:
                # GFPGAN отдаёт кадр вдвое крупнее и перерисовывает ФОН апскейлером:
                # фон «мылился», надписи и контуры плыли. Берём у него только лицо
                # и вклеиваем в кадр исходного размера.
                guest_bytes = facecrop.paste_face(guest_bytes, enhanced, (info or {}).get("bbox")) or enhanced
                print(f"[enhance] GFPGAN применён (лицо {face_px}px, резкость {sharp})")
        else:
            print(f"[enhance] пропущен — фото и так хорошее "
                  f"(лицо {face_px}px, резкость {sharp})")

    # два кадра гостя: крупное лицо (для точных черт) + корпус (для телосложения)
    face_png, body_png = facecrop.crops(guest_bytes)

    # отладка: сохраняем, что реально уходит в модель (смотреть при проблемах качества)
    debug_id = uuid.uuid4().hex[:6]
    _save(guest_bytes, f"dbg_{debug_id}_raw.jpg")
    _save(face_png, f"dbg_{debug_id}_face.png")
    _save(body_png, f"dbg_{debug_id}_body.png")

    from PIL import Image, ImageOps
    ref_path = config.REFERENCES / loc["reference"]
    reference = None
    if ref_path.exists():
        rimg = ImageOps.exif_transpose(Image.open(ref_path)).convert("RGB")
        rbuf = io.BytesIO(); rimg.save(rbuf, format="PNG"); reference = rbuf.getvalue()

    # промпты с разными нарядами по выбранному образу (девушкам — розовый/белый)
    # Образ задаёт ПЛОЩАДКА, а не пол гостя: на Столбах джинсы и худи,
    # на рабочих площадках спецовка. Выбор пола на наряд не влияет (см. OUTFITS).
    outfit_set = loc.get("outfit", "workwear")
    outfits = [config.OUTFIT_SETS.get(outfit_set, config.OUTFIT_SETS["workwear"])]

    # Кого рисуем — говорит сам гость на экране выбора. Автоопределение по лицу
    # проверено и забраковано: genderage ошибается на десятилетия (см. config).
    kind = config.GUEST_KINDS.get(outfit, config.GUEST_KINDS["male"])
    who, scale = kind["who"], kind["scale"]
    if config.AGE_HINT_ENABLED:                      # автодетект как запасной путь
        auto = face_metric.describe_guest(guest_bytes)
        who, scale = auto.get("who", who), auto.get("scale", scale)
    if who:
        print(f"[guest] {outfit} → {who}, масштаб {scale}")
    gender = "young woman" if outfit in ("female", "girl") else "young man"

    # Режим генерации можно задать площадке отдельно: "composite" (человек
    # вклеивается в эталон) или "edit" (модель сама вписывает гостя в кадр).
    # Кабине нужен edit: composite умеет вклеивать только фигуру в полный рост,
    # а в кресле человек должен СИДЕТЬ — стоящий силуэт там нелеп.
    mode = loc.get("mode", config.GEN_MODE)
    if mode == "composite" and reference:
        # основной режим: фон не генерируется — человек вклеивается в эталон
        import person_composite
        variants = []
        for i in range(config.VARIANTS):
            out = person_composite.generate_composite(
                face_png, body_png, reference,
                outfits[i % len(outfits)], loc.get("anchor", {}), loc.get("light", ""),
                who=who, scale=scale, fill=loc.get("frame_fill"),
                frame_cx=loc.get("frame_cx"))
            if out:
                variants.append(out)
        if not variants:
            raise HTTPException(502, "Не удалось сгенерировать кадр. Попробуйте ещё раз.")

        # Перенос настоящего лица гостя на готовый кадр. Замер 06.09.2026 на
        # имитациях кадров с вебки: сходство 0.697 → 0.799, восковых лиц нет.
        # Свап делается ПАРАЛЛЕЛЬНО по вариантам: он занимает 30-60 с, и
        # последовательно удвоил бы ожидание гостя. Неудача не критична —
        # вариант остаётся без свапа.
        if config.FACE_SWAP_ENABLED and replicate_client.available():
            import concurrent.futures
            def _swap(img):
                try:
                    return replicate_client.face_swap(img, face_raw) or img
                except Exception as exc:  # noqa: BLE001
                    print(f"[swap] сбой: {type(exc).__name__}")
                    return img
            t_swap = time.time()
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(variants)) as pool:
                variants = list(pool.map(_swap, variants))
            print(f"[swap] перенос лица на {len(variants)} кадр(ах) за {time.time() - t_swap:.0f}с")
    else:
        # В edit-режиме подсказку кладём первой фразой: дальше идёт описание сцены,
        # и модель должна знать, кого в неё вписывать, до всех прочих указаний.
        hint = f"The person in image 1 is {who}. " if who else ""
        prompts = [hint + loc["prompt"].replace("{OUTFIT}", outfits[i % len(outfits)]).replace("{GENDER}", gender)
                   for i in range(config.VARIANTS)]
        # фирменный знак отдельным изображением: словами идентичности не добиться,
        # модель должна видеть настоящий логотип и скопировать его на мерч
        brand_logo = None
        if config.BRAND_LOGO_ENABLED and config.BRAND_LOGO_FILE.exists():
            limg = Image.open(config.BRAND_LOGO_FILE).convert("RGBA")
            flat = Image.new("RGB", limg.size, (255, 255, 255))
            flat.paste(limg, mask=limg.split()[3])          # прозрачность → белый фон
            lbuf = io.BytesIO(); flat.save(lbuf, format="PNG"); brand_logo = lbuf.getvalue()
        try:
            variants = gemini_client.generate_variants(prompts, face_png, reference,
                                                       body_png=body_png, swap_face=face_raw,
                                                       brand_logo=brand_logo)
        except gemini_client.GenerationError as exc:
            raise HTTPException(502, str(exc))

    # отбраковка кадров с полным ростом: модель иногда игнорирует FRAMING LOCK
    # и отходит камерой назад. Проверяем детерминированно — по доле высоты кадра,
    # занятой лицом. Бракуем только если остаётся хотя бы один нормальный кадр.
    if config.FRAME_MIN_FACE > 0 and len(variants) > 1:
        ratios = [face_metric.frame_ratio(v) for v in variants]
        good = [i for i, r in enumerate(ratios) if r is not None and r >= config.FRAME_MIN_FACE]
        if good and len(good) < len(variants):
            print(f"[frame] доли лица: {[round(r, 3) if r else None for r in ratios]} → "
                  f"полный рост отбракован, оставлено {len(good)}")
            variants = [variants[i] for i in good]

    # отбраковка кадров, где модель дорисовала второго человека (сосед гостя в
    # кадре, толпа за спиной). Как и выше — только если остаётся чистый вариант.
    if config.EXTRA_PEOPLE_CHECK and variants:
        extra = [face_metric.extra_people(v) for v in variants]
        if any(extra):
            clean = [i for i, n in enumerate(extra) if n == 0]
            if clean and len(clean) < len(variants):
                variants = [variants[i] for i in clean]
                print(f"[people] лишние люди в вариантах {extra} → оставлено {len(variants)}")
            else:
                print(f"[people] ВНИМАНИЕ: лишние люди во всех вариантах {extra}, заменить нечем")

    # ранжирование по сходству с гостем (ArcFace): лучший кадр — первым; слабые
    # (ниже порога) отбраковываются, но хотя бы один вариант всегда остаётся.
    sims: list = [None] * len(variants)
    if config.FACE_RANK_ENABLED:
        ranking = face_metric.rank_variants(guest_bytes, variants)
        if ranking:
            kept = [(i, s) for i, s in ranking if s >= config.FACE_SIM_THRESHOLD] or ranking[:1]
            # Относительный фильтр вдобавок к абсолютному порогу. Кадр, у которого
            # face-swap не применился (сходство ~0.53), проходит порог 0.45 и встаёт
            # рядом с нормальным (~0.75) — гость может выбрать именно его и получить
            # «не своё» лицо (случай 26.07.2026). Сильно хуже лучшего — в брак.
            best = kept[0][1]
            kept = [(i, s) for i, s in kept if s >= best - config.FACE_SIM_SPREAD] or kept[:1]
            variants = [variants[i] for i, _ in kept]
            sims = [round(s, 3) for _, s in kept]
            print(f"[rank] similarities: {[round(s, 3) for _, s in ranking]} → оставлено {len(variants)}")

    session_id = uuid.uuid4().hex[:8]
    out = []
    for i, data in enumerate(variants):
        name = f"gen_{session_id}_{i}.png"
        _save(data, name)
        view = _save_preview(data, name)
        out.append({"id": name, "url": f"/files/{view}", "full_url": f"/files/{name}",
                    "similarity": sims[i] if i < len(sims) else None})

    return {"session": session_id, "location": loc["title"], "variants": out,
            "stub_mode": config.STUB_MODE}


@app.post("/api/card")
def make_card(variant_id: str = Form(...), location: str = Form("")):
    src = config.OUTPUT / variant_id
    if not src.exists():
        raise HTTPException(404, "Вариант не найден")
    loc = LOCATIONS.get(location, {})
    card = compositor.build_card(src.read_bytes(),
                                 caption_lines=loc.get("card_caption", []),
                                 footer=loc.get("card_footer", ""))
    card_id = f"card_{uuid.uuid4().hex[:8]}.png"
    _save(card, card_id)
    card_view = _save_preview(card, card_id)

    # QR на цифровую версию
    qr_url = f"{config.PUBLIC_BASE_URL}/d/{card_id}"
    qr = qrcode.make(qr_url)
    qbuf = io.BytesIO(); qr.save(qbuf, format="PNG")
    qr_id = f"qr_{card_id}"
    _save(qbuf.getvalue(), qr_id)

    return {"card_id": card_id, "card_url": f"/files/{card_id}",
            "card_preview_url": f"/files/{card_view}",
            "qr_url": f"/files/{qr_id}", "digital_url": qr_url}


# ---------------- Загрузка фото с телефона гостя (QR-флоу партнёра) ----------------

@app.post("/api/upload-session")
def create_upload_session(request: Request):
    """Киоск вызывает перед показом QR. Возвращает session_id и QR-код."""
    session_id = uuid.uuid4().hex[:12]
    _upload_sessions[session_id] = {"status": "waiting", "path": None, "created_at": time.time()}

    # Если задан публичный URL (домен/туннель) — используем его.
    # Иначе определяем локальный IP для работы в одной сети (мини-ПК киоска).
    pub = config.PUBLIC_BASE_URL
    if pub and "localhost" not in pub and "127.0.0.1" not in pub:
        upload_url = f"{pub}/u/{session_id}"
    else:
        import subprocess as _sp
        local_ip = None
        for iface in ("en0", "en1", "eth0", "wlan0"):
            try:
                out = _sp.check_output(["ipconfig", "getifaddr", iface],
                                       stderr=_sp.DEVNULL, text=True).strip()
                if out and not out.startswith("127."):
                    local_ip = out
                    break
            except Exception:
                pass
        if not local_ip:
            local_ip = str(request.base_url.hostname)
        port = request.base_url.port or 8000
        upload_url = f"http://{local_ip}:{port}/u/{session_id}"
    qr = qrcode.make(upload_url)
    qr_buf = io.BytesIO(); qr.save(qr_buf, format="PNG")
    qr_name = f"qr_up_{session_id}.png"
    _save(qr_buf.getvalue(), qr_name)

    return {"session_id": session_id, "upload_url": upload_url, "qr_url": f"/files/{qr_name}"}


@app.get("/api/upload-status/{session_id}")
def upload_status(session_id: str):
    """Киоск опрашивает раз в 2 сек — ждёт когда гость загрузит фото."""
    s = _upload_sessions.get(session_id)
    if not s:
        raise HTTPException(404, "Сессия не найдена")
    return {"ready": s["status"] == "ready", "photo_id": s.get("path")}


@app.post("/api/upload-photo/{session_id}")
async def receive_upload(session_id: str, photo: UploadFile = File(...)):
    """Мобильная страница POST сюда — сохраняем фото и помечаем сессию готовой."""
    s = _upload_sessions.get(session_id)
    if not s:
        raise HTTPException(404, "Сессия не найдена или истекла")
    data = await photo.read()
    name = f"upload_{session_id}.jpg"
    _save(data, name)
    s["status"] = "ready"
    s["path"] = name
    return {"ok": True}


@app.get("/u/{session_id}", response_class=HTMLResponse)
def mobile_upload_page(session_id: str):
    """Мобильная страница для гостя: открывается по QR, гость выбирает фото из галереи."""
    if session_id not in _upload_sessions:
        return HTMLResponse("<h2 style='font-family:sans-serif;padding:40px'>Сессия не найдена или истекла</h2>", 404)
    return HTMLResponse(f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>Ремтехника · Загрузить фото</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#0d0d0f;color:#fff;font-family:-apple-system,Arial,sans-serif;
  min-height:100vh;display:flex;flex-direction:column;align-items:center;
  justify-content:center;padding:32px 24px;text-align:center}}
.logo{{font-size:13px;font-weight:800;letter-spacing:3px;color:#ffc20e;margin-bottom:24px}}
h1{{font-size:28px;font-weight:700;margin-bottom:12px}}
p{{color:#a2a2ab;font-size:17px;line-height:1.6;margin-bottom:36px}}
.btn{{display:block;width:100%;max-width:360px;background:linear-gradient(180deg,#ffc20e,#e8ae00);
  color:#0d0d0f;border:none;border-radius:20px;padding:22px;font-size:20px;
  font-weight:700;cursor:pointer;text-align:center}}
.status{{margin-top:28px;font-size:17px;color:#ffc20e;min-height:26px;line-height:1.5}}
.status.err{{color:#f87171}}
input[type=file]{{display:none}}
</style>
</head>
<body>
<div class="logo">РЕМТЕХНИКА</div>
<h1>«Я в деле»</h1>
<p>Выберите своё фото из галереи.<br>Оно автоматически появится на стенде.</p>
<label class="btn" for="photo">📷 Выбрать фото</label>
<input type="file" id="photo" accept="image/*">
<p class="status" id="status"></p>
<script>
document.getElementById('photo').addEventListener('change', async function() {{
  const file = this.files[0];
  if (!file) return;
  const st = document.getElementById('status');
  st.className = 'status';
  st.textContent = 'Отправляем фото…';
  const fd = new FormData();
  fd.append('photo', file, file.name);
  try {{
    const r = await fetch('/api/upload-photo/{session_id}', {{method:'POST', body:fd}});
    if (r.ok) {{
      st.textContent = '✓ Готово! Смотрите на экран стенда.';
    }} else {{
      st.className = 'status err';
      st.textContent = 'Ошибка — попробуйте ещё раз.';
    }}
  }} catch(e) {{
    st.className = 'status err';
    st.textContent = 'Нет соединения. Убедитесь что вы в сети стенда.';
  }}
}});
</script>
</body>
</html>""")


@app.post("/api/send-email")
def send_email_card(card_id: str = Form(...), email: str = Form(...)):
    """Отправка готовой карточки на почту гостя (модуль партнёра, Яндекс SMTP)."""
    path = _output_file(card_id)
    try:
        result = email_client.send_card(email, path)
    except Exception as exc:  # noqa: BLE001
        # Текст исключения нужен журналу; гостю на экране он ничего не говорит.
        print(f"[email] сбой отправки: {exc!r}")
        return {"sent": False, "reason": "Не удалось отправить письмо. Заберите карточку по QR-коду."}
    return result


def _queue_marker(card_id: str) -> Path:
    """Метка «карточка ждёт печати». Простой файл рядом с карточкой: переживает
    перезапуск сервиса и не требует базы."""
    return config.OUTPUT / f"toprint_{card_id}.flag"


@app.post("/api/print")
def print_card(card_id: str = Form(...)):
    """Кнопка «Отправить в печать» у гостя.

    Печатать с сервера нельзя: он в другой стране, принтер стоит у киоска.
    Поэтому карточка ставится в очередь, а программа оператора у принтера
    забирает задания через /api/print-jobs и печатает их локально.
    Серверная печать через lpr остаётся на случай, если приложение однажды
    переедет на компьютер киоска — тогда PRINT_ENABLED=1 и очередь не нужна."""
    path = _output_file(card_id)

    if config.PRINT_ENABLED:
        cmd = ["lpr"]
        if config.PRINT_PRINTER:
            cmd += ["-P", config.PRINT_PRINTER]
        cmd.append(str(path))
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=30)
        except Exception as exc:  # noqa: BLE001
            print(f"[print] сбой lpr: {exc!r}")
            raise HTTPException(500, "Не удалось отправить карточку на печать")
        return {"printed": True, "printer": config.PRINT_PRINTER or "default"}

    _queue_marker(path.name).write_text(str(time.time()), encoding="utf-8")
    print(f"[print] в очередь: {path.name}")
    return {"printed": False, "queued": True, "card_id": path.name}


# ---- API очереди печати для программы оператора ----

@app.get("/api/print-jobs")
def print_jobs(key: str = "", include_done: bool = False):
    """Список карточек, ожидающих печати. Забирает программа у принтера.

    Отдаёт JSON, поэтому клиенту достаточно HTTP — ни SSH, ни доступа к серверу
    не требуется. Ключ тот же, что у страницы очереди."""
    if not config.PRINT_QUEUE_KEY or key != config.PRINT_QUEUE_KEY:
        raise HTTPException(404, "Не найдено")

    jobs = []
    for flag in sorted(config.OUTPUT.glob("toprint_*.flag"), key=lambda p: p.stat().st_mtime):
        card = flag.name[len("toprint_"):-len(".flag")]
        if not (config.OUTPUT / card).is_file():
            flag.unlink(missing_ok=True)      # карточку удалили — задание неактуально
            continue
        jobs.append({
            "card_id": card,
            "url": f"{config.PUBLIC_BASE_URL}/files/{card}",
            "queued_at": round(flag.stat().st_mtime, 3),
        })
    return {"jobs": jobs, "count": len(jobs)}


@app.post("/api/print-jobs/done")
def print_job_done(card_id: str = Form(...), key: str = Form("")):
    """Программа оператора подтверждает, что карточка напечатана."""
    if not config.PRINT_QUEUE_KEY or key != config.PRINT_QUEUE_KEY:
        raise HTTPException(404, "Не найдено")
    name = os.path.basename(str(card_id))
    flag = _queue_marker(name)
    existed = flag.exists()
    flag.unlink(missing_ok=True)
    print(f"[print] напечатано: {name}" if existed else f"[print] задания не было: {name}")
    return {"ok": True, "was_queued": existed}


@app.get("/d/{card_id}", response_class=HTMLResponse)
def digital(card_id: str):
    _output_file(card_id)  # проверка, что запрошен файл из output, а не путь наружу
    return f"""<!doctype html><html lang=ru><head><meta charset=utf-8>
<meta name=viewport content='width=device-width,initial-scale=1'>
<title>Ремтехника · ваша карточка</title>
<style>body{{margin:0;background:#0d0d0f;color:#fff;font-family:-apple-system,Arial,sans-serif;text-align:center}}
img{{max-width:92%;margin:24px auto;border-radius:12px;box-shadow:0 8px 30px rgba(0,0,0,.4)}}
a{{display:inline-block;margin:12px;padding:14px 24px;background:#ffc20e;color:#0d0d0f;border-radius:10px;text-decoration:none;font-weight:700}}</style>
</head><body><h2>Ваша карточка · «Ремтехника»</h2>
<img src='/files/{card_id}'><br><a href='/files/{card_id}' download>Скачать фото</a></body></html>"""


# ---------------- Очередь печати (для оператора у принтера) ----------------

@app.get("/print-queue", response_class=HTMLResponse)
def print_queue(key: str = "", limit: int = 40, kind: str = "card"):
    """Страница со свежими карточками для компьютера, к которому подключён принтер.

    Печать идёт не с сервера: он в другой стране, а принтер стоит у киоска.
    Оператор открывает эту страницу в браузере, видит новые карточки по мере
    их появления и печатает нужную штатным драйвером. Никаких доступов к коду
    и серверу для этого не требуется.

    Доступ по ключу: на странице лица гостей, и открытым её оставлять нельзя."""
    if not config.PRINT_QUEUE_KEY or key != config.PRINT_QUEUE_KEY:
        raise HTTPException(404, "Не найдено")

    prefix = "gen_" if kind == "photo" else "card_"
    files = sorted((p for p in config.OUTPUT.glob(f"{prefix}*.png") if p.is_file()),
                   key=lambda p: p.stat().st_mtime, reverse=True)[:max(1, min(limit, 200))]

    cards = "".join(
        f"<figure><a href='/files/{p.name}' target='_blank'>"
        f"<img src='/files/{p.name}' loading='lazy'></a>"
        f"<figcaption>{time.strftime('%H:%M:%S', time.localtime(p.stat().st_mtime))}"
        f" · {p.stat().st_size // 1024} КБ<br>"
        f"<a class='dl' href='/files/{p.name}' download>Скачать</a></figcaption></figure>"
        for p in files
    ) or "<p class='empty'>Пока пусто — карточки появятся здесь сразу после съёмки.</p>"

    other = "photo" if kind == "card" else "card"
    other_name = "оригиналы без рамки" if kind == "card" else "готовые карточки"

    # Полоса состояния оплаты. 31.07.2026 киоск встал посреди форума, и это
    # заметили только по остановке — баланс через API Replicate не отдаётся,
    # поэтому показываем то, что провайдер сам сообщает в ошибках.
    b = replicate_client.billing_state()
    hours = max((time.time() - b["started_at"]) / 3600, 0.01)
    if b["status"] == "empty":
        bill_bg, bill_text = "#7f1d1d", (
            "ДЕНЬГИ НА REPLICATE КОНЧИЛИСЬ — генерация не работает. "
            "Пополните баланс: replicate.com/account/billing")
    elif b["status"] == "low":
        bill_bg, bill_text = "#7c4a11", (
            "БАЛАНС НИЖЕ $5 — провайдер режет скорость, генерация идёт долго "
            "и срывается. Пополните заранее: replicate.com/account/billing")
    else:
        bill_bg, bill_text = "#14424a", "Генерация работает, ограничений нет"
    bill_stats = (f"с запуска сервиса: кадров {b['images']}, переносов лица "
                  f"{b['swaps']}, ориентировочно ${b['spent']:.2f} "
                  f"(~${b['spent'] / hours:.2f} в час)")

    return HTMLResponse(f"""<!doctype html><html lang=ru><head><meta charset=utf-8>
<meta name=viewport content='width=device-width,initial-scale=1'>
<meta http-equiv=refresh content='15'>
<title>Очередь печати · Ремтехника</title>
<style>
 body{{margin:0;background:#0e1a22;color:#e8f1f3;font-family:-apple-system,Arial,sans-serif}}
 header{{position:sticky;top:0;background:#061226;padding:14px 20px;display:flex;
   align-items:center;gap:16px;flex-wrap:wrap;border-bottom:1px solid rgba(255,255,255,.12)}}
 h1{{font-size:17px;margin:0;font-weight:800;letter-spacing:1px}}
 .hint{{color:#8fb0b8;font-size:13px}}
 a.sw{{color:#5fd0df;font-size:13px}}
 .grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));
   gap:16px;padding:20px}}
 figure{{margin:0;background:#16242c;border-radius:12px;overflow:hidden;
   border:1px solid rgba(255,255,255,.08)}}
 figure img{{width:100%;display:block;background:#fff}}
 figcaption{{padding:8px 10px;font-size:12px;color:#9fbcc4;text-align:center}}
 a.dl{{display:inline-block;margin-top:6px;padding:6px 14px;background:#14707f;
   color:#fff;border-radius:8px;text-decoration:none;font-weight:700}}
 .empty{{padding:40px;text-align:center;color:#8fb0b8}}
 .bill{{padding:10px 20px;font-size:13px;line-height:1.5;background:{bill_bg};
   border-bottom:1px solid rgba(255,255,255,.15)}}
 .bill b{{font-size:14px;letter-spacing:.4px}}
 .bill span{{color:rgba(255,255,255,.72)}}
</style></head><body>
<header>
  <h1>ОЧЕРЕДЬ ПЕЧАТИ</h1>
  <span class=hint>{len(files)} шт. · обновляется само каждые 15 сек · новые сверху</span>
  <a class=sw href='/print-queue?key={key}&kind={other}'>показать {other_name}</a>
</header>
<div class=bill><b>{bill_text}</b><br><span>{bill_stats}</span></div>
<div class=grid>{cards}</div>
</body></html>""", headers={"Cache-Control": "no-store"})


# ---------------- Статика ----------------

@app.get("/", response_class=HTMLResponse)
def index():
    """Главную отдаём без кэша и с версией у скриптов.

    Иначе браузер гостя остаётся на старом app.js после обновления. 26.07.2026
    это сломало бы флоу: контракт /api/generate сменился на job_id + опрос, а
    закэшированный скрипт ждёт прежний ответ и падает на разборе. Версия берётся
    из времени правки файлов, так что бампать её руками не нужно."""
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    try:
        v = int(max((FRONTEND / n).stat().st_mtime for n in ("app.js", "styles.css")))
    except OSError:
        v = 0
    return HTMLResponse(html.replace("__V__", str(v)),
                        headers={"Cache-Control": "no-store, must-revalidate"})


app.mount("/files", StaticFiles(directory=str(config.OUTPUT)), name="files")
app.mount("/logos", StaticFiles(directory=str(config.LOGOS)), name="logos")
if FRONTEND.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND), html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)

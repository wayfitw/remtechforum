"""Конфигурация AI-фотоинсталляции «Ремтехника».

Все параметры читаются из переменных окружения (.env). Секретов в коде нет.
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ASSETS = BASE_DIR / "assets"
REFERENCES = ASSETS / "references"
LOGOS = ASSETS / "logos"          # белые версии — для веб-интерфейса (тёмный фон)
CARD_LOGOS = ASSETS / "logos_card"  # цветные — для печатной карточки (белый фон)
OUTPUT = ASSETS / "output"
for _d in (REFERENCES, LOGOS, CARD_LOGOS, OUTPUT):
    _d.mkdir(parents=True, exist_ok=True)


def _load_dotenv() -> None:
    """Минимальный парсер .env без внешних зависимостей."""
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()

# --- Провайдер генерации (Gemini «Nano Banana») ---
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
# «Nano Banana» = gemini-2.5-flash-image; «Nano Banana Pro» = gemini-3-pro-image-preview
GEMINI_IMAGE_MODEL = os.environ.get("GEMINI_IMAGE_MODEL", "gemini-2.5-flash-image").strip()

# Сколько вариантов генерировать на один запрос (гость выбирает лучший)
VARIANTS = int(os.environ.get("VARIANTS", "3"))

# Образы (одежда) по выбору гостя — подставляются в промпт вместо {OUTFIT}.
# Единый образ для мужчин и женщин: фирменная спецовка «Ремтехника» — та же, что
# на эталонных кадрах. Формулировка намеренно подробная: модель охотно рисует
# «какую-нибудь» жилетку, если не описать полосы, кепку и место знака на груди.
# Кепка, а не каска — решение заказчика от 02.09.2026.
_WORKWEAR_OUTFIT = (
    'the official Remtehnika work jacket exactly as on the reference shots: a black work jacket with '
    'bright yellow panels down the sides and on the forearms, grey retroreflective stripes across the '
    'sleeves, and on the left chest a yellow rectangular badge with the black lowercase letters "rt" '
    'and, directly below it, the small Cyrillic line «ООО РЕМТЕХНИКА» in dark capitals. IMAGE 4 IS THE '
    'OFFICIAL BRAND LOGO — reproduce it EXACTLY as in image 4 on that chest badge and on the front of '
    'the headwear: a yellow plate with cut corners and the black letters "rt" inside. On the head a '
    'BLACK BASEBALL CAP with a curved peak carrying the same logo — a cap, never a hard hat. Keep the '
    'identical shapes, proportions and colours of the logo, scaled to the garment and following the '
    'folds of the fabric and the curve of the cap; do not redraw, restyle or invent a different emblem, '
    'do not add any other lettering, and do not use image 4 anywhere else. Black work gloves, dark work '
    'trousers. The workwear is clean and well fitted, not worn or dirty'
)
# Городской образ для площадок «не про работу» (Столбы): те же джинсы и худи,
# что на согласованных кадрах. Спецовка там смотрится нелепо — человек приехал
# на скалы, а не на смену.
_CASUAL_OUTFIT = (
    'classic blue denim jeans, well fitted, with visible seams and pockets, and a black hoodie with a '
    'hood and long sleeves, worn loosely. IMAGE 4 IS THE OFFICIAL BRAND LOGO - reproduce it EXACTLY on '
    'the chest of the hoodie: a yellow plate with cut corners and the black lowercase letters "rt" '
    'inside, printed flat on the fabric, following its folds, about a hand span wide and clearly '
    'legible. Do not redraw, restyle or invent a different emblem, add no other lettering or prints, '
    'and do not use image 4 anywhere else. Clean light sneakers. The clothes are clean and well fitted'
)

# Набор выбирается площадкой: поле "outfit" в locations.json ("workwear" | "casual").
OUTFIT_SETS = {
    "workwear": _WORKWEAR_OUTFIT,
    "casual": _CASUAL_OUTFIT,
}

OUTFITS = {
    "female": [_WORKWEAR_OUTFIT],
    "male": [_WORKWEAR_OUTFIT],
}
DEFAULT_OUTFIT = "neutral clean industrial workwear in muted colors"

# --- Резервный провайдер (Replicate, FLUX Kontext Pro) — ADR-6 ---
# Токен из https://replicate.com/account/api-tokens. Пусто → резерв выключен.
REPLICATE_API_TOKEN = os.environ.get("REPLICATE_API_TOKEN", "").strip()

# Модель генерации на Replicate. По итогам A/B-теста (18.07.2026) —
# google/nano-banana-2 в 2K: лучшее лицо БЕЗ face-swap (см. _ab_*.jpg).
# Альтернативы: bytedance/seedream-4, google/nano-banana-pro.
# По итогам тестов 23.07.2026 выбран google/nano-banana-pro: по ArcFace он на
# уровне seedream-4.5 (0.848 vs 0.851), но визуально даёт лучший результат.
# Альтернатива (быстрее ~2x): bytedance/seedream-4.5:9fe3b8282dcb9d9063b05e33210a1432801f7c5a6641db944baefcec4886761a
NANO_BANANA_MODEL = os.environ.get("NANO_BANANA_MODEL", "google/nano-banana-pro").strip()
NANO_BANANA_RESOLUTION = os.environ.get("NANO_BANANA_RESOLUTION", "2K").strip()

# Качество для openai/gpt-image-*: high даёт >6 мин на кадр (для киоска слишком долго),
# поэтому по умолчанию medium. Допустимо: low | medium | high | auto.
GPT_IMAGE_QUALITY = os.environ.get("GPT_IMAGE_QUALITY", "medium").strip()

# Резервная модель на случай, когда основная отвечает «Service is temporarily
# unavailable (E004)». По статистике запусков у seedream-4.5 отказов заметно
# меньше, поэтому гость получает кадр, а не ошибку. Пусто — резерв выключен.
FALLBACK_MODEL = os.environ.get(
    "FALLBACK_MODEL",
    "bytedance/seedream-4.5:9fe3b8282dcb9d9063b05e33210a1432801f7c5a6641db944baefcec4886761a").strip()

# Face-swap (inswapper) работает в 128×128 → «восковое» лицо. ВЫКЛЮЧЕН по умолчанию;
# включать только как аварийный вариант, если генерация теряет сходство.
FACE_SWAP_ENABLED = os.environ.get("FACE_SWAP", "0").strip() in ("1", "true", "yes")

# Фирменный логотип бренда, который модель должна воспроизвести на мерче.
# Передаётся в генерацию ОТДЕЛЬНЫМ изображением (описанием словами идентичности
# не добиться — модель рисует «похожее»). Файл: assets/logos/_brand_cap.png.
BRAND_LOGO_ENABLED = os.environ.get("BRAND_LOGO", "1").strip() in ("1", "true", "yes")
# _brand_cap.png — знак «rt» для нашивки на спецовке и каске.
# Имя с подчёркивания: так файл не попадает ни в /api/logos (веб), ни в ряд на карточке.
BRAND_LOGO_FILE = LOGOS / os.environ.get("BRAND_LOGO_FILE", "_brand_cap.png")

# Улучшение входного фото гостя через GFPGAN (для вебкамеры: чистит шум/блюр,
# делает лицо резче и красивее). Небольшой минус к ArcFace, но картинка лучше.
FACE_ENHANCE_ENABLED = os.environ.get("FACE_ENHANCE", "0").strip() in ("1", "true", "yes")

# Доработка после свапа: лёгкий GFPGAN-блендинг (красивее кожа, сходство почти держится).
SWAP_REFINE_ENABLED = os.environ.get("SWAP_REFINE", "0").strip() in ("1", "true", "yes")
SWAP_REFINE_ALPHA = float(os.environ.get("SWAP_REFINE_ALPHA", "0.3"))  # доля GFPGAN в бленде
# Резкость после свапа (unsharp): чётче контуры губ/лица, идентичность не страдает.
SWAP_SHARPEN_ENABLED = os.environ.get("SWAP_SHARPEN", "0").strip() in ("1", "true", "yes")
SWAP_SHARPEN_PERCENT = int(os.environ.get("SWAP_SHARPEN_PERCENT", "70"))
# Выравнивание света на входном кадре (убирает тени с лица с вебки), CLAHE.
FACE_DESHADOW_ENABLED = os.environ.get("FACE_DESHADOW", "0").strip() in ("1", "true", "yes")
FACE_DESHADOW_CLIP = float(os.environ.get("FACE_DESHADOW_CLIP", "2.0"))

# Кадрирование composite-кадра вокруг гостя (см. person_composite._frame_on_person).
# CARD_FILL — доля высоты кадра под фигуру: 0.8 даёт долю лица ~0.12-0.13 при
# пороге отбраковки FRAME_MIN_FACE=0.10, то есть с запасом.
CARD_FILL = float(os.environ.get("CARD_FILL", "0.66"))
# Доля высоты кадра, которую занимает ЛИЦО гостя. По ней масштабируется
# фигура: так размер лица не зависит от того, обрезала модель ноги или нет.
# 0.13 — с запасом над порогом отбраковки FRAME_MIN_FACE=0.10.
TARGET_FACE = float(os.environ.get("TARGET_FACE", "0.13"))

# Плёночный грейд на готовый кадр. На «Сахалине» сочность давала сама модель
# (там кадр рисовался целиком по промпту с cinematic colour grade); у нас фон
# берётся из эталона нетронутым, поэтому цвет вытягиваем сами.
GRADE_ENABLED = os.environ.get("GRADE", "1").strip() in ("1", "true", "yes")
GRADE_SATURATION = float(os.environ.get("GRADE_SATURATION", "1.14"))
GRADE_CONTRAST = float(os.environ.get("GRADE_CONTRAST", "0.22"))
GRADE_VIGNETTE = float(os.environ.get("GRADE_VIGNETTE", "0.10"))
# Какая доля роста остаётся в кадре, считая от макушки: 0.62 — поясной портрет
# со срезом по бедру. Меньше — крупнее лицо, но теряется поза и фон.
CARD_PERSON_PART = float(os.environ.get("CARD_PERSON_PART", "0.62"))
# Пропорции кадра (ширина/высота). 0.77 — как у печатной карточки, поэтому
# на карточке ничего не досрезается по бокам.
CARD_ASPECT = float(os.environ.get("CARD_ASPECT", "0.77"))

# Режим генерации:
#   composite — фон НЕ генерируется: генерим только человека, вырезаем и вклеиваем
#               в эталон (фон гарантированно неизменен). Основной режим.
#   edit      — модель редактирует эталон целиком (фон может «уплывать»). Резерв.
GEN_MODE = os.environ.get("GEN_MODE", "composite").strip()

# --- Печать ---
# По умолчанию печать ВЫКЛючена (карточка просто сохраняется) — чтобы не печатать случайно.
# Включить реальную печать через CUPS/lpr: PRINT_ENABLED=1, PRINT_PRINTER=<имя из `lpstat -p`>
PRINT_ENABLED = os.environ.get("PRINT_ENABLED", "0").strip() in ("1", "true", "yes")
PRINT_PRINTER = os.environ.get("PRINT_PRINTER", "").strip()

# Ключ доступа к странице /print-queue — она для оператора у принтера, и на ней
# лица гостей, поэтому без ключа страница отдаёт 404. Пусто → страница выключена.
PRINT_QUEUE_KEY = os.environ.get("PRINT_QUEUE_KEY", "").strip()

# Публичный базовый URL для QR (в проде — домен; локально — адрес мини-ПК)
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")

# Срок хранения цифровой версии (часы) — для автоудаления (в проде)
DIGITAL_TTL_HOURS = int(os.environ.get("DIGITAL_TTL_HOURS", "72"))
# Отладочные dbg_* и отклонённые rej_* — свой, более короткий срок: их объём
# втрое больше самих карточек (~4 МБ на гостя), а нужны они только для разбора
# свежей жалобы. Гостю они не отдаются.
DEBUG_TTL_HOURS = int(os.environ.get("DEBUG_TTL_HOURS", "12"))

# Пороги, ниже которых входное фото считается слабым и его стоит прогнать через
# GFPGAN. Выше — не трогаем: он синтезирует лицо заново и на хорошем снимке даёт
# пластиковую кожу и раздутые черты. Ориентиры по замерам: вебка даёт лицо
# 250-420 px и резкость 58-131, телефон — 630-780 px и 212-649.
FACE_ENHANCE_MAX_PX = int(os.environ.get("FACE_ENHANCE_MAX_PX", "450"))
FACE_ENHANCE_MAX_SHARP = float(os.environ.get("FACE_ENHANCE_MAX_SHARP", "140"))

# --- Email (отправка карточки гостю, Яндекс SMTP) ---
# Письма шлются только когда задан SMTP_PASS (App-пароль из Яндекс ID:
# Безопасность → Пароли приложений → Почта). Без него /api/send-email
# вернёт вежливый отказ, флоу не ломается.
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.yandex.ru").strip()
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
SMTP_USER = os.environ.get("SMTP_USER", "").strip()
SMTP_PASS = os.environ.get("SMTP_PASS", "").strip()
SMTP_FROM = os.environ.get("SMTP_FROM", "").strip()

# --- ArcFace-метрика сходства лиц (insightface) — рекомендация №1 ---
FACE_MODEL = os.environ.get("FACE_MODEL", "buffalo_l").strip()
# Какие модули insightface грузить. По умолчанию только нужные — экономит RAM
# (важно на VPS с 2 GB). Минимальный набор для работы: detection,recognition
# (без landmark_3d_68 отключится только проверка поворота головы).
FACE_MODULES = [m.strip() for m in os.environ.get(
    "FACE_MODULES", "detection,recognition,landmark_3d_68").split(",") if m.strip()]
# Подсказка модели, КОГО она рисует. Без неё возраст угадывается только по кропам:
# на хорошем кадре ребёнок выходит ребёнком, но на слабом (мелкое лицо, тень,
# шапка) модель рисует взрослого. genderage из buffalo_l уже загружен, берём его.
# ПРОВЕРЕНО 06.09.2026: genderage из buffalo_l врёт — ребёнку 10 лет ставит 49,
# мужчине за тридцать 53. Автоопределение выключено, типаж берём с экрана выбора.
# Оставлено на случай другой модели: AGE_HINT=1 включит подсказку обратно
# (тогда вернуть genderage в FACE_MODULES).
AGE_HINT_ENABLED = os.environ.get("AGE_HINT", "0").strip() in ("1", "true", "yes")
# Ребёнок вклеивается тем же анкером, что и взрослый, и выходит ростом со взрослого.
# Уменьшаем фигуру: ребёнку — сильнее, подростку — мягче.
CHILD_SCALE = float(os.environ.get("CHILD_SCALE", "0.86"))
TEEN_SCALE = float(os.environ.get("TEEN_SCALE", "0.94"))

# Кто на фото — говорит сам гость на экране выбора. Подсказка НАМЕРЕННО без
# возраста: киоск сообщает модели только пол, а возраст она берёт с кропов лица
# и корпуса. Проверено 06.09.2026 — ребёнка рисует ребёнком; а вот навязанное
# слово «взрослый» сломало бы детские кадры. Автоопределение возраста через
# insightface genderage забраковано: ребёнку 10 лет ставит 49 (см. AGE_HINT).
GUEST_KINDS = {
    "male":   {"who": "male", "scale": 1.0},
    "female": {"who": "female", "scale": 1.0},
}
# Размер входа детектора: меньше — меньше памяти и быстрее на слабом CPU.
_ds = int(os.environ.get("FACE_DET_SIZE", "640"))
FACE_DET_SIZE = (_ds, _ds)
# Гейт входного фото гостя (размер лица, один в кадре, резкость) до генерации.
FACE_GATE_ENABLED = os.environ.get("FACE_GATE", "1").strip() in ("1", "true", "yes")
# Ранжирование/отбраковка сгенерированных вариантов по сходству с гостем.
FACE_RANK_ENABLED = os.environ.get("FACE_RANK", "1").strip() in ("1", "true", "yes")
FACE_MIN_PX = int(os.environ.get("FACE_MIN_PX", "512"))          # мин. ширина лица (реком. 512)
FACE_MAX_YAW = float(os.environ.get("FACE_MAX_YAW", "25"))       # макс. поворот головы, град
FACE_MIN_BLUR = float(os.environ.get("FACE_MIN_BLUR", "40"))     # устар.: var лапласиана по кадру, только в логах
# Мин. резкость ЛИЦА, приведённого к 256×256 — не зависит от разрешения вебки.
# Замеры 26.07.2026: нормальный кадр 77–121, слабая вебка с апскейлом ~51,
# заметный смаз ~22, сильный ~10. Порог 30 пропускает слабые камеры и режет смаз.
FACE_MIN_SHARP = float(os.environ.get("FACE_MIN_SHARP", "30"))
FACE_SIM_THRESHOLD = float(os.environ.get("FACE_SIM_THRESHOLD", "0.45"))  # порог отбраковки варианта
# Насколько вариант может отставать от лучшего по сходству, прежде чем уйдёт в брак.
# Кадры без свапа дают ~0.53 против ~0.75 у нормальных — разрыв 0.15 их отрезает,
# а обычный разброс двух удачных кадров (0.02–0.08) не трогает.
FACE_SIM_SPREAD = float(os.environ.get("FACE_SIM_SPREAD", "0.15"))
# Отбраковка кадров, где модель проигнорировала FRAMING LOCK и сняла полный рост.
# Доля высоты кадра, занятая лицом: поясной портрет 0.11–0.18, полный рост 0.05–0.10
# (калибровка по реальным генерациям 26.07.2026). 0 — проверка выключена.
FRAME_MIN_FACE = float(os.environ.get("FRAME_MIN_FACE", "0.10"))

# Демо-режим (заглушки) — только когда НИ ОДИН провайдер не настроен
STUB_MODE = not (bool(GEMINI_API_KEY) or bool(REPLICATE_API_TOKEN))

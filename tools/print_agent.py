"""Агент печати для ноутбука у принтера DNP QW410.

Сервер стоит в другой стране и печатать сам не может. Гость нажимает
«Отправить в печать», карточка встаёт в очередь на сервере, а этот агент на
ноутбуке у принтера забирает её по HTTPS и печатает на месте.

Печать идёт напрямую через Windows (GDI), без программы просмотра картинок.
Карточка с сервера 1200x1800 — это 4x6" при 300 dpi, родной размер DNP QW410;
она ложится ровно 10x15 см по центру листа. Драйвер DNP отдаёт лист 4x6 с
запасом под обрез (у DP-QW410 — 107x155 мм), этот запас остаётся белым. Если в
драйвере выбран другой размер бумаги, агент не печатает и пишет, что исправить, —
так рулон не уходит впустую.

Настройка — файл settings.txt рядом с программой (строки КЛЮЧ=ЗНАЧЕНИЕ) или
переменные окружения с теми же именами:
    PRINT_QUEUE_KEY  ключ доступа к очереди (обязателен)
    BASE_URL         адрес сайта, по умолчанию https://remtech-forum.ru
    PRINTER_NAME     имя принтера; пусто — первый, в имени которого есть QW410 или DNP
    POLL_SECONDS     как часто спрашивать сервер, по умолчанию 3
    DRY_RUN=1        скачивать и подтверждать, но НЕ печатать (проверка связки)

Запуск из исходников: pip install requests pillow pywin32; python print_agent.py
Сборка в один exe:    pyinstaller --onefile --name RemtehnikaPrint print_agent.py

На Mac печать идёт через CUPS (lp) с официальным драйвером DNP: агент сам
находит принтер, берёт у драйвера размер бумаги 4x6 и заполняет им лист.
Запуск на Mac — start-mac.command рядом с программой.
"""
from __future__ import annotations

import os
import platform
import subprocess
import sys
import time
from pathlib import Path

import requests
from PIL import Image

HERE = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent


def _load_settings() -> dict:
    cfg: dict[str, str] = {}
    f = HERE / "settings.txt"
    if f.exists():
        for line in f.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                cfg[k.strip()] = v.strip()
    for k in ("PRINT_QUEUE_KEY", "BASE_URL", "PRINTER_NAME", "POLL_SECONDS", "DRY_RUN"):
        if os.environ.get(k):
            cfg[k] = os.environ[k]
    return cfg


CFG = _load_settings()
BASE = (CFG.get("BASE_URL") or "https://remtech-forum.ru").rstrip("/")
KEY = CFG.get("PRINT_QUEUE_KEY", "")
PRINTER = CFG.get("PRINTER_NAME", "")
POLL = int(CFG.get("POLL_SECONDS") or 3)
DRY_RUN = CFG.get("DRY_RUN", "") in ("1", "true", "yes")

DOWNLOADS = HERE / "printed"
LOGFILE = HERE / "agent.log"
# Какие листы считаем бумагой 4x6. Драйвер DNP отдаёт лист с запасом под обрез:
# у DP-QW410 (драйвер 1.0.1.1) при размере 4x6 это 107x155 мм, а не ровно
# 101.6x152.4 — принтер печатает чуть за край, чтобы не было белой кромки.
# Прежняя проверка «пропорции ±3%» такой лист браковала (15.09.2026, стенд).
SHEET_SHORT_MM = (98, 112)
SHEET_LONG_MM = (148, 160)
CARD_INCHES = (4, 6)      # физический размер карточки: 1200x1800 при 300 dpi

# Коды GetDeviceCaps
LOGPIXELSX, LOGPIXELSY = 88, 90
PHYSICALWIDTH, PHYSICALHEIGHT, PHYSICALOFFSETX, PHYSICALOFFSETY = 110, 111, 112, 113

IS_WINDOWS = platform.system() == "Windows"

_reported: set[str] = set()   # по каким карточкам ошибку уже показали


# Консоль Windows по умолчанию в кодировке cp1252/cp866, и print кириллицы
# падал UnicodeEncodeError прямо на старте exe (проверено 13.09.2026, когда
# вывод перенаправлен). Переводим вывод в UTF-8, непечатное заменяем.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def log(msg: str) -> None:
    line = f"[{time.strftime('%d.%m %H:%M:%S')}] {msg}"
    try:
        print(line, flush=True)
    except (UnicodeEncodeError, OSError):
        pass                                # журнал в файле всё равно пишется
    try:
        with LOGFILE.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


# ---------------- Windows: выбор принтера и печать через GDI ----------------

def pick_printer() -> str:
    import win32print
    flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
    names = [p[2] for p in win32print.EnumPrinters(flags)]
    if PRINTER:
        if PRINTER not in names:
            raise SystemExit(f"Принтер «{PRINTER}» не найден. Установленные: {', '.join(names) or 'нет'}")
        return PRINTER
    for mark in ("QW410", "DNP"):
        for n in names:
            if mark.lower() in n.lower():
                return n
    default = win32print.GetDefaultPrinter()
    log(f"ВНИМАНИЕ: принтер DNP не найден, беру принтер по умолчанию «{default}». "
        f"Установленные: {', '.join(names)}")
    return default


def _caps(printer: str, *codes: int) -> list[int]:
    import win32ui
    dc = win32ui.CreateDC()
    dc.CreatePrinterDC(printer)
    try:
        return [dc.GetDeviceCaps(c) for c in codes]
    finally:
        dc.DeleteDC()


def sheet_problem(pw: int, ph: int, dx: int, dy: int) -> str | None:
    """None — лист 4x6 (ровный или с запасом под обрез); иначе текст, что исправить."""
    mm_w, mm_h = pw / dx * 25.4, ph / dy * 25.4
    short, long_ = sorted((mm_w, mm_h))
    if SHEET_SHORT_MM[0] <= short <= SHEET_SHORT_MM[1] and SHEET_LONG_MM[0] <= long_ <= SHEET_LONG_MM[1]:
        return None
    return (f"в драйвере выбран лист {round(mm_w)}x{round(mm_h)} мм, а карточка рассчитана на 10x15 см. "
            f"Откройте «Настройки печати» принтера и выберите размер 4x6.")


def placement(pw: int, ph: int, dx: int, dy: int, ox: int, oy: int,
              portrait: bool) -> tuple[int, int, int, int]:
    """Прямоугольник вывода карточки в координатах устройства.

    Карточка ложится ровно 4x6 дюйма по центру физического листа — без растяжения
    и обрезки: запас драйвера под обрез остаётся белым, как и рамка карточки
    (у неё ~55 px белого поля по краям). Если лист вдруг меньше 4x6, карточка
    вписывается в него целиком с сохранением пропорций."""
    cw_in, ch_in = CARD_INCHES if portrait else CARD_INCHES[::-1]
    cw, ch = cw_in * dx, ch_in * dy
    k = min(1.0, pw / cw, ph / ch)
    cw, ch = round(cw * k), round(ch * k)
    left, top = (pw - cw) // 2 - ox, (ph - ch) // 2 - oy
    return left, top, left + cw, top + ch


def print_windows(printer: str, path: Path, output_file: str | None = None) -> None:
    """Печать карточки на весь лист. output_file — только для проверки без
    бумаги: виртуальный PDF-принтер пишет в этот файл, не спрашивая имя."""
    import win32ui
    from PIL import ImageWin

    img = Image.open(path).convert("RGB")
    pw, ph, ox, oy, dx, dy = _caps(printer, PHYSICALWIDTH, PHYSICALHEIGHT, PHYSICALOFFSETX,
                                   PHYSICALOFFSETY, LOGPIXELSX, LOGPIXELSY)
    problem = sheet_problem(pw, ph, dx, dy)
    if problem:
        raise RuntimeError(problem)
    if (img.width > img.height) != (pw > ph):
        img = img.rotate(90, expand=True)
    rect = placement(pw, ph, dx, dy, ox, oy, portrait=img.height >= img.width)

    dc = win32ui.CreateDC()
    dc.CreatePrinterDC(printer)
    try:
        if output_file:
            dc.StartDoc(path.name, output_file)
        else:
            dc.StartDoc(path.name)
        dc.StartPage()
        # Координаты устройства отсчитываются от печатной области (см. placement).
        ImageWin.Dib(img).draw(dc.GetHandleOutput(), rect)
        dc.EndPage()
        dc.EndDoc()
    finally:
        dc.DeleteDC()


# ---------------- macOS: печать через CUPS ----------------

def _run(cmd: list[str]) -> str:
    """Вывод команды CUPS. Язык принудительно английский: на русской macOS lpstat
    пишет «принтер … простаивает», и разбор строк ломался бы."""
    env = dict(os.environ, LC_ALL="C", LANG="C")
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=env)
    return res.stdout


def parse_lpstat_printers(text: str) -> list[str]:
    return [line.split()[1] for line in text.splitlines()
            if line.startswith("printer ") and len(line.split()) > 1]


def parse_lpstat_default(text: str) -> str:
    line = text.strip()
    return line.rsplit(":", 1)[1].strip() if line.startswith("system default destination:") else ""


def find_4x6(options: str) -> str | None:
    """Имя размера 4x6 из вывода `lpoptions -l`. Драйверы называют его по-разному:
    w288h432 (4x6 дюйма в пунктах), 4x6, PC4x6, 102x152 — ищем любое из них."""
    for line in options.splitlines():
        key, _, values = line.partition(":")
        if key.split("/")[0].strip() not in ("PageSize", "media"):
            continue
        for value in values.split():
            name = value.lstrip("*")
            low = name.lower()
            if any(mark in low for mark in ("4x6", "w288h432", "102x152")):
                return name
    return None


def pick_cups_printer() -> str:
    names = parse_lpstat_printers(_run(["lpstat", "-p"]))
    if PRINTER:
        if PRINTER not in names:
            raise SystemExit(f"Принтер «{PRINTER}» не найден. Установленные: {', '.join(names) or 'нет'}")
        return PRINTER
    for mark in ("QW410", "DNP"):
        for n in names:
            if mark.lower() in n.lower():
                return n
    default = parse_lpstat_default(_run(["lpstat", "-d"]))
    if not default:
        raise SystemExit("Принтер не найден. Добавьте DNP QW410 в «Системных настройках → Принтеры "
                         "и сканеры» и запустите программу снова.")
    log(f"ВНИМАНИЕ: принтер DNP не найден, беру принтер по умолчанию «{default}». "
        f"Установленные: {', '.join(names)}")
    return default


def cups_media_problem(printer: str) -> tuple[str | None, str | None]:
    """(имя размера 4x6, None) или (None, текст, что исправить)."""
    media = find_4x6(_run(["lpoptions", "-p", printer, "-l"]))
    if media:
        return media, None
    return None, (f"у принтера «{printer}» в драйвере нет размера 4x6. Установите официальный "
                  f"драйвер DNP для macOS и добавьте принтер заново.")


def print_cups(printer: str, path: Path) -> None:
    media, problem = cups_media_problem(printer)
    if problem:
        raise RuntimeError(problem)
    # print-scaling=fill растягивает на весь лист без полей; пропорции карточки и
    # листа 4x6 совпадают, поэтому ничего не обрезается.
    cmd = ["lp", "-d", printer, "-o", f"PageSize={media}", "-o", "print-scaling=fill", str(path)]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if res.returncode != 0:
        raise RuntimeError(f"lp не принял задание: {(res.stderr or res.stdout).strip()}")


# ---------------- очередь на сервере ----------------

def fetch_jobs() -> list[dict]:
    r = requests.get(f"{BASE}/api/print-jobs", params={"key": KEY}, timeout=15)
    if r.status_code == 404:
        raise SystemExit("Сервер не принял ключ. Проверьте PRINT_QUEUE_KEY в settings.txt.")
    r.raise_for_status()
    return r.json().get("jobs", [])


def mark_done(card_id: str) -> None:
    r = requests.post(f"{BASE}/api/print-jobs/done", data={"card_id": card_id, "key": KEY}, timeout=15)
    r.raise_for_status()


def handle(job: dict, printer: str) -> None:
    card = job["card_id"]
    DOWNLOADS.mkdir(exist_ok=True)
    dest = DOWNLOADS / card
    if not dest.exists():
        r = requests.get(job["url"], timeout=60)
        r.raise_for_status()
        dest.write_bytes(r.content)
        log(f"скачано {card} ({len(r.content) // 1024} КБ)")

    if DRY_RUN:
        log(f"пробный режим: печать пропущена для {card}")
    elif IS_WINDOWS:
        print_windows(printer, dest)
        log(f"отправлено на принтер: {card}")
    else:
        print_cups(printer, dest)
        log(f"отправлено на принтер: {card}")

    # Подтверждаем ТОЛЬКО после успешной печати: иначе задание остаётся в очереди
    # и повторится на следующем круге.
    mark_done(card)
    _reported.discard(card)
    log(f"задание закрыто: {card}")


def self_check(printer: str) -> None:
    log(f"агент запущен · сайт {BASE}{' · ПРОБНЫЙ РЕЖИМ, без печати' if DRY_RUN else ''}")
    if not IS_WINDOWS:
        media, problem = cups_media_problem(printer)
        log(f"принтер «{printer}» · размер бумаги {media or 'не найден'}")
        log(f"ВНИМАНИЕ: {problem}" if problem else "размер бумаги подходит под карточку 10x15 см")
    else:
        pw, ph, dx, dy = _caps(printer, PHYSICALWIDTH, PHYSICALHEIGHT, LOGPIXELSX, LOGPIXELSY)
        log(f"принтер «{printer}» · лист {round(pw / dx * 25.4)}x{round(ph / dy * 25.4)} мм · {dx} dpi")
        problem = sheet_problem(pw, ph, dx, dy)
        log(f"ВНИМАНИЕ: {problem}" if problem else
            "лист 4x6 подходит: карточка ляжет по центру ровно 10x15 см, запас под обрез останется белым")
    # Нет сети на старте — не повод падать: раньше exe закрывался с ошибкой, и
    # оператор видел только мигнувшее окно. Основной цикл сам дождётся связи.
    try:
        fetch_jobs()
    except SystemExit:
        raise
    except Exception as exc:                        # noqa: BLE001
        log(f"сеть пока недоступна ({type(exc).__name__}), жду связи и карточки…")
        return
    log("связь с сервером и ключ в порядке, жду карточки…")


def main() -> None:
    if not KEY:
        raise SystemExit("Не задан PRINT_QUEUE_KEY — впишите его в settings.txt рядом с программой.")
    printer = pick_printer() if IS_WINDOWS else pick_cups_printer()
    self_check(printer)
    while True:
        try:
            for job in fetch_jobs():
                try:
                    handle(job, printer)
                except Exception as exc:            # noqa: BLE001
                    # Задание не подтверждено и вернётся на следующем круге.
                    # Одну и ту же ошибку по карточке пишем один раз, без спама.
                    cid = job.get("card_id")
                    if cid not in _reported:
                        log(f"ОШИБКА по {cid}: {exc}")
                        _reported.add(cid)
        except SystemExit:
            raise
        except Exception as exc:                    # noqa: BLE001
            log(f"сеть недоступна: {type(exc).__name__}: {exc}")
        time.sleep(POLL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("остановлен")
    except SystemExit as exc:
        log(str(exc))
        if getattr(sys, "frozen", False):
            input("Нажмите Enter, чтобы закрыть окно…")
        sys.exit(1)
    except Exception as exc:                        # noqa: BLE001
        log(f"программа остановилась из-за ошибки: {type(exc).__name__}: {exc}")
        if getattr(sys, "frozen", False):
            input("Пришлите agent.log. Нажмите Enter, чтобы закрыть окно…")
        sys.exit(1)

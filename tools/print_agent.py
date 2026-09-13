"""Агент печати для ноутбука у принтера DNP QW410.

Сервер стоит в другой стране и печатать сам не может. Гость нажимает
«Отправить в печать», карточка встаёт в очередь на сервере, а этот агент на
ноутбуке у принтера забирает её по HTTPS и печатает на месте.

Печать идёт напрямую через Windows (GDI), без программы просмотра картинок:
изображение ложится ровно на лист, выбранный в драйвере. Карточка с сервера
1200x1800 — это 4x6" при 300 dpi, родной размер DNP QW410. Если в драйвере
выбран другой размер бумаги, агент не печатает и пишет, что исправить, — так
рулон не уходит впустую.

Настройка — файл settings.txt рядом с программой (строки КЛЮЧ=ЗНАЧЕНИЕ) или
переменные окружения с теми же именами:
    PRINT_QUEUE_KEY  ключ доступа к очереди (обязателен)
    BASE_URL         адрес сайта, по умолчанию https://remtech-forum.ru
    PRINTER_NAME     имя принтера; пусто — первый, в имени которого есть QW410 или DNP
    POLL_SECONDS     как часто спрашивать сервер, по умолчанию 3
    DRY_RUN=1        скачивать и подтверждать, но НЕ печатать (проверка связки)

Запуск из исходников: pip install requests pillow pywin32; python print_agent.py
Сборка в один exe:    pyinstaller --onefile --name RemtehnikaPrint print_agent.py
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
TOLERANCE = 0.03          # допустимое расхождение пропорций карточки и листа

# Коды GetDeviceCaps
LOGPIXELSX, LOGPIXELSY = 88, 90
PHYSICALWIDTH, PHYSICALHEIGHT, PHYSICALOFFSETX, PHYSICALOFFSETY = 110, 111, 112, 113

_reported: set[str] = set()   # по каким карточкам ошибку уже показали


def log(msg: str) -> None:
    line = f"[{time.strftime('%d.%m %H:%M:%S')}] {msg}"
    print(line, flush=True)
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


def paper_problem(printer: str, size: tuple[int, int]) -> str | None:
    """None — лист подходит под карточку; иначе текст, что исправить в драйвере."""
    pw, ph, dx, dy = _caps(printer, PHYSICALWIDTH, PHYSICALHEIGHT, LOGPIXELSX, LOGPIXELSY)
    iw, ih = size
    if (iw > ih) != (pw > ph):
        iw, ih = ih, iw                      # карточку повернём под ориентацию листа
    if abs(pw / ph - iw / ih) / (iw / ih) > TOLERANCE:
        mm_w, mm_h = round(pw / dx * 25.4), round(ph / dy * 25.4)
        return (f"в драйвере выбран лист {mm_w}x{mm_h} мм, а карточка рассчитана на 10x15 см. "
                f"Откройте «Настройки печати» принтера и выберите размер 4x6.")
    return None


def print_windows(printer: str, path: Path, output_file: str | None = None) -> None:
    """Печать карточки на весь лист. output_file — только для проверки без
    бумаги: виртуальный PDF-принтер пишет в этот файл, не спрашивая имя."""
    import win32ui
    from PIL import ImageWin

    img = Image.open(path).convert("RGB")
    problem = paper_problem(printer, img.size)
    if problem:
        raise RuntimeError(problem)
    pw, ph, ox, oy = _caps(printer, PHYSICALWIDTH, PHYSICALHEIGHT, PHYSICALOFFSETX, PHYSICALOFFSETY)
    if (img.width > img.height) != (pw > ph):
        img = img.rotate(90, expand=True)

    dc = win32ui.CreateDC()
    dc.CreatePrinterDC(printer)
    try:
        if output_file:
            dc.StartDoc(path.name, output_file)
        else:
            dc.StartDoc(path.name)
        dc.StartPage()
        # Координаты устройства отсчитываются от печатной области; рисуем на весь
        # физический лист, чтобы у сублимационного принтера не было белых полей.
        ImageWin.Dib(img).draw(dc.GetHandleOutput(), (-ox, -oy, pw - ox, ph - oy))
        dc.EndPage()
        dc.EndDoc()
    finally:
        dc.DeleteDC()


def print_cups(path: Path) -> None:
    cmd = ["lp", "-o", "media=4x6", "-o", "fit-to-page"]
    if PRINTER:
        cmd += ["-d", PRINTER]
    subprocess.run(cmd + [str(path)], check=True, timeout=60)


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


def handle(job: dict, printer: str | None) -> None:
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
    elif printer is not None:
        print_windows(printer, dest)
        log(f"отправлено на принтер: {card}")
    else:
        print_cups(dest)
        log(f"отправлено на принтер: {card}")

    # Подтверждаем ТОЛЬКО после успешной печати: иначе задание остаётся в очереди
    # и повторится на следующем круге.
    mark_done(card)
    _reported.discard(card)
    log(f"задание закрыто: {card}")


def self_check(printer: str | None) -> None:
    log(f"агент запущен · сайт {BASE}{' · ПРОБНЫЙ РЕЖИМ, без печати' if DRY_RUN else ''}")
    if printer is not None:
        pw, ph, dx, dy = _caps(printer, PHYSICALWIDTH, PHYSICALHEIGHT, LOGPIXELSX, LOGPIXELSY)
        log(f"принтер «{printer}» · лист {round(pw / dx * 25.4)}x{round(ph / dy * 25.4)} мм · {dx} dpi")
        problem = paper_problem(printer, (1200, 1800))
        log(f"ВНИМАНИЕ: {problem}" if problem else "размер бумаги подходит под карточку 10x15 см")
    fetch_jobs()
    log("связь с сервером и ключ в порядке, жду карточки…")


def main() -> None:
    if not KEY:
        raise SystemExit("Не задан PRINT_QUEUE_KEY — впишите его в settings.txt рядом с программой.")
    printer = pick_printer() if platform.system() == "Windows" else None
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

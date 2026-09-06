"""Агент печати для компьютера у киоска.

Забирает задания с сервера по HTTPS, скачивает карточку и отправляет её на
принтер локальным драйвером. Доступ к серверу по SSH не нужен — только ключ.

Запуск:
    python print_agent.py

Настройка — переменные окружения (или правка констант ниже):
    PRINT_QUEUE_KEY  ключ доступа к очереди (обязателен)
    PRINTER_NAME     имя принтера как в системе; пусто — принтер по умолчанию
    POLL_SECONDS     как часто спрашивать сервер, по умолчанию 3
    DRY_RUN=1        скачивать и подтверждать, но НЕ печатать (для проверки связки)

Зависимости: pip install requests
              на Windows дополнительно: pip install pywin32
"""
from __future__ import annotations

import os
import platform
import subprocess
import sys
import time
from pathlib import Path

import requests

BASE = os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/")
KEY = os.environ.get("PRINT_QUEUE_KEY", "")
PRINTER = os.environ.get("PRINTER_NAME", "")          # пусто = принтер по умолчанию
POLL = int(os.environ.get("POLL_SECONDS", "3"))
DRY_RUN = os.environ.get("DRY_RUN", "") in ("1", "true", "yes")

DOWNLOADS = Path(__file__).with_name("printed")        # куда складывать скачанное
DOWNLOADS.mkdir(exist_ok=True)


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def send_to_printer(path: Path) -> None:
    """Отправляет файл на принтер средствами операционной системы.

    Печать идёт через штатный драйвер Mitsubishi, поэтому размер бумаги и
    цветовой профиль берутся из настроек принтера — их достаточно один раз
    выставить в свойствах принтера (карточка в пропорции 3:4, под 15x20 см)."""
    system = platform.system()

    if system == "Windows":
        # ShellExecute с глаголом print — тот же путь, что «Печать» в контекстном
        # меню файла: используется драйвер и текущие настройки принтера.
        import win32api  # из пакета pywin32
        win32api.ShellExecute(0, "print", str(path), f'"{PRINTER}"' if PRINTER else None, ".", 0)
        return

    # macOS и Linux: CUPS
    cmd = ["lp"]
    if PRINTER:
        cmd += ["-d", PRINTER]
    cmd += ["-o", "fit-to-page", str(path)]
    subprocess.run(cmd, check=True, timeout=60)


def fetch_jobs() -> list[dict]:
    r = requests.get(f"{BASE}/api/print-jobs", params={"key": KEY}, timeout=15)
    if r.status_code == 404:
        raise SystemExit("Сервер не принял ключ (404). Проверьте PRINT_QUEUE_KEY.")
    r.raise_for_status()
    return r.json().get("jobs", [])


def mark_done(card_id: str) -> None:
    r = requests.post(f"{BASE}/api/print-jobs/done",
                      data={"card_id": card_id, "key": KEY}, timeout=15)
    r.raise_for_status()


def handle(job: dict) -> None:
    card = job["card_id"]
    dest = DOWNLOADS / card

    if not dest.exists():
        r = requests.get(job["url"], timeout=60)
        r.raise_for_status()
        dest.write_bytes(r.content)
        log(f"скачано {card} ({len(r.content) // 1024} КБ)")

    if DRY_RUN:
        log(f"DRY_RUN: печать пропущена для {card}")
    else:
        send_to_printer(dest)
        log(f"отправлено на принтер: {card}")

    # Подтверждаем ТОЛЬКО после успешной отправки: если печать упала, задание
    # останется в очереди и повторится на следующем круге.
    mark_done(card)
    log(f"задание закрыто: {card}")


def main() -> None:
    if not KEY:
        raise SystemExit("Не задан PRINT_QUEUE_KEY — ключ доступа к очереди.")
    log(f"агент запущен · сервер {BASE} · принтер "
        f"{PRINTER or 'по умолчанию'}{' · DRY_RUN' if DRY_RUN else ''}")

    while True:
        try:
            for job in fetch_jobs():
                try:
                    handle(job)
                except Exception as exc:            # noqa: BLE001
                    # Одно неудачное задание не должно останавливать очередь:
                    # оно не подтверждено и вернётся на следующем круге.
                    log(f"ОШИБКА по {job.get('card_id')}: {type(exc).__name__}: {exc}")
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
        sys.exit(0)

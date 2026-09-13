#!/bin/bash
# Запуск программы печати «Ремтехника» на Mac.
# Первый запуск ставит нужные библиотеки в папку .venv рядом, дальше запуск мгновенный.

cd "$(dirname "$0")" || exit 1

pause_and_exit() {
  read -r -p "Нажмите Enter, чтобы закрыть окно…"
  exit 1
}

if ! command -v python3 >/dev/null 2>&1; then
  echo "Не найден Python 3. Установите его с python.org (Downloads → macOS) и запустите снова."
  pause_and_exit
fi

if [ ! -x .venv/bin/python ]; then
  echo "Первый запуск: готовлю окружение, это займёт около минуты…"
  python3 -m venv .venv || { echo "Не удалось создать окружение Python."; pause_and_exit; }
  .venv/bin/python -m pip install --quiet --upgrade pip
  .venv/bin/python -m pip install --quiet requests pillow \
    || { echo "Не удалось установить библиотеки: проверьте интернет."; rm -rf .venv; pause_and_exit; }
fi

exec .venv/bin/python print_agent.py

@echo off
REM Запуск AI-фотоинсталляции «Ремтехника» на Windows.
REM Создаёт venv, ставит зависимости, копирует .env из примера и поднимает сервер.
setlocal
cd /d "%~dp0backend"

if not exist ".venv\Scripts\python.exe" (
  echo [setup] Создаю виртуальное окружение...
  py -m venv .venv || python -m venv .venv
)

call ".venv\Scripts\activate.bat"
echo [setup] Устанавливаю зависимости (первый раз — несколько минут)...
python -m pip install --upgrade pip -q
pip install -q -r requirements.txt

if not exist ".env" (
  copy ".env.example" ".env" >nul
  echo [setup] Создан .env из .env.example — впишите REPLICATE_API_TOKEN для реальной генерации.
)

echo.
echo   Откройте в браузере:  http://localhost:8000
echo   (без REPLICATE_API_TOKEN в backend\.env работает демо-режим — заглушки)
echo.
python -m uvicorn main:app --host 0.0.0.0 --port 8000
endlocal

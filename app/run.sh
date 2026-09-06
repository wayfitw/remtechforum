#!/bin/bash
# Запуск AI-фотоинсталляции «Ремтехника»
cd "$(dirname "$0")/backend"
[ -d .venv ] || python3 -m venv .venv
source .venv/bin/activate
pip install -q -r requirements.txt
echo ""
echo "  Открой в браузере:  http://localhost:8000"
echo "  (для генерации по-настоящему — впиши GEMINI_API_KEY в backend/.env)"
echo ""
uvicorn main:app --host 0.0.0.0 --port 8000

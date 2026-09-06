#!/usr/bin/env bash
# Установка AI-фотоинсталляции «Ремтехника» на Ubuntu/Debian VPS (AEZA и любой другой).
# Запуск от root:  bash deploy/install.sh
set -euo pipefail

APP_DIR=${APP_DIR:-/opt/remtech-photo}
REPO=${REPO:-https://github.com/wayfitw/remtech-photo.git}
SERVICE=remtech-photo

echo "==> Системные пакеты"
apt-get update -qq
# libgl1/libglib2.0-0 нужны opencv и insightface, иначе импорт падает
apt-get install -y -qq python3 python3-venv python3-pip git nginx \
    libgl1 libglib2.0-0 ffmpeg curl

echo "==> Код"
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" pull --ff-only
else
  git clone --depth 1 "$REPO" "$APP_DIR"
fi

echo "==> Python-окружение (первый раз — несколько минут: onnxruntime/insightface)"
cd "$APP_DIR/app/backend"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install --upgrade pip -q
.venv/bin/pip install -q -r requirements.txt

if [ ! -f .env ]; then
  cp .env.example .env
  echo "!!! Создан .env — впишите REPLICATE_API_TOKEN и PUBLIC_BASE_URL"
fi

echo "==> systemd"
sed "s#__APP_DIR__#$APP_DIR#g" "$APP_DIR/deploy/$SERVICE.service" > "/etc/systemd/system/$SERVICE.service"
systemctl daemon-reload
systemctl enable --now "$SERVICE"
sleep 3
systemctl --no-pager status "$SERVICE" || true

echo
echo "Готово. Локальная проверка:  curl -s localhost:8000/api/health"
echo "Дальше: настройте nginx (deploy/nginx.conf) и ОБЯЗАТЕЛЬНО HTTPS —"
echo "без https веб-камера в браузере работать не будет."

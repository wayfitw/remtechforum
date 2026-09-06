#!/usr/bin/env bash
# Развёртывание AI-фотоинсталляции «Ремтехника» на чистом Ubuntu VPS (AEZA и т.п.).
# Запуск от root:   bash deploy.sh
# Повторный запуск безопасен — обновляет код и перезапускает сервис.
set -euo pipefail

REPO="https://github.com/wayfitw/remtech-photo.git"
DIR="/opt/remtech-photo"
PORT=8000

echo "==> 1/7 Системные пакеты"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
  python3 python3-venv python3-pip git nginx \
  libgl1 libglib2.0-0 ca-certificates curl

echo "==> 2/7 Swap (критично при 2 GB RAM: insightface грузит модели в память)"
if ! swapon --show | grep -q .; then
  fallocate -l 4G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile >/dev/null
  swapon /swapfile
  grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  echo "    создан swap 4 GB"
else
  echo "    swap уже настроен"
fi

echo "==> 3/7 Код"
if [ -d "$DIR/.git" ]; then
  git -C "$DIR" pull --ff-only
else
  git clone --depth 1 "$REPO" "$DIR"
fi

echo "==> 4/7 Python-окружение (первый раз — несколько минут)"
cd "$DIR/app/backend"
[ -d .venv ] || python3 -m venv .venv
./.venv/bin/pip install -q --upgrade pip
./.venv/bin/pip install -q -r requirements.txt

echo "==> 5/7 Конфигурация"
if [ ! -f .env ]; then
  cp .env.example .env
  cat >> .env <<'EOF'

# --- добавлено deploy.sh ---
NANO_BANANA_MODEL=google/nano-banana-pro
GEN_MODE=edit
FACE_SWAP=1
FACE_ENHANCE=1
FACE_DESHADOW=1
# на 2 GB RAM грузим только нужные модели insightface и уменьшаем вход детектора
FACE_MODULES=detection,recognition,landmark_3d_68
FACE_DET_SIZE=512
# порог размера лица: 512 — качество, 280 — терпимо для вебки
FACE_MIN_PX=280
EOF
  echo "    создан .env — ВПИШИТЕ REPLICATE_API_TOKEN и PUBLIC_BASE_URL!"
else
  echo "    .env уже существует — не трогаю"
fi

echo "==> 6/7 systemd-сервис"
cat > /etc/systemd/system/remtech-photo.service <<EOF
[Unit]
Description=Remtehnika photo installation (FastAPI)
After=network.target

[Service]
WorkingDirectory=$DIR/app/backend
ExecStart=$DIR/app/backend/.venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port $PORT
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now remtech-photo >/dev/null 2>&1 || systemctl restart remtech-photo

echo "==> 7/7 nginx (порт 80 → приложение)"
cat > /etc/nginx/sites-available/remtech-photo <<EOF
server {
    listen 80 default_server;
    client_max_body_size 25m;
    location / {
        proxy_pass http://127.0.0.1:$PORT;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        # генерация идёт 3-5 минут — таймауты должны быть длинными
        proxy_read_timeout 600s;
        proxy_send_timeout 600s;
    }
}
EOF
ln -sf /etc/nginx/sites-available/remtech-photo /etc/nginx/sites-enabled/remtech-photo
rm -f /etc/nginx/sites-enabled/default
nginx -t >/dev/null && systemctl reload nginx

IP=$(curl -s --max-time 5 ifconfig.me || echo "<IP сервера>")
echo
echo "=================================================="
echo " ГОТОВО.  Откройте:  http://$IP"
echo
echo " Осталось вписать ключ:"
echo "   nano $DIR/app/backend/.env       # REPLICATE_API_TOKEN=... и PUBLIC_BASE_URL=http://$IP"
echo "   systemctl restart remtech-photo"
echo
echo " Логи:      journalctl -u remtech-photo -f"
echo " Статус:    systemctl status remtech-photo"
echo " Обновить:  bash $DIR/app/deploy.sh"
echo "=================================================="

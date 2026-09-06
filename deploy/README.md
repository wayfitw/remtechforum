# Развёртывание на VPS (AEZA)

Веб-приложение (генерация + карточки + QR) разворачивается в облаке; камера и
принтер остаются на физическом стенде.

## Требования
- Ubuntu 22.04/24.04, 2+ CPU, **4 ГБ RAM** (insightface/onnxruntime), 10 ГБ диска.
- GPU не нужен: генерация идёт через Replicate API, локально считается только
  метрика лица (ArcFace на CPU).
- Домен, направленный A-записью на IP сервера — **обязателен** (см. HTTPS ниже).

## 1. Установка

```bash
ssh root@IP_СЕРВЕРА
apt-get update && apt-get install -y git
git clone https://github.com/wayfitw/remtech-photo.git /opt/remtech-photo
bash /opt/remtech-photo/deploy/install.sh
```

Скрипт ставит системные пакеты, создаёт venv, ставит зависимости, поднимает
systemd-сервис `remtech-photo` на `127.0.0.1:8000`.

## 2. Ключи

```bash
nano /opt/remtech-photo/app/backend/.env
```

| Параметр | Значение |
|---|---|
| `REPLICATE_API_TOKEN` | токен с replicate.com — **без него демо-режим (заглушки)** |
| `PUBLIC_BASE_URL` | `https://ваш-домен` — иначе QR будет вести на localhost |
| `PRINT_ENABLED` | `0` на сервере (принтер стоит на стенде, не здесь) |

После правки: `systemctl restart remtech-photo`

## 3. nginx + HTTPS

```bash
cp /opt/remtech-photo/deploy/nginx.conf /etc/nginx/sites-available/remtech-photo
nano /etc/nginx/sites-available/remtech-photo      # заменить server_name на домен
ln -s /etc/nginx/sites-available/remtech-photo /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx

apt-get install -y certbot python3-certbot-nginx
certbot --nginx -d ваш-домен
```

> ⚠️ **HTTPS обязателен.** Браузеры дают доступ к веб-камере (`getUserMedia`)
> только на `https://` или `localhost`. По голому `http://IP` съёмка с камеры
> работать не будет — гость увидит «камера недоступна».

## 4. Фоновое видео (опционально)

`app/frontend/media/intro.mp4` не хранится в git (большой файл). Если нужен
зацикленный фон — скопировать вручную:

```bash
scp intro.mp4 root@IP:/opt/remtech-photo/app/frontend/media/
```

Без файла сайт работает, фон — постер/тёмный.

## 5. Проверка

```bash
curl -s localhost:8000/api/health          # {"ok":true,...}
systemctl status remtech-photo
journalctl -u remtech-photo -f           # логи генерации
```

Открыть `https://ваш-домен` — должен появиться экран «Я в деле».

## Обновление

```bash
cd /opt/remtech-photo && git pull
app/backend/.venv/bin/pip install -q -r app/backend/requirements.txt
systemctl restart remtech-photo
```

## Замечания по эксплуатации
- **Генерация платная** (Replicate) — следите за балансом, иначе `/api/generate`
  начнёт отдавать ошибки.
- Одна генерация занимает 2–5 минут (3 варианта), поэтому в nginx выставлены
  таймауты 600 с. Не уменьшайте их.
- Результаты копятся в `app/backend/assets/output/` — периодически чистить.
- Печать (`lpr`/CUPS) работает только там, где физически подключён принтер.

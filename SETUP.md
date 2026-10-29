# Развёртывание на сервере

Сайт работает полностью автономно на Python (`FastAPI` + `uvicorn` + `SQLite`) без внешних облачных воркеров.

## 1. Установка зависимостей

Предполагается каталог `/opt/krafen` (путь совпадает с `krafen.service`):

```bash
sudo mkdir -p /opt/krafen
sudo chown "$USER:$USER" /opt/krafen
git clone https://github.com/krafenx/krafenz.git /opt/krafen
cd /opt/krafen

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Если используется запись эфиров для `/danonsha`, установите зависимости рекордера и `ffmpeg`:

```bash
sudo apt update && sudo apt install -y ffmpeg
.venv/bin/pip install -r recorder/requirements.txt
```

## 2. Настройка `.env`

Скопируйте шаблон и ограничьте права чтения файла:

```bash
cp .env.example .env
chmod 600 .env
```

Основные переменные:

- `HOST=127.0.0.1` и `PORT=8080` (при работе за reverse-proxy `nginx` или `Caddy`, либо `0.0.0.0` для прямого доступа)
- `PUBLIC_ORIGIN=https://krafen.me` — публичный адрес сайта (используется для проверки Origin и OAuth redirect Spotify)
- `ADMIN_PASSWORD` и случайный `SECRET_KEY` (`python3 -c "import secrets; print(secrets.token_hex(32))"`, если не задан — генерируется автоматически и сохраняется в `data/auth.db`) — для входа в админ-режим коллекций
- `TMDB_ACCESS_TOKEN` (или `TMDB_API_KEY`) — поиск фильмов и сериалов в `/watchlist`
- `TWITCH_CLIENT_ID` и `TWITCH_CLIENT_SECRET` — статус Twitch на главной, `/danonsha` и поиск игр в IGDB (`/gamelist`)
- `SPOTIFY_CLIENT_ID` и `SPOTIFY_CLIENT_SECRET` — текущий трек со шкалой прогресса (см. [`SPOTIFY_SETUP.md`](SPOTIFY_SETUP.md))
- `LASTFM_API_KEY` и `LASTFM_USER` — резервный источник скробблов, если Spotify не подключён

## 3. Запуск через `systemd`

Создайте системного пользователя и выдайте доступ к каталогам данных:

```bash
sudo useradd --system --home /opt/krafen --shell /usr/sbin/nologin krafen || true
mkdir -p /opt/krafen/data /opt/krafen/vods
sudo chown -R krafen:krafen /opt/krafen/data /opt/krafen/vods
sudo chown root:krafen /opt/krafen/.env
sudo chmod 640 /opt/krafen/.env
```

Подключите unit-файл (отредактируйте `ExecStart` на `/opt/krafen/.venv/bin/python /opt/krafen/server.py`, если используете виртуальное окружение):

```bash
sudo cp /opt/krafen/krafen.service /etc/systemd/system/krafen.service
sudo systemctl daemon-reload
sudo systemctl enable --now krafen.service
sudo systemctl status krafen.service
```

Проверка здоровья сервера:

```bash
curl -fsS http://127.0.0.1:8080/api/health
```

## 4. Пример конфигурации Caddy / Nginx

### Caddy (`Caddyfile`)

```caddyfile
krafen.me {
    reverse_proxy 127.0.0.1:8080
}
```

### Nginx

```nginx
server {
    listen 443 ssl http2;
    server_name krafen.me;

    # ssl_certificate / ssl_certificate_key ...

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;
    }
}
```

`proxy_buffering off;` важен для мгновенной перемотки видеофайлов `/vods/*` через HTTP Range.

# krafen.me

Личный сайт, коллекции и архив трансляций на едином сервере **Python FastAPI + SQLite**.

## Архитектура

- **`server.py`** — FastAPI-приложение, отдающее все страницы с динамическим CSP-nonce, статические ассеты, REST API `/api/*`, OAuth-авторизацию Spotify и потоковую раздачу записей стримов с поддержкой HTTP Range.
- **`data/krafen.db`** — локальная база данных SQLite (WAL) для коллекций (`watchlist`, `gamelist`, `setup`, `wishlist`), токенов и метаданных. Существующие JSON-файлы в `data/*.json` автоматически мигрируются при первом запуске.
- **`style.css` + `app.js`** — единая дизайн-система и клиентский рантайм в эстетике `danonsha` (тёмный обсидиан, багрянец, серебро, холст дождя, туман, молнии, плавная прокрутка и нижний док).
- **`recorder/recorder.py`** — фоновый рекордер эфиров и IRC-чата Twitch в каталог `vods/` для страницы `/danonsha`.

## Страницы

| Маршрут | Файл | Описание |
| --- | --- | --- |
| `/` | `index.html` | Главная страница: музыка Spotify/Last.fm, часы (МСК), Twitch, Discord, соцсети и реквизиты |
| `/watchlist` | `watchlist.html` | Коллекция аниме, фильмов и сериалов (поиск через TMDB и Shikimori) |
| `/gamelist` | `gamelist.html` | Игры с оценками 0–10 и отзывами (поиск через IGDB) |
| `/setup` | `setup.html` | Конфигурация системного блока и периферии |
| `/wishlist` | `wishlist.html` | Список желаемых покупок с приоритетами и суммами по валютам |
| `/tasks` | `tasks.html` | Калькулятор ежедневных заданий BP и таймеры клубных квестов |
| `/danonsha` | `danonsha.html` | Страница danonsha: статус эфира, архив VOD с синхронизацией чата |

## Быстрый старт

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
# Заполните переменные окружения в .env
.venv/bin/python server.py
```

По умолчанию сервер поднимается на `http://0.0.0.0:8080`. Подробное руководство по продакшен-развёртыванию через `systemd` и `nginx`/`Caddy` — в [`SETUP.md`](SETUP.md), настройка интеграции со Spotify — в [`SPOTIFY_SETUP.md`](SPOTIFY_SETUP.md).

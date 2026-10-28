# danonsha-archive — установка

Архитектура: запись идёт на диск ПК → сразу после стрима улетает в Google
Drive (5 ТБ) → локальная копия удаляется → сайт показывает записи прямо из
Drive через монтирование. На ПК занято место только на время одного стрима.

## 1. Установка

```bash
pip install -r recorder/requirements.txt
```

- **Python 3.10+** — https://python.org
- **streamlink** — `pip install streamlink` (или скачать с streamlink.github.io)
- **rclone** — https://rclone.org/downloads (распаковать, добавить в PATH)
- **WinFsp** (только Windows, нужен для монтирования) — https://winfsp.dev

## 2. Привязка Google Drive к rclone

```bash
rclone config
# n) New remote → имя: gdrive
# Storage: drive (Google Drive)
# Оставить client_id пустым (Enter)
# scope: 1 (Full access) — или 2 (только чтение файлов, которыми поделились)
# root_folder_id: Enter (пусто)
# service_account: Enter (пусто)
# Edit advanced config: n
# Use web browser to automatically authenticate: y
# → откроется браузер, залогинься своим Google-аккаунтом
```

Проверка: `rclone lsd gdrive:` — должно показать папки твоего Drive.

Создай папку для записей: `rclone mkdir gdrive:vods`

## 2.5 Telegram-канал (опционально, запись дублируется туда)

1. **BotFather** → `/newbot` → получи токен вида `123456:ABC...`
2. **my.telegram.org** → API Development Tools → создай приложение → получи
   `api_id` (число) и `api_hash`
3. Добавь бота в канал **как администратора** (с правом постить)
4. Узнай id канала: перешли любой пост из канала боту `@getmyid_bot` —
   придёт отрицательное число вида `-100xxxxxxxxxx`

## 3. Запуск (всегда в этом порядке)

Терминал 1 — монтирование Drive как папки `vods/` (сайт читает отсюда):

```bash
# Windows (winfsp должен быть установлен)
rclone mount gdrive:vods vods --vfs-cache-mode minimal --network-mode

# Linux/macOS
rclone mount gdrive:vods vods --vfs-cache-mode minimal --daemon
```

Терминал 2 — сервер + автозапись:

```bash
export TWITCH_CHANNEL=danonsha
export TWITCH_CLIENT_ID=xxx        # dev.twitch.tv/console
export TWITCH_CLIENT_SECRET=xxx
export RCLONE_REMOTE=gdrive:vods   # ← после стрима запись улетает в Drive
export MIN_FREE_GB=10              # удалять старое, если свободно < 10 ГБ
# Telegram-дубль (опционально):
export TELEGRAM_API_ID=123456
export TELEGRAM_API_HASH=xxxxxxxx
export TELEGRAM_BOT_TOKEN=123456:ABC...
export TELEGRAM_CHAT_ID=-100xxxxxxxxxx
RECORDER=1 python3 server.py
```

Открыть http://localhost:8080

## 4. Автозапуск в фоне (опционально)

**Windows** — Планировщик задач, две задачи «При входе в систему»:

1. `rclone mount gdrive:vods "C:\путь\krafen\vods" --vfs-cache-mode minimal --network-mode`
2. `pythonw.exe C:\путь\krafen\server.py` (переменные задать в начале скрипта
   или через `setx`)

**Linux** — два systemd-юнита: `rclone mount` (зависимость winfsp не нужна)
и `server.py`. Могу написать по запросу.

## 5. Как это работает

1. Канал вышел в эфир → пишется `video.mp4` + `chat.jsonl` локально в
   `vods/<дата>_<название>/`
2. Стрим кончился → сначала в канал Telegram (пост с описанием, видео —
   если больше ~1.9 ГБ, разбивается на части через ffmpeg, + чат и meta
   файлами), затем `rclone move` переносит папку в `gdrive:vods`, локальная
   копия удаляется; если места на диске < MIN_FREE_GB — стираются самые
   старые локальные записи
3. Сайт читает `vods/` — это смонтированный Drive, поэтому видит всё,
   что когда-либо записано
4. Молнии, дождь, чат в плеере — как было

## Частые проблемы

- **«rclone не найден»** — не добавлен в PATH; пропиши полный путь в RCLONE_BIN
- **Монтирование падает на Windows** — не установлен WinFsp
- **Загрузка в Drive не стартует** — проверь `rclone lsd gdrive:`; при ошибке
  `rate limit` подожди, rclone сам ретраит
- **Сайт показывает «Архив пуст»** — проверь, что монтирование работает:
  в папке `vods/` должны быть папки записей

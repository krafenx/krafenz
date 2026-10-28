#!/usr/bin/env python3
"""
Автозапись стримов Twitch с записью чата.

Как работает:
  1. Каждые POLL_INTERVAL секунд проверяет через Twitch Helix API, в эфире ли канал.
  2. Как только стрим начался — запускает streamlink для записи видео
     и параллельно подключается к Twitch IRC (анонимно, без OAuth) и пишет
     чат в JSONL с метками времени от начала записи.
  3. Когда стрим заканчивается — финализирует meta.json (название, игра,
     длительность, файлы). Дальше эти файлы можно отдать на страницу архива.

Запуск:
  export TWITCH_CHANNEL=danonsha
  export TWITCH_CLIENT_ID=...        # dev.twitch.tv/console, app registration
  export TWITCH_CLIENT_SECRET=...
  export RECORD_DIR=/var/vods        # куда складывать записи
  python3 recorder.py

Ротация/постоянство: держите процесс под systemd/supervisor или в tmux.
Для чистоты сессии запускайте одну копию на канал.
"""

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import websocket

# ----------------------------- конфиг (env) -----------------------------

CHANNEL = os.environ.get("TWITCH_CHANNEL", "danonsha")
CLIENT_ID = os.environ.get("TWITCH_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("TWITCH_CLIENT_SECRET", "")
RECORD_DIR = Path(os.environ.get("RECORD_DIR", "./vods"))
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "60"))

# --- облако и ротация ---
# RCLONE_REMOTE="gdrive:vods" — после завершения записи папка улетает в облако
# (rclone move), локальная копия удаляется. ПК держит только запись в процессе.
RCLONE_REMOTE = os.environ.get("RCLONE_REMOTE", "")
RCLONE_BIN = os.environ.get("RCLONE_BIN", "rclone")
# Сколько ГБ должно остаться свободно на диске: перед стартом записи старые
# папки удаляются, пока места не станет достаточно (работает и без rclone).
MIN_FREE_GB = float(os.environ.get("MIN_FREE_GB", "10"))

# --- telegram ---
# Запись (видео + чат + meta) после стрима улетает в канал.
# Нужны: bot-токен (BotFather), api_id/api_hash (my.telegram.org),
# id канала вида -100xxxxxxxxxx или @имя_канала. Бот должен быть админом канала.
TG_API_ID = os.environ.get("TELEGRAM_API_ID", "")
TG_API_HASH = os.environ.get("TELEGRAM_API_HASH", "")
TG_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
TG_SIZE_LIMIT = 1.9 * 1024 ** 3  # лимит одного файла в Telegram (с запасом)

# --hls-live-restart: при подключении к идущему стриму начать запись
# с начала стрима, а не с текущего момента (для полного VOD-архива).
EXTRA_STREAMLINK_ARGS = os.environ.get("STREAMLINK_ARGS", "--hls-live-restart").split()

IRC_URI = "wss://irc-ws.chat.twitch.tv:443"
CHAT_FLUSH_EVERY = 50  # сообщений до принудительного сброса буфера на диск

# ----------------------------- утилиты ---------------------------------


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def slugify(text: str) -> str:
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE)
    text = re.sub(r"[\s_]+", "-", text.strip())
    return (text or "stream")[:60].strip("-") or "stream"


# ------------------------- Twitch Helix API -----------------------------

_token_cache: dict = {}


def app_access_token() -> str:
    if _token_cache.get("token") and _token_cache["expires"] > time.time() + 60:
        return _token_cache["token"]
    r = requests.post(
        "https://id.twitch.tv/oauth2/token",
        data={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": "client_credentials",
        },
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    _token_cache["token"] = data["access_token"]
    _token_cache["expires"] = time.time() + data["expires_in"]
    return _token_cache["token"]


def get_live_stream() -> dict | None:
    """Вернуть dict стрима из Helix или None если офлайн/ошибка."""
    try:
        r = requests.get(
            "https://api.twitch.tv/helix/streams",
            params={"user_login": CHANNEL},
            headers={
                "Client-ID": CLIENT_ID,
                "Authorization": f"Bearer {app_access_token()}",
            },
            timeout=15,
        )
        r.raise_for_status()
        data = r.json().get("data", [])
        return data[0] if data else None
    except Exception as exc:  # noqa: BLE001 - сеть/токен не должны ронять демон
        log(f"Helix error: {exc}")
        return None


# ----------------------------- чат --------------------------------------


class ChatRecorder(threading.Thread):
    """
    Анонимное подключение к Twitch IRC. Пишет PRIVMSG в JSONL:
      {"t": <секунды от старта записи, float>, "user": "...", "msg": "..."}
    """

    def __init__(self, channel: str, out_file: Path):
        super().__init__(daemon=True)
        self.channel = channel.lower()
        self.out_file = out_file
        self.start_ts = time.time()
        self._stop = threading.Event()
        self._buffer: list[str] = []

    def stop(self) -> None:
        self._stop.set()

    def _offset(self) -> float:
        return round(time.time() - self.start_ts, 3)

    def _flush(self) -> None:
        if not self._buffer:
            return
        with self.out_file.open("a", encoding="utf-8") as fh:
            fh.write("".join(self._buffer))
        self._buffer.clear()

    def _handle(self, raw: str) -> None:
        # PING/PONG — иначе Twitch отключит
        if raw.startswith("PING"):
            self.ws.send("PONG :tmi.twitch.tv")
            return
        m = re.match(
            r"^:(\w+)!.*PRIVMSG #\w+ :(.*)$", raw, flags=re.IGNORECASE | re.DOTALL
        )
        if not m:
            return
        user, msg = m.group(1), m.group(2).rstrip("\r\n")
        self._buffer.append(
            json.dumps({"t": self._offset(), "user": user, "msg": msg},
                       ensure_ascii=False)
            + "\n"
        )
        if len(self._buffer) >= CHAT_FLUSH_EVERY:
            self._flush()

    def run(self) -> None:
        backoff = 5
        while not self._stop.is_set():
            try:
                log("chat: подключение к IRC...")
                self.ws = websocket.create_connection(
                    IRC_URI, timeout=30,
                    header=["Origin: https://www.twitch.tv"],
                )
                # анонимный вход: без регистрации приложения, только чтение
                self.ws.send("PASS SCHMOOPIIE")
                self.ws.send("NICK justinfan%05d" % (int(time.time()) % 100000))
                self.ws.send(f"JOIN #{self.channel}")
                backoff = 5
                log("chat: в канале, пишу сообщения")
                while not self._stop.is_set():
                    try:
                        self._handle(self.ws.recv())
                    except websocket.WebSocketTimeoutException:
                        self.ws.send("PING :tmi.twitch.tv")
            except Exception as exc:  # noqa: BLE001 - переподключаемся всегда
                log(f"chat: {exc} — повтор через {backoff}с")
                time.sleep(backoff)
                backoff = min(backoff * 2, 120)
            finally:
                try:
                    self._flush()
                except Exception:
                    pass


# ----------------------------- запись -----------------------------------

def disk_free_gb() -> float:
    usage = os.statvfs(RECORD_DIR)
    return usage.f_bavail * usage.f_frsize / (1024 ** 3)


def rotate_old_recordings() -> None:
    """Удалять самые старые завершённые записи, пока свободно < MIN_FREE_GB."""
    while disk_free_gb() < MIN_FREE_GB:
        candidates = sorted(
            (p for p in RECORD_DIR.iterdir()
             if p.is_dir() and (p / "meta.json").exists()),
            key=lambda p: p.name,
        )
        if not candidates:
            log("rotate: удалять нечего, а места всё равно мало!")
            return
        victim = candidates[0]
        log(f"rotate: удаляю {victim.name} (свободно {disk_free_gb():.1f} ГБ)")
        shutil.rmtree(victim, ignore_errors=True)


def upload_to_cloud(folder: Path) -> bool:
    """rclone move папки записи в облако. True — успех, локальная копия снесена."""
    if not RCLONE_REMOTE:
        return False
    try:
        log(f"cloud: заливаю {folder.name} → {RCLONE_REMOTE} ...")
        res = subprocess.run(
            [RCLONE_BIN, "move", str(folder), f"{RCLONE_REMOTE}/{folder.name}",
             "--transfers", "4", "--checkers", "8", "--stats-one-line", "-v"],
            capture_output=True, text=True, timeout=None,
        )
        if res.returncode == 0:
            log(f"cloud: {folder.name} в облаке, локальная копия удалена")
            return True
        log(f"cloud: ошибка rclone: {res.stderr.strip()[-300:]}")
    except FileNotFoundError:
        log("cloud: rclone не найден в PATH — запись остаётся локально")
    except Exception as exc:  # noqa: BLE001
        log(f"cloud: {exc}")
    return False


def tg_configured() -> bool:
    return bool(TG_API_ID and TG_API_HASH and TG_BOT_TOKEN and TG_CHAT)


def split_video(video: Path, max_bytes: float) -> list[Path]:
    """Разбить видео на части по ~max_bytes (нужен ffmpeg)."""
    out_pattern = str(video.with_suffix(".part%03d.mp4"))
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(video), "-c", "copy", "-fs", str(int(max_bytes)), out_pattern],
        check=True, capture_output=True,
    )
    return sorted(video.parent.glob(video.stem + ".part*.mp4"))


def upload_to_telegram(folder: Path, meta: dict) -> bool:
    """Видео + чат + meta в канал. True — успех."""
    if not tg_configured():
        return False
    try:
        import asyncio
        from telethon import TelegramClient
    except ImportError:
        log("telegram: telethon не установлен — пропускаю (pip install telethon)")
        return False

    video = folder / meta.get("video_file", "video.mp4")

    async def _run() -> None:
        session = Path(__file__).parent / ".tg_session"
        client = TelegramClient(str(session), int(TG_API_ID), TG_API_HASH)
        await client.start(bot_token=TG_BOT_TOKEN)
        try:
            chat = await client.get_input_entity(TG_CHAT)
            dur = meta.get("duration_s", 0)
            caption = (
                f"📼 {meta.get('title') or 'Запись'}\n"
                f"{meta.get('game') or '—'} · {dur // 3600}ч {(dur % 3600) // 60}мин"
            )
            await client.send_message(chat, caption)

            parts: list[Path] = [video]
            if video.exists() and video.stat().st_size > TG_SIZE_LIMIT:
                if shutil.which("ffmpeg"):
                    log(f"telegram: видео {video.stat().st_size / 2**30:.1f} ГБ — разбиваю на части")
                    parts = split_video(video, TG_SIZE_LIMIT)
                else:
                    log("telegram: видео больше лимита, а ffmpeg не найден — пропускаю видео")

            total = len(parts)
            for i, part in enumerate(parts, 1):
                cap = f"часть {i}/{total}" if total > 1 else None
                await client.send_file(
                    chat, str(part),
                    caption=cap, supports_streaming=(total == 1),
                    force_document=(total > 1),
                )
                log(f"telegram: отправлено {part.name} ({part.stat().st_size / 2**20:.0f} МБ)")

            # чат и meta — маленькие документы
            for extra in ("chat.jsonl", "meta.json"):
                f = folder / extra
                if f.exists():
                    await client.send_file(chat, str(f), force_document=True)
        finally:
            await client.disconnect()

    try:
        log(f"telegram: загружаю {folder.name} в канал...")
        asyncio.run(_run())
        log("telegram: готово")
        return True
    except Exception as exc:  # noqa: BLE001
        log(f"telegram: ошибка: {exc}")
        return False


def extract_vod_thumbnail(video: Path, thumb: Path, duration_s: int) -> bool:
    """Извлекает кадр из записанного VOD с помощью ffmpeg."""
    if not video.is_file() or not shutil.which("ffmpeg"):
        return False
    seek_candidates = [min(180, max(5, duration_s // 5)) if duration_s > 10 else 2, 1]
    tmp = thumb.with_suffix(".tmp.jpg")
    for ss in seek_candidates:
        try:
            subprocess.run(
                [
                    "ffmpeg", "-y",
                    "-ss", str(ss),
                    "-i", str(video),
                    "-frames:v", "1",
                    "-vf", "scale=960:-2",
                    "-q:v", "3",
                    str(tmp),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=25,
                check=False,
            )
            if tmp.is_file() and tmp.stat().st_size > 1024:
                tmp.replace(thumb)
                return True
        except Exception:
            pass
        finally:
            if tmp.exists():
                tmp.unlink(missing_ok=True)
    return False


def record_session(stream: dict) -> None:
    started_at = stream.get("started_at", datetime.now(timezone.utc).isoformat())
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M")
    folder = RECORD_DIR / f"{stamp}_{slugify(stream.get('title', 'stream'))}"
    folder.mkdir(parents=True, exist_ok=True)

    video = folder / "video.mp4"
    chat_file = folder / "chat.jsonl"
    meta = {
        "channel": CHANNEL,
        "title": stream.get("title"),
        "game": stream.get("game_name"),
        "started_at": started_at,
        "record_started_at": datetime.now(timezone.utc).isoformat(),
        "video_file": "video.mp4",
        "chat_file": "chat.jsonl",
    }
    log(f"стрим живой: «{meta['title']}» ({meta['game']}) → {folder}")

    chat = ChatRecorder(CHANNEL, chat_file)
    chat.start()

    cmd = [
        "streamlink",
        f"https://www.twitch.tv/{CHANNEL}",
        "best",
        "-o", str(video),
        "--force",
        "--twitch-disable-ads",
        "--twitch-disable-hosting",
        *EXTRA_STREAMLINK_ARGS,
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)

    try:
        proc.wait()
    finally:
        chat.stop()
        chat.join(timeout=10)
        meta["ended_at"] = datetime.now(timezone.utc).isoformat()
        started = datetime.fromisoformat(meta["record_started_at"].replace("Z", "+00:00"))
        meta["duration_s"] = round((datetime.now(timezone.utc) - started).total_seconds())
        if extract_vod_thumbnail(video, folder / "thumb.jpg", meta["duration_s"]):
            meta["thumb_file"] = "thumb.jpg"
        (folder / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        log(f"запись завершена: {meta['duration_s']}с, чат: "
            f"{chat_file.stat().st_size if chat_file.exists() else 0} байт")

        # 1) Telegram (нужен локальный файл) → 2) Drive (rclone move) → 3) ротация
        if tg_configured():
            upload_to_telegram(folder, meta)
        if RCLONE_REMOTE:
            upload_to_cloud(folder)
        rotate_old_recordings()


# ----------------------------- главный цикл ----------------------------

_running = True


def _sigterm(_sig, _frm):
    global _running
    _running = False


def main() -> None:
    if not CLIENT_ID or not CLIENT_SECRET:
        sys.exit("Задайте TWITCH_CLIENT_ID и TWITCH_CLIENT_SECRET "
                 "(dev.twitch.tv/console → Register Your Application)")
    RECORD_DIR.mkdir(parents=True, exist_ok=True)
    signal.signal(signal.SIGINT, _sigterm)
    signal.signal(signal.SIGTERM, _sigterm)

    log(f"слежу за twitch.tv/{CHANNEL}...")
    while _running:
        stream = get_live_stream()
        if stream:
            rotate_old_recordings()
            try:
                record_session(stream)
            except Exception as exc:  # noqa: BLE001
                log(f"ошибка сессии: {exc}")
                time.sleep(30)
        else:
            time.sleep(POLL_INTERVAL)
    log("остановлено")


if __name__ == "__main__":
    main()

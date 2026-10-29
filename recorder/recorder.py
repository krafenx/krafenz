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


def _load_dotenv(env_file: Path) -> None:
    if not env_file.is_file():
        return
    try:
        lines = env_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


ROOT_DIR = Path(__file__).resolve().parent.parent
_load_dotenv(ROOT_DIR / ".env")

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

# ----------------------------- конфиг (env) -----------------------------

CHANNEL = (os.environ.get("RECORDER_CHANNEL") or os.environ.get("TWITCH_CHANNEL") or "danonsha").strip()
CLIENT_ID = os.environ.get("TWITCH_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("TWITCH_CLIENT_SECRET", "")
RECORD_DIR = Path(os.environ.get("RECORD_DIR", ROOT_DIR / "vods"))
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "60"))
STREAMLINK_BIN = os.environ.get("STREAMLINK_BIN", "streamlink")
FFMPEG_BIN = os.environ.get("FFMPEG_BIN", "ffmpeg")

# --- облако и ротация ---
# RCLONE_REMOTE="gdrive:" или "gdrive:vods" — после завершения записи папка
# улетает в облако (rclone move), локальная копия удаляется.
RCLONE_REMOTE = os.environ.get("RCLONE_REMOTE", "").strip()
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


def _resolve_bin(configured: str, default_name: str) -> str | None:
    """Ищет исполняемый файл: явный путь, корень проекта, recorder/, .venv и PATH."""
    raw = (configured or default_name).strip().strip('"').strip("'")
    if raw:
        cand = Path(raw)
        if cand.is_file():
            return str(cand.resolve())
        rel = ROOT_DIR / raw
        if rel.is_file():
            return str(rel.resolve())
        found_cfg = shutil.which(raw)
        if found_cfg:
            return found_cfg

    stem = Path(default_name).stem
    local_dirs = [
        ROOT_DIR,
        ROOT_DIR / "recorder",
        ROOT_DIR / ".venv" / "Scripts",
        ROOT_DIR / ".venv" / "bin",
        ROOT_DIR / "venv" / "Scripts",
        ROOT_DIR / "venv" / "bin",
    ]
    for d in local_dirs:
        for ext in (".exe", ""):
            p = d / f"{stem}{ext}"
            if p.is_file():
                return str(p.resolve())
    return shutil.which(default_name)


def _normalize_rclone_remote(remote: str) -> str:
    r = (remote or "").strip().strip('"').strip("'")
    if not r:
        return ""
    if ":" not in r:
        return f"{r}:"
    return r


def _rclone_dest(remote: str, sub_name: str) -> str:
    base = _normalize_rclone_remote(remote)
    if base.endswith(":"):
        return f"{base}{sub_name}"
    return f"{base.rstrip('/')}/{sub_name}"


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
        headers = {
            "Client-ID": CLIENT_ID,
            "Authorization": f"Bearer {app_access_token()}",
        }
        r = requests.get(
            "https://api.twitch.tv/helix/streams",
            params={"user_login": CHANNEL},
            headers=headers,
            timeout=15,
        )
        if r.status_code == 401:
            _token_cache.clear()
            headers["Authorization"] = f"Bearer {app_access_token()}"
            r = requests.get(
                "https://api.twitch.tv/helix/streams",
                params={"user_login": CHANNEL},
                headers=headers,
                timeout=15,
            )
        r.raise_for_status()
        data = r.json().get("data", [])
        return data[0] if data else None
    except Exception as exc:  # noqa: BLE001 - сеть/токен не должны ронять демон
        log(f"Helix error: {exc}")
        return None


# ----------------------------- чат --------------------------------------

_IRC_PRIVMSG_RE = re.compile(
    r"^(?:@(\S+)\s+)?:([A-Za-z0-9_]+)!.*?\sPRIVMSG\s+#\S+\s+:(.*)$",
    flags=re.IGNORECASE,
)


def _unescape_irc_tag(val: str) -> str:
    return (
        val.replace(r"\s", " ")
        .replace(r"\:", ";")
        .replace(r"\r", "")
        .replace(r"\n", "")
        .replace(r"\\", "\\")
    )


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
        self.ws = None

    def stop(self) -> None:
        self._stop.set()
        ws = self.ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    def _offset(self) -> float:
        return max(0.0, round(time.time() - self.start_ts, 3))

    def _flush(self) -> None:
        if not self._buffer:
            return
        with self.out_file.open("a", encoding="utf-8") as fh:
            fh.write("".join(self._buffer))
        self._buffer.clear()

    def _handle_line(self, line: str) -> None:
        if not line:
            return
        # PING/PONG — иначе Twitch отключит
        if line.startswith("PING"):
            _, _, payload = line.partition(" ")
            self.ws.send(f"PONG {payload or ':tmi.twitch.tv'}")
            return
        m = _IRC_PRIVMSG_RE.match(line)
        if not m:
            return
        raw_tags, login_user, msg = m.group(1) or "", m.group(2), m.group(3)
        user = login_user
        if raw_tags:
            for pair in raw_tags.split(";"):
                if pair.startswith("display-name="):
                    dn = _unescape_irc_tag(pair[13:]).strip()
                    if dn:
                        user = dn
                    break
        if msg.startswith("\x01ACTION ") and msg.endswith("\x01"):
            msg = msg[8:-1].strip()
        elif msg.startswith("\x01") and msg.endswith("\x01"):
            return
        if not msg:
            return
        self._buffer.append(
            json.dumps({"t": self._offset(), "user": user, "msg": msg},
                       ensure_ascii=False)
            + "\n"
        )
        if len(self._buffer) >= CHAT_FLUSH_EVERY:
            self._flush()

    def _handle(self, raw: str) -> None:
        for line in raw.splitlines():
            self._handle_line(line.rstrip("\r\n"))

    def run(self) -> None:
        backoff = 5
        while not self._stop.is_set():
            try:
                log("chat: подключение к IRC...")
                self.ws = websocket.create_connection(
                    IRC_URI, timeout=10,
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
                        data = self.ws.recv()
                        if not data:
                            break
                        self._handle(data)
                    except websocket.WebSocketTimeoutException:
                        if not self._stop.is_set():
                            self.ws.send("PING :tmi.twitch.tv")
            except Exception as exc:  # noqa: BLE001 - переподключаемся всегда
                if self._stop.is_set():
                    break
                log(f"chat: {exc} — повтор через {backoff}с")
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 120)
            finally:
                try:
                    self._flush()
                except Exception:
                    pass
                ws = self.ws
                self.ws = None
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        pass


# ----------------------------- запись -----------------------------------

def disk_free_gb() -> float:
    RECORD_DIR.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(RECORD_DIR).free / (1024 ** 3)


def rotate_old_recordings() -> None:
    """Удалять самые старые завершённые записи, пока свободно < MIN_FREE_GB."""
    attempted: set[Path] = set()
    while disk_free_gb() < MIN_FREE_GB:
        candidates = sorted(
            (p for p in RECORD_DIR.iterdir()
             if p not in attempted and p.is_dir() and (p / "meta.json").exists()),
            key=lambda p: p.name,
        )
        if not candidates:
            log("rotate: удалять нечего, а места всё равно мало!")
            return
        victim = candidates[0]
        attempted.add(victim)
        log(f"rotate: удаляю {victim.name} (свободно {disk_free_gb():.1f} ГБ)")
        shutil.rmtree(victim, ignore_errors=True)


def _format_size(num_bytes: int) -> str:
    if num_bytes <= 0:
        return ""
    gb = num_bytes / (1024 ** 3)
    if gb >= 1.0:
        return f"{gb:.2f} GB"
    mb = num_bytes / (1024 ** 2)
    if mb >= 1.0:
        return f"{mb:.1f} MB"
    return f"{max(1, round(num_bytes / 1024))} KB"


def upload_to_cloud(folder: Path) -> bool:
    """rclone move папки записи в облако. True — успех, локальная копия снесена."""
    remote = _normalize_rclone_remote(RCLONE_REMOTE)
    if not remote:
        return False
    rclone_exe = _resolve_bin(RCLONE_BIN, "rclone")
    if not rclone_exe:
        log("cloud: rclone не найден (положите rclone.exe в папку проекта или добавьте в PATH) — запись остаётся локально")
        return False
    dest = _rclone_dest(remote, folder.name)
    cmd = [
        rclone_exe, "move", str(folder), dest,
        "--delete-empty-src-dirs", "--transfers", "4", "--checkers", "8", "--stats-one-line", "-v",
    ]
    local_conf = ROOT_DIR / "rclone.conf"
    if local_conf.is_file():
        cmd.extend(["--config", str(local_conf)])
    try:
        log(f"cloud: заливаю {folder.name} → {dest} ...")
        res = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=None,
        )
        if res.returncode == 0:
            if folder.exists():
                shutil.rmtree(folder, ignore_errors=True)
            log(f"cloud: {folder.name} загружена в Google Drive, локальная копия удалена")
            return True
        err_tail = (res.stderr or res.stdout or "").strip()[-400:]
        log(f"cloud: ошибка rclone (код {res.returncode}): {err_tail}")
    except Exception as exc:  # noqa: BLE001
        log(f"cloud: {exc}")
    return False


def sync_pending_uploads() -> None:
    """Дозагружает завершённые локальные записи (с meta.json), если прошлая выгрузка прервалась."""
    if not _normalize_rclone_remote(RCLONE_REMOTE) or not RECORD_DIR.exists():
        return
    pending = sorted(
        (p for p in RECORD_DIR.iterdir() if p.is_dir() and (p / "meta.json").is_file()),
        key=lambda p: p.name,
    )
    for folder in pending:
        if _stop_event.is_set():
            break
        upload_to_cloud(folder)


def test_cloud_connection() -> int:
    """Проверяет найденный rclone и доступ к RCLONE_REMOTE без ожидания стрима."""
    rclone_exe = _resolve_bin(RCLONE_BIN, "rclone")
    if not rclone_exe:
        log("ОШИБКА: rclone не найден ни в папке проекта, ни в PATH.")
        return 1
    log(f"rclone найден: {rclone_exe}")
    remote = _normalize_rclone_remote(RCLONE_REMOTE)
    if not remote:
        log("ОШИБКА: RCLONE_REMOTE не задан в .env (например: RCLONE_REMOTE=gdrive:)")
        return 1
    cmd = [rclone_exe, "lsd", remote, "--max-depth", "1"]
    local_conf = ROOT_DIR / "rclone.conf"
    if local_conf.is_file():
        cmd.extend(["--config", str(local_conf)])
    log(f"проверяю доступ к {remote} ...")
    res = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
    if res.returncode != 0:
        log(f"ОШИБКА rclone: {(res.stderr or res.stdout or '').strip()}")
        return 1
    lines = [ln.strip() for ln in (res.stdout or "").splitlines() if ln.strip()]
    log(f"УСПЕХ! Доступ к Google Drive ({remote}) работает, найдено подпапок: {len(lines)}")
    return 0


def tg_configured() -> bool:
    return bool(TG_API_ID and TG_API_HASH and TG_BOT_TOKEN and TG_CHAT)


def split_video(video: Path, max_bytes: float) -> list[Path]:
    """Разбить видео на части по ~max_bytes (нужен ffmpeg)."""
    ffmpeg_exe = _resolve_bin(FFMPEG_BIN, "ffmpeg") or "ffmpeg"
    out_pattern = str(video.with_suffix(".part%03d.mp4"))
    subprocess.run(
        [ffmpeg_exe, "-y", "-i", str(video), "-c", "copy", "-fs", str(int(max_bytes)), out_pattern],
        stdin=subprocess.DEVNULL, check=True, capture_output=True,
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
            split_parts: list[Path] = []
            if video.exists() and video.stat().st_size > TG_SIZE_LIMIT:
                if _resolve_bin(FFMPEG_BIN, "ffmpeg"):
                    log(f"telegram: видео {video.stat().st_size / 2**30:.1f} ГБ — разбиваю на части")
                    split_parts = split_video(video, TG_SIZE_LIMIT)
                    parts = split_parts
                else:
                    log("telegram: видео больше лимита, а ffmpeg не найден — пропускаю видео")
                    parts = []

            try:
                total = len(parts)
                for i, part in enumerate(parts, 1):
                    cap = f"часть {i}/{total}" if total > 1 else None
                    await client.send_file(
                        chat, str(part),
                        caption=cap, supports_streaming=(total == 1),
                        force_document=(total > 1),
                    )
                    log(f"telegram: отправлено {part.name} ({part.stat().st_size / 2**20:.0f} МБ)")
            finally:
                # Удаляем временные .partNNN.mp4, чтобы rclone не выгружал их дублями в Google Drive
                for sp in split_parts:
                    sp.unlink(missing_ok=True)

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


def remux_mpegts_to_mp4(video: Path) -> bool:
    """Перепаковывает сырой MPEG-TS поток от streamlink в настоящий MP4 (+faststart) без перекодирования."""
    if not video.is_file() or video.stat().st_size < 1024:
        return False
    try:
        with video.open("rb") as fh:
            head = fh.read(12)
    except OSError:
        return False
    # Если уже ISO BMFF MP4 (байты 4..8 == b'ftyp') — ничего делать не нужно
    if len(head) >= 8 and head[4:8] == b"ftyp":
        return False
    ffmpeg_exe = _resolve_bin(FFMPEG_BIN, "ffmpeg")
    if not ffmpeg_exe:
        log("remux: ffmpeg не найден — video.mp4 остаётся в исходном MPEG-TS контейнере")
        return False

    orig_size = video.stat().st_size
    tmp = video.with_suffix(".remux.mp4")
    cmds = [
        [
            ffmpeg_exe, "-y",
            "-i", str(video),
            "-c", "copy",
            "-bsf:a", "aac_adtstoasc",
            "-movflags", "+faststart",
            str(tmp),
        ],
        [
            ffmpeg_exe, "-y",
            "-i", str(video),
            "-c", "copy",
            "-movflags", "+faststart",
            str(tmp),
        ],
    ]
    try:
        for cmd in cmds:
            tmp.unlink(missing_ok=True)
            res = subprocess.run(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=None,
                check=False,
            )
            if res.returncode == 0 and tmp.is_file() and tmp.stat().st_size > max(1024, int(orig_size * 0.5)):
                tmp.replace(video)
                log(f"remux: MPEG-TS → MP4 (+faststart) готово ({_format_size(video.stat().st_size)})")
                return True
    except Exception as exc:  # noqa: BLE001
        log(f"remux: ошибка ffmpeg: {exc}")
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
    return False


def extract_vod_thumbnail(video: Path, thumb: Path, duration_s: int) -> bool:
    """Извлекает кадр из записанного VOD с помощью ffmpeg."""
    ffmpeg_exe = _resolve_bin(FFMPEG_BIN, "ffmpeg")
    if not video.is_file() or not ffmpeg_exe:
        return False
    seek_candidates = [min(180, max(5, duration_s // 5)) if duration_s > 10 else 2, 1]
    tmp = thumb.with_suffix(".tmp.jpg")
    for ss in seek_candidates:
        try:
            subprocess.run(
                [
                    ffmpeg_exe, "-y",
                    "-ss", str(ss),
                    "-i", str(video),
                    "-frames:v", "1",
                    "-vf", "scale=960:-2",
                    "-q:v", "3",
                    str(tmp),
                ],
                stdin=subprocess.DEVNULL,
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


_active_proc: subprocess.Popen | None = None
_stop_event = threading.Event()


def record_session(stream: dict) -> None:
    global _active_proc
    streamlink_exe = _resolve_bin(STREAMLINK_BIN, "streamlink")
    if not streamlink_exe:
        log(f"streamlink ({STREAMLINK_BIN}) не найден в PATH — установите: pip install streamlink")
        _stop_event.wait(POLL_INTERVAL)
        return

    started_at = stream.get("started_at", datetime.now(timezone.utc).isoformat())
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M")
    base_name = f"{stamp}_{slugify(stream.get('title', 'stream'))}"
    folder = RECORD_DIR / base_name
    suffix = 2
    while folder.exists() and (folder / "meta.json").exists():
        folder = RECORD_DIR / f"{base_name}_{suffix}"
        suffix += 1
    folder.mkdir(parents=True, exist_ok=True)

    video = folder / "video.mp4"
    chat_file = folder / "chat.jsonl"
    sl_log = folder / "streamlink.log"
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
        streamlink_exe,
        f"https://www.twitch.tv/{CHANNEL}",
        "best",
        "-o", str(video),
        "--force",
        "--twitch-disable-ads",
        *EXTRA_STREAMLINK_ARGS,
    ]
    log_fh = sl_log.open("w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
    )
    _active_proc = proc

    try:
        proc.wait()
    finally:
        _active_proc = None
        log_fh.close()
        chat.stop()
        chat.join(timeout=10)
        started = datetime.fromisoformat(meta["record_started_at"].replace("Z", "+00:00"))
        duration_s = round((datetime.now(timezone.utc) - started).total_seconds())
        has_video = video.is_file() and video.stat().st_size > 1024

        if not has_video:
            tail = ""
            if sl_log.is_file():
                try:
                    tail = sl_log.read_text(encoding="utf-8", errors="replace").strip()[-400:]
                except OSError:
                    tail = ""
            log(f"streamlink завершился без видео (код {proc.returncode})" + (f": {tail}" if tail else ""))
            shutil.rmtree(folder, ignore_errors=True)
            if not _stop_event.is_set():
                _stop_event.wait(min(POLL_INTERVAL, 30))
        else:
            sl_log.unlink(missing_ok=True)
            meta["ended_at"] = datetime.now(timezone.utc).isoformat()
            meta["duration_s"] = duration_s
            remux_mpegts_to_mp4(video)
            v_bytes = video.stat().st_size
            meta["size_bytes"] = v_bytes
            meta["size_label"] = _format_size(v_bytes)
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
    _stop_event.set()
    proc = _active_proc
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
        except Exception:
            pass


def main() -> None:
    if "--test-cloud" in sys.argv[1:]:
        sys.exit(test_cloud_connection())

    if not CLIENT_ID or not CLIENT_SECRET:
        sys.exit("Задайте TWITCH_CLIENT_ID и TWITCH_CLIENT_SECRET "
                 "(dev.twitch.tv/console → Register Your Application)")
    RECORD_DIR.mkdir(parents=True, exist_ok=True)
    signal.signal(signal.SIGINT, _sigterm)
    signal.signal(signal.SIGTERM, _sigterm)

    if _normalize_rclone_remote(RCLONE_REMOTE):
        rc_path = _resolve_bin(RCLONE_BIN, "rclone")
        log(f"облако: {_normalize_rclone_remote(RCLONE_REMOTE)} "
            f"({'rclone: ' + rc_path if rc_path else 'ВНИМАНИЕ: rclone не найден!'})")
        sync_pending_uploads()

    log(f"слежу за twitch.tv/{CHANNEL}...")
    while _running and not _stop_event.is_set():
        stream = get_live_stream()
        if stream:
            rotate_old_recordings()
            try:
                record_session(stream)
            except Exception as exc:  # noqa: BLE001
                log(f"ошибка сессии: {exc}")
                _stop_event.wait(30)
        else:
            sync_pending_uploads()
            _stop_event.wait(POLL_INTERVAL)
    log("остановлено")


if __name__ == "__main__":
    main()

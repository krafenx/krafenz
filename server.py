#!/usr/bin/env python3
"""
Сервер сайта Krafen и архива стримов danonsha (FastAPI + SQLite).

Полностью заменяет Cloudflare Workers и Workers KV:
  - раздаёт HTML/CSS/JS и медиа-ассеты с чистыми URL (/watchlist, /setup и т.д.);
  - хранит коллекции (watchlist, gamelist, setup, wishlist) и OAuth-токены в SQLite (data/krafen.db)
    с автоматической миграцией существующих JSON-файлов;
  - реализует защищённую админ-сессию (HMAC SHA-256 cookie) и версионирование списков (revision);
  - проксирует и кэширует Twitch Helix, Discord Invite, Spotify, Last.fm, TMDB, Shikimori и IGDB;
  - раздаёт VOD-архив с поддержкой HTTP Range и чата записи;
  - при RECORDER=1 запускает фоновый процесс автозаписи recorder/recorder.py.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import gzip
import hashlib
import html
import hmac
import io
import ipaddress
import json
import os
import platform
import re
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

import requests
import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    StreamingResponse,
)

ROOT = Path(__file__).resolve().parent
_DOTENV_VALUES: dict[str, str] = {}


def load_dotenv(env_file: Path) -> None:
    """Читает локальный .env без внешних зависимостей, не перезаписывая уже заданные переменные."""
    if not env_file.is_file():
        return
    try:
        raw = env_file.read_bytes()
    except OSError:
        return
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        text_content = raw.decode("utf-16", errors="ignore")
    else:
        text_content = raw.decode("utf-8-sig", errors="ignore")
        if "\x00" in text_content:
            text_content = text_content.replace("\x00", "")
    for raw_line in text_content.splitlines():
        line = raw_line.strip().lstrip("\ufeff")
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if not key:
            continue
        if len(val) >= 2 and val[0] == val[-1] and val[0] in {'"', "'"}:
            val = val[1:-1]
        elif " #" in val:
            val = val.split(" #", 1)[0].strip()
        _DOTENV_VALUES[key] = val
        os.environ.setdefault(key, val)


load_dotenv(ROOT / ".env")

def _resolve_root_path(raw_value: str | Path) -> Path:
    p = Path(raw_value)
    return p if p.is_absolute() else (ROOT / p).resolve()


DATA_DIR = _resolve_root_path(os.environ.get("DATA_DIR", ROOT / "data"))
DB_PATH = DATA_DIR / "krafen.db"
AUTH_DB_PATH = DATA_DIR / "auth.db"
ANALYTICS_DB_PATH = DATA_DIR / "analytics.db"
HEADLESS_LOG_PATH = DATA_DIR / "server.log"
SEED_SQL_PATH = DATA_DIR / "seed.sql"
_SERVER_STARTED_AT = time.time()
THUMB_CACHE_DIR = DATA_DIR / "vod_thumbs"
CHAT_CACHE_DIR = DATA_DIR / "vod_chats"
IMG_CACHE_DIR = DATA_DIR / "img_cache"
RECORD_DIR = _resolve_root_path(os.environ.get("RECORD_DIR", ROOT / "vods"))
PORT = int(os.environ.get("PORT", "8080"))
HOST = os.environ.get("HOST", "0.0.0.0")
PUBLIC_ORIGIN = os.environ.get("PUBLIC_ORIGIN", "").strip().rstrip("/")

_RAW_ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
_WEAK_PLACEHOLDER_PASSWORDS = {"change-me", "changeme"}
ADMIN_PASSWORD = "" if _RAW_ADMIN_PASSWORD.strip().lower() in _WEAK_PLACEHOLDER_PASSWORDS else _RAW_ADMIN_PASSWORD
_ADMIN_PASSWORD_IS_PLACEHOLDER = bool(_RAW_ADMIN_PASSWORD and not ADMIN_PASSWORD)
SECURE_COOKIES_MODE = os.environ.get("SECURE_COOKIES", "auto").strip().lower()
TRUST_PROXY_MODE = os.environ.get("TRUST_PROXY_HEADERS", "auto").strip().lower()

TWITCH_CHANNEL = os.environ.get("TWITCH_CHANNEL", "krafen")
TWITCH_CLIENT_ID = os.environ.get("TWITCH_CLIENT_ID", "")
TWITCH_CLIENT_SECRET = os.environ.get("TWITCH_CLIENT_SECRET", "")
IGDB_CLIENT_ID = os.environ.get("IGDB_CLIENT_ID", "") or TWITCH_CLIENT_ID
IGDB_CLIENT_SECRET = os.environ.get("IGDB_CLIENT_SECRET", "") or TWITCH_CLIENT_SECRET
IGDB_ACCESS_TOKEN = os.environ.get("IGDB_ACCESS_TOKEN", "")

LASTFM_API_KEY = os.environ.get("LASTFM_API_KEY", "")
LASTFM_USER = os.environ.get("LASTFM_USER", "krafen")
TMDB_ACCESS_TOKEN = os.environ.get("TMDB_ACCESS_TOKEN", "")
TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "")
SPOTIFY_CLIENT_ID = os.environ.get("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "")
DISCORD_INVITE_CODE = os.environ.get("DISCORD_INVITE_CODE", "kGdxDzxtmK")
GDRIVE_FOLDER_ID = os.environ.get("GDRIVE_FOLDER_ID", "1e417_q9s6SjcZHyqMYzbVs8OHqfY5Ifk").strip()
GDRIVE_API_KEY = os.environ.get("GDRIVE_API_KEY", "").strip()

AUTO_UPDATE_MODE = os.environ.get("AUTO_UPDATE", "auto").strip().lower()
AUTO_UPDATE_REPO = os.environ.get("AUTO_UPDATE_REPO", "krafenx/krafenz").strip().strip("/")
AUTO_UPDATE_BRANCH = os.environ.get("AUTO_UPDATE_BRANCH", "main").strip() or "main"
try:
    AUTO_UPDATE_INTERVAL = max(0, int(os.environ.get("AUTO_UPDATE_INTERVAL", "0") or "0"))
except ValueError:
    AUTO_UPDATE_INTERVAL = 0
UPDATE_SHA_PATH = DATA_DIR / ".update_sha"
_UPDATE_LOCK = threading.Lock()

SESSION_COOKIE = "kf_admin_session"
SPOTIFY_STATE_COOKIE = "kf_spotify_oauth"
SESSION_TTL_SECONDS = 12 * 60 * 60

TMDB_BASE = "https://api.themoviedb.org"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w342"
SHIKIMORI_BASE = "https://shikimori.one"
IGDB_BASE = "https://api.igdb.com/v4"
TWITCH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
LASTFM_API_BASE = "https://ws.audioscrobbler.com/2.0/"
SPOTIFY_API_BASE = "https://api.spotify.com/v1"
SPOTIFY_AUTH_URL = "https://accounts.spotify.com/authorize"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
DISCORD_INVITE_BASE = "https://discord.com/api/invites"

DATA_DIR.mkdir(parents=True, exist_ok=True)
RECORD_DIR.mkdir(parents=True, exist_ok=True)

_UPDATE_SKIP_TOP_DIRS = {".git", ".github", ".venv", "venv", "__pycache__", "vods"}
_MAX_UPDATE_ZIP_BYTES = 64 * 1024 * 1024


def _is_pterodactyl_env() -> bool:
    return bool(
        os.environ.get("P_SERVER_UUID")
        or os.environ.get("P_SERVER_LOCATION")
        or str(ROOT).startswith("/home/container")
    )


def is_auto_update_enabled() -> bool:
    # 1. Приоритет у собственной переменной KRAFEN_AUTO_UPDATE (из .env или окружения),
    #    которая не конфликтует с системной AUTO_UPDATE=0 панели Pterodactyl.
    explicit_kf = (_DOTENV_VALUES.get("KRAFEN_AUTO_UPDATE") or os.environ.get("KRAFEN_AUTO_UPDATE") or "").strip().lower()
    if explicit_kf in {"0", "false", "no", "off"}:
        return False
    if explicit_kf in {"1", "true", "yes", "on"}:
        return True

    # 2. Если пользователь явно прописал AUTO_UPDATE именно в своём локальном .env — учитываем его.
    dotenv_au = (_DOTENV_VALUES.get("AUTO_UPDATE") or "").strip().lower()
    if dotenv_au in {"0", "false", "no", "off"}:
        return False
    if dotenv_au in {"1", "true", "yes", "on"}:
        return True

    # 3. В контейнере Pterodactyl (H1Cloud и др.) сама панель выставляет системный AUTO_UPDATE=0 —
    #    игнорируем его и включаем встроенное автообновление по умолчанию (даже если остался .git).
    if _is_pterodactyl_env():
        return True

    # 4. Вне Pterodactyl: уважаем системную AUTO_UPDATE, а в режиме auto включаем, если нет .git.
    if AUTO_UPDATE_MODE in {"0", "false", "no", "off"}:
        return False
    if AUTO_UPDATE_MODE in {"1", "true", "yes", "on"}:
        return True
    return not (ROOT / ".git").exists()


def _is_protected_update_path(rel_posix: str, target: Path) -> bool:
    parts = [p for p in rel_posix.split("/") if p]
    if not parts or ".." in parts:
        return True
    top = parts[0]
    if top in _UPDATE_SKIP_TOP_DIRS:
        return True
    if len(parts) == 1 and (top == ".env" or (top.startswith(".env.") and top != ".env.example")):
        return True
    if top == "data":
        if len(parts) >= 2 and parts[1] in {".update_sha", "vod_thumbs", "vod_chats", "img_cache"}:
            return True
        name_lower = parts[-1].lower()
        is_user_db_or_json = (
            name_lower.endswith(".json")
            or name_lower.endswith(".db")
            or ".db-" in name_lower
            or ".sqlite" in name_lower
        )
        # Сохраняем живую БД и пользовательские JSON, если они уже созданы на диске
        if is_user_db_or_json and target.exists():
            return True
    return False


def _resolve_github_token() -> str:
    for key in ("GITHUB_TOKEN", "GH_TOKEN", "ACCESS_TOKEN", "GIT_ACCESS_TOKEN"):
        val = (_DOTENV_VALUES.get(key) or os.environ.get(key) or "").strip()
        if val:
            return val
    return ""


def _fetch_remote_branch_sha() -> tuple[str, str]:
    """Возвращает (sha, error_code). При успехе error_code == ''."""
    if not AUTO_UPDATE_REPO or "/" not in AUTO_UPDATE_REPO:
        return "", "invalid_repo"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "krafen-auto-updater/1.0",
    }
    gh_token = _resolve_github_token()
    if gh_token:
        headers["Authorization"] = f"Bearer {gh_token}"
    api_url = f"https://api.github.com/repos/{AUTO_UPDATE_REPO}/commits/{AUTO_UPDATE_BRANCH}"
    try:
        resp = requests.get(api_url, headers=headers, timeout=8)
        if resp.status_code == 200:
            data = resp.json()
            sha = str(data.get("sha") or "").strip()
            if sha:
                return sha, ""
        return "", f"http_{resp.status_code}"
    except Exception as exc:
        return "", type(exc).__name__


_SAFE_GH_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SAFE_GH_REF_RE = re.compile(r"^[A-Za-z0-9_./-]{1,120}$")
_ALLOWED_GH_ZIP_HOSTS = {"api.github.com", "github.com", "codeload.github.com"}


def _is_allowed_github_download_host(hostname: str) -> bool:
    h = (hostname or "").lower().rstrip(".")
    return h in _ALLOWED_GH_ZIP_HOSTS or h.endswith(".githubusercontent.com")


def _download_github_zip() -> bytes:
    if not _SAFE_GH_REPO_RE.fullmatch(AUTO_UPDATE_REPO) or not _SAFE_GH_REF_RE.fullmatch(AUTO_UPDATE_BRANCH):
        raise ValueError("invalid_auto_update_config")
    gh_token = _resolve_github_token()
    candidates: list[tuple[str, dict[str, str]]] = []
    api_headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "krafen-auto-updater/1.0",
    }
    if gh_token:
        api_headers["Authorization"] = f"Bearer {gh_token}"
    # Для приватных репозиториев GitHub отдаёт архив именно через REST API /zipball/{ref}
    candidates.append(
        (f"https://api.github.com/repos/{AUTO_UPDATE_REPO}/zipball/{AUTO_UPDATE_BRANCH}", api_headers)
    )

    pub_headers = {"User-Agent": "krafen-auto-updater/1.0"}
    if gh_token:
        pub_headers["Authorization"] = f"Bearer {gh_token}"
    candidates.append(
        (f"https://codeload.github.com/{AUTO_UPDATE_REPO}/zip/refs/heads/{AUTO_UPDATE_BRANCH}", pub_headers)
    )
    candidates.append(
        (f"https://github.com/{AUTO_UPDATE_REPO}/archive/refs/heads/{AUTO_UPDATE_BRANCH}.zip", pub_headers)
    )

    last_err: Exception | None = None
    saw_404 = False
    for start_url, base_headers in candidates:
        try:
            cur_url = start_url
            cur_headers = dict(base_headers)
            for _hop in range(5):
                parsed = urlsplit(cur_url)
                if parsed.scheme != "https" or not _is_allowed_github_download_host(parsed.hostname or ""):
                    raise ValueError("disallowed_redirect_host")
                # Не пересылаем токен на CDN/S3 presigned URLs (*.githubusercontent.com)
                if (parsed.hostname or "").lower().endswith(".githubusercontent.com"):
                    cur_headers.pop("Authorization", None)
                with requests.get(cur_url, headers=cur_headers, stream=True, timeout=25, allow_redirects=False) as resp:
                    if resp.status_code in (301, 302, 303, 307, 308):
                        loc = (resp.headers.get("location") or "").strip()
                        if not loc:
                            raise RuntimeError("empty_redirect_location")
                        cur_url = loc
                        continue
                    if resp.status_code == 404:
                        saw_404 = True
                    resp.raise_for_status()
                    buf = io.BytesIO()
                    total = 0
                    for chunk in resp.iter_content(chunk_size=64 * 1024):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > _MAX_UPDATE_ZIP_BYTES:
                            raise ValueError("update_archive_too_large")
                        buf.write(chunk)
                    return buf.getvalue()
            raise RuntimeError("too_many_redirects")
        except Exception as exc:
            last_err = exc
    if saw_404 and not gh_token:
        raise RuntimeError(
            f"репозиторий {AUTO_UPDATE_REPO} приватный (404) — задайте GITHUB_TOKEN в .env (Contents: Read-only) или сделайте репозиторий публичным"
        )
    raise RuntimeError(f"не удалось скачать архив с GitHub ({last_err})")


def perform_github_update(*, force: bool = False, allow_zip_fallback_on_api_error: bool = True) -> dict[str, Any]:
    with _UPDATE_LOCK:
        saved_sha = ""
        try:
            if UPDATE_SHA_PATH.is_file():
                saved_sha = UPDATE_SHA_PATH.read_text(encoding="utf-8").strip()
        except OSError:
            saved_sha = ""

        remote_sha, api_err = _fetch_remote_branch_sha()
        if not force and remote_sha and saved_sha and remote_sha == saved_sha:
            print(f"автообновление: актуальная версия ({remote_sha[:7]})", flush=True)
            return {
                "ok": True,
                "up_to_date": True,
                "sha": remote_sha,
                "updated": [],
                "server_changed": False,
            }
        if not remote_sha and not allow_zip_fallback_on_api_error:
            return {"ok": False, "error": f"github_api_{api_err}", "updated": [], "server_changed": False}

        try:
            raw_zip = _download_github_zip()
            updated_files: list[str] = []
            server_changed = False
            total_unpacked = 0

            with zipfile.ZipFile(io.BytesIO(raw_zip)) as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    if ((info.external_attr >> 16) & 0o170000) == 0o120000:
                        continue
                    if info.file_size > _MAX_UPDATE_ZIP_BYTES:
                        raise ValueError("zip_entry_too_large")
                    total_unpacked += max(0, int(info.file_size))
                    if total_unpacked > _MAX_UPDATE_ZIP_BYTES * 4:
                        raise ValueError("zip_unpacked_too_large")
                    raw_name = info.filename.replace("\\", "/").lstrip("/")
                    if "/" not in raw_name:
                        continue
                    _, _, rel_posix = raw_name.partition("/")
                    rel_posix = rel_posix.strip("/")
                    if not rel_posix:
                        continue

                    target = (ROOT / rel_posix).resolve()
                    try:
                        target.relative_to(ROOT)
                    except ValueError:
                        continue

                    if _is_protected_update_path(rel_posix, target):
                        continue

                    new_bytes = zf.read(info)
                    if target.is_file():
                        try:
                            old_hash = hashlib.sha256(target.read_bytes()).hexdigest()
                            new_hash = hashlib.sha256(new_bytes).hexdigest()
                            if old_hash == new_hash:
                                continue
                        except OSError:
                            pass

                    target.parent.mkdir(parents=True, exist_ok=True)
                    tmp_path = target.with_name(f".{target.name}.upd.{os.getpid()}")
                    try:
                        tmp_path.write_bytes(new_bytes)
                        os.replace(tmp_path, target)
                    finally:
                        if tmp_path.exists():
                            with contextlib.suppress(OSError):
                                tmp_path.unlink()

                    updated_files.append(rel_posix)
                    if rel_posix == "server.py":
                        server_changed = True

            if remote_sha:
                with contextlib.suppress(OSError):
                    DATA_DIR.mkdir(parents=True, exist_ok=True)
                    UPDATE_SHA_PATH.write_text(remote_sha + "\n", encoding="utf-8")

            sha_label = f" [{AUTO_UPDATE_BRANCH} @ {remote_sha[:7]}]" if remote_sha else f" [{AUTO_UPDATE_BRANCH}]"
            if updated_files:
                preview = ", ".join(updated_files[:8])
                more = f" и ещё {len(updated_files) - 8}" if len(updated_files) > 8 else ""
                print(
                    f"автообновление: обновлено файлов: {len(updated_files)} ({preview}{more}){sha_label}",
                    flush=True,
                )
            else:
                print(f"автообновление: все файлы актуальны{sha_label}", flush=True)

            return {
                "ok": True,
                "up_to_date": len(updated_files) == 0,
                "sha": remote_sha or saved_sha,
                "updated": updated_files,
                "server_changed": server_changed,
            }
        except Exception as exc:
            print(f"автообновление: ошибка обновления ({exc}) — продолжаю с локальными файлами", flush=True)
            return {"ok": False, "error": "update_failed", "updated": [], "server_changed": False}


_HEADLESS_LOG_FH: Any = None
_CONSOLE_TEE_LOG_FH: Any = None
_ACTIVE_REC_PROC: subprocess.Popen | None = None
_WIN_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
_AUTOSTART_REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
_AUTOSTART_REG_NAME = "KrafenServer"
PID_FILE_PATH = DATA_DIR / "server.pid"
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")
_CLI_MGMT_FLAGS = {
    "--install-autostart": "install",
    "--uninstall-autostart": "uninstall",
    "--restart": "restart",
    "--stop": "stop",
    "--status": "status",
}


class _TeeLogStream:
    """Дублирует вывод обычной консоли в data/server.log без ANSI-кодов."""

    def __init__(self, primary: Any, log_fh: Any) -> None:
        self._primary = primary
        self._log_fh = log_fh
        self.encoding = getattr(primary, "encoding", "utf-8") or "utf-8"

    def write(self, s: str) -> int:
        n = self._primary.write(s)
        if s:
            with contextlib.suppress(Exception):
                self._log_fh.write(_ANSI_ESCAPE_RE.sub("", s))
                self._log_fh.flush()
        return n

    def flush(self) -> None:
        with contextlib.suppress(Exception):
            self._primary.flush()
        with contextlib.suppress(Exception):
            self._log_fh.flush()

    def isatty(self) -> bool:
        try:
            return bool(self._primary.isatty())
        except Exception:
            return False

    def fileno(self) -> int:
        return self._primary.fileno()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._primary, name)


def _has_cli_mgmt_flag() -> bool:
    return any(flag in sys.argv[1:] for flag in _CLI_MGMT_FLAGS)


def _is_headless_process() -> bool:
    if _has_cli_mgmt_flag():
        return False
    if "--background" in sys.argv[1:] or os.environ.get("KRAFEN_HEADLESS") == "1":
        return True
    if sys.stdout is None or sys.stderr is None:
        return True
    exe_stem = Path(sys.executable).stem.lower()
    if exe_stem.startswith(("pythonw", "pyw")):
        return True
    for stream in (sys.stdout, sys.stderr):
        name = str(getattr(stream, "name", "") or "").lower()
        if name in {"nul", "/dev/null"} or name.endswith(("\\nul", "/nul")):
            return True
    return False


def _open_rotating_server_log() -> Any:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    log_path = DATA_DIR / "server.log"
    if log_path.is_file() and log_path.stat().st_size > 10 * 1024 * 1024:
        bak = DATA_DIR / "server.log.old"
        with contextlib.suppress(OSError):
            bak.unlink(missing_ok=True)
            log_path.replace(bak)
    return open(log_path, "a", encoding="utf-8", buffering=1, errors="replace")


def _reexec_current_process() -> None:
    os.environ["_KRAFEN_REEXEC"] = "1"
    script_path = str((ROOT / "server.py").resolve())
    print("автообновление: server.py изменился — перезапускаю процесс...", flush=True)
    if _ACTIVE_REC_PROC is not None and _ACTIVE_REC_PROC.poll() is None:
        with contextlib.suppress(Exception):
            _ACTIVE_REC_PROC.terminate()
            _ACTIVE_REC_PROC.wait(timeout=3)
    if _read_pid_file() == os.getpid():
        with contextlib.suppress(OSError):
            PID_FILE_PATH.unlink(missing_ok=True)
    if os.name != "nt":
        os.execv(sys.executable, [sys.executable, script_path, *sys.argv[1:]])
    elif _HEADLESS_LOG_FH is not None or threading.current_thread() is not threading.main_thread():
        was_headless = _HEADLESS_LOG_FH is not None
        for fh in (_HEADLESS_LOG_FH, _CONSOLE_TEE_LOG_FH):
            if fh is not None:
                with contextlib.suppress(Exception):
                    fh.flush()
                    fh.close()
        exe_dir = Path(sys.executable).resolve().parent
        py_console = exe_dir / "python.exe"
        helper_py = str(py_console if py_console.is_file() else Path(sys.executable).resolve())
        target_args = [sys.executable, script_path, *sys.argv[1:]]
        if was_headless and "--background" not in target_args:
            target_args.append("--background")
        flags = _WIN_NO_WINDOW | getattr(subprocess, "DETACHED_PROCESS", 0)
        helper_code = (
            "import subprocess, time; "
            "time.sleep(1.2); "
            f"subprocess.Popen({target_args!r}, cwd={str(ROOT)!r}, "
            "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, "
            f"creationflags={flags})"
        )
        env = os.environ.copy()
        if was_headless:
            env["KRAFEN_HEADLESS"] = "1"
        subprocess.Popen(
            [helper_py, "-c", helper_code],
            cwd=str(ROOT),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )
        os._exit(0)
    else:
        code = subprocess.call([sys.executable, script_path, *sys.argv[1:]])
        raise SystemExit(code)


def _configure_stdio_utf8(*, mirror_console_to_log: bool = False) -> None:
    global _HEADLESS_LOG_FH, _CONSOLE_TEE_LOG_FH
    if _is_headless_process():
        if _HEADLESS_LOG_FH is None:
            try:
                _HEADLESS_LOG_FH = _open_rotating_server_log()
                sys.stdout = _HEADLESS_LOG_FH
                sys.stderr = _HEADLESS_LOG_FH
            except OSError:
                pass
        return

    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            with contextlib.suppress(Exception):
                _stream.reconfigure(encoding="utf-8", errors="replace")

    if mirror_console_to_log and _CONSOLE_TEE_LOG_FH is None and not _has_cli_mgmt_flag():
        try:
            _CONSOLE_TEE_LOG_FH = _open_rotating_server_log()
            if sys.stdout is not None and not isinstance(sys.stdout, _TeeLogStream):
                sys.stdout = _TeeLogStream(sys.stdout, _CONSOLE_TEE_LOG_FH)
            if sys.stderr is not None and not isinstance(sys.stderr, _TeeLogStream):
                sys.stderr = _TeeLogStream(sys.stderr, _CONSOLE_TEE_LOG_FH)
        except OSError:
            pass


def _is_local_port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.6):
            return True
    except OSError:
        return False


def _read_pid_file() -> int | None:
    try:
        if PID_FILE_PATH.is_file():
            val = int(PID_FILE_PATH.read_text(encoding="utf-8").strip())
            return val if val > 0 else None
    except Exception:
        pass
    return None


def _stop_running_server() -> bool:
    pid = _read_pid_file()
    if not pid or pid == os.getpid():
        return False
    stopped = False
    if os.name == "nt":
        # Безопасно проверяем, что этот PID действительно python/pythonw
        chk = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            creationflags=_WIN_NO_WINDOW,
            check=False,
        )
        out_lower = (chk.stdout or "").lower()
        if "python" in out_lower or "py.exe" in out_lower or "pyw.exe" in out_lower:
            res = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                creationflags=_WIN_NO_WINDOW,
                check=False,
            )
            stopped = res.returncode == 0
    else:
        try:
            os.kill(pid, 15)
            stopped = True
        except OSError:
            pass
    if stopped:
        with contextlib.suppress(OSError):
            PID_FILE_PATH.unlink(missing_ok=True)
        for _ in range(20):
            if not _is_local_port_open(PORT):
                break
            time.sleep(0.15)
    return stopped


def _spawn_background_server() -> Path:
    exe_dir = Path(sys.executable).resolve().parent
    pythonw = exe_dir / "pythonw.exe"
    launcher_exe = pythonw if pythonw.is_file() else Path(sys.executable).resolve()
    script_path = (ROOT / "server.py").resolve()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    log_path = DATA_DIR / "server.log"
    if not log_path.exists():
        with contextlib.suppress(OSError):
            log_path.touch()
    env = os.environ.copy()
    env["KRAFEN_HEADLESS"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    flags = _WIN_NO_WINDOW | getattr(subprocess, "DETACHED_PROCESS", 0)
    subprocess.Popen(
        [str(launcher_exe), str(script_path), "--background"],
        cwd=str(ROOT),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
    )
    for _ in range(30):
        if _is_local_port_open(PORT):
            break
        time.sleep(0.1)
    return launcher_exe


def _manage_autostart_cli(action: str) -> int:
    if action == "stop":
        if _stop_running_server():
            print("Фоновый сервер (и автозапись) остановлены.")
        elif _is_local_port_open(PORT):
            print(f"Порт {PORT} занят другим окном/процессом (PID-файл не найден) — закройте открытое окно терминала.")
        else:
            print("Сервер сейчас не запущен.")
        return 0

    if action == "status":
        running = _is_local_port_open(PORT)
        pid = _read_pid_file()
        reg_val = None
        if os.name == "nt":
            import winreg

            with contextlib.suppress(FileNotFoundError, OSError):
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _AUTOSTART_REG_PATH, 0, winreg.KEY_READ) as key:
                    reg_val, _ = winreg.QueryValueEx(key, _AUTOSTART_REG_NAME)
        print(f"Автозапуск Windows: {'ВКЛЮЧЁН (' + str(reg_val) + ')' if reg_val else 'выключен'}")
        print(f"Статус сервера (порт {PORT}): {'РАБОТАЕТ' + (f' (PID {pid})' if pid else '') if running else 'не запущен'}")
        log_path = DATA_DIR / "server.log"
        if log_path.is_file():
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-15:]
            if lines:
                print(f"\nПоследние строки {log_path}:")
                for ln in lines:
                    print(f"  {ln}")
        return 0

    if os.name != "nt":
        print("На Linux используйте systemd-юнит krafen.service: sudo systemctl enable --now krafen")
        return 0

    import winreg  # стандартная библиотека Windows Python

    exe_dir = Path(sys.executable).resolve().parent
    pythonw = exe_dir / "pythonw.exe"
    launcher_exe = pythonw if pythonw.is_file() else Path(sys.executable).resolve()
    script_path = (ROOT / "server.py").resolve()
    cmd_str = f'"{launcher_exe}" "{script_path}" --background'

    if action == "restart":
        # Если запись в автозагрузке уже есть — обновим команду до актуальной (--background)
        with contextlib.suppress(FileNotFoundError, OSError):
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _AUTOSTART_REG_PATH, 0, winreg.KEY_READ) as rkey:
                winreg.QueryValueEx(rkey, _AUTOSTART_REG_NAME)
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _AUTOSTART_REG_PATH, 0, winreg.KEY_SET_VALUE) as wkey:
                winreg.SetValueEx(wkey, _AUTOSTART_REG_NAME, 0, winreg.REG_SZ, cmd_str)
        was_stopped = _stop_running_server()
        if not _is_local_port_open(PORT):
            _spawn_background_server()
            state_word = "перезапущен" if was_stopped else "запущен"
            print(f"Фоновый сервер {state_word} на порту {PORT}.")
            print(f"Логи пишутся в: {DATA_DIR / 'server.log'}")
        else:
            print(f"Порт {PORT} занят открытым окном терминала — закройте его и повторите команду.")
        return 0

    if action == "install":
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _AUTOSTART_REG_PATH, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, _AUTOSTART_REG_NAME, 0, winreg.REG_SZ, cmd_str)
        print(f"Автозапуск включён в Windows (HKCU\\...\\Run -> {_AUTOSTART_REG_NAME}):")
        print(f"  {cmd_str}")
        print(f"Логи фонового режима пишутся в: {DATA_DIR / 'server.log'}")
        was_stopped = _stop_running_server()
        if not _is_local_port_open(PORT):
            _spawn_background_server()
            if was_stopped:
                print(f"Фоновый сервер перезапущен на порту {PORT}.")
            else:
                print(f"Сервер сразу запущен в фоне (без окна) на порту {PORT}.")
        else:
            print(
                f"Сервер сейчас уже запущен на порту {PORT} в открытом окне консоли. "
                "Закройте то окно и выполните py server.py --restart, чтобы перевести работу в тихий фоновый режим."
            )
        print("Управление: py server.py --status | py server.py --restart | py server.py --stop | py server.py --uninstall-autostart")
        return 0

    if action == "uninstall":
        removed = False
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _AUTOSTART_REG_PATH, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, _AUTOSTART_REG_NAME)
                removed = True
        except FileNotFoundError:
            pass
        stopped = _stop_running_server()
        print("Автозапуск отключён." if removed else "Запись автозапуска не найдена (уже отключена).")
        if stopped:
            print("Фоновый сервер остановлен.")
        return 0

    return 1


_startup_auto_update_done = False


def _maybe_run_startup_auto_update() -> None:
    global _startup_auto_update_done
    if _startup_auto_update_done:
        return
    _startup_auto_update_done = True
    if not is_auto_update_enabled():
        return
    if os.environ.get("_KRAFEN_REEXEC") == "1":
        os.environ.pop("_KRAFEN_REEXEC", None)
        return
    upd = perform_github_update(force=False, allow_zip_fallback_on_api_error=True)
    if upd.get("ok") and upd.get("server_changed"):
        _reexec_current_process()


def _cf_api_call(method: str, url: str, auth_headers: dict[str, str], payload: dict[str, Any] | None = None) -> dict[str, Any]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "krafen-cf-helper/1.0",
        **auth_headers,
    }
    raw_body = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=raw_body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            return json.loads(body)
        except Exception:
            return {"success": False, "errors": [{"code": exc.code, "message": body[:200]}]}
    except Exception as exc:
        return {"success": False, "errors": [{"message": str(exc)}]}


def _disable_cloudflare_ech_cli(extra_args: list[str]) -> int:
    """
    Отключает TLS ECH (Encrypted Client Hello) и включает HTTP/3 (QUIC) + 0-RTT
    в бесплатной зоне Cloudflare через официальный API v4 (для обхода блокировки ECH на ТСПУ в РФ).
    """
    print("=== Настройка зоны Cloudflare для работы из РФ (отключение TLS ECH + включение HTTP/3) ===")
    domain = (
        os.environ.get("CF_DOMAIN")
        or (urlsplit(PUBLIC_ORIGIN).hostname if PUBLIC_ORIGIN else "")
        or "krafen.me"
    ).strip().lower()
    zone_id = (os.environ.get("CF_ZONE_ID") or "").strip()
    api_token = (os.environ.get("CF_API_TOKEN") or "").strip()
    cf_email = (os.environ.get("CF_EMAIL") or "").strip()
    cf_global_key = (os.environ.get("CF_GLOBAL_KEY") or "").strip()

    pos = [a.strip() for a in extra_args if a.strip() and not a.startswith("-")]
    if len(pos) == 1:
        api_token = pos[0]
    elif len(pos) >= 2:
        if "@" in pos[0]:
            cf_email, cf_global_key = pos[0], pos[1]
            if len(pos) >= 3:
                domain = pos[2].lower()
        else:
            zone_id, api_token = pos[0], pos[1]

    if not api_token and not (cf_email and cf_global_key):
        print(f"Домен: {domain}")
        print("В бесплатном тарифе Cloudflare переключатель ECH в браузере закрыт, но через API он отключается бесплатно.")
        print("Где взять ключ (любой из двух вариантов за 15 секунд):")
        print("  Вариант А (проще всего): Cloudflare → My Profile → API Tokens → Global API Key → View")
        print("  Вариант Б: API Token с правами Zone → Zone Settings → Edit и Zone → SSL and Certificates → Edit\n")
        try:
            entered = input("Введите API Token ИЛИ ваш Email от Cloudflare: ").strip()
        except EOFError:
            entered = ""
        if not entered:
            print("Отменено: ключ не указан.")
            return 1
        if "@" in entered:
            cf_email = entered
            try:
                cf_global_key = input("Введите Global API Key: ").strip()
            except EOFError:
                cf_global_key = ""
        else:
            api_token = entered

    if cf_email and cf_global_key:
        auth_headers = {"X-Auth-Email": cf_email, "X-Auth-Key": cf_global_key}
    elif api_token:
        auth_headers = {"Authorization": f"Bearer {api_token}"}
    else:
        print("Ошибка: не указан API Token или пара Email + Global API Key.")
        return 1

    if not zone_id:
        z_resp = _cf_api_call("GET", f"https://api.cloudflare.com/client/v4/zones?name={quote(domain)}", auth_headers)
        zones = z_resp.get("result") if isinstance(z_resp.get("result"), list) else []
        if not zones:
            z_all = _cf_api_call("GET", "https://api.cloudflare.com/client/v4/zones?per_page=50", auth_headers)
            all_list = z_all.get("result") if isinstance(z_all.get("result"), list) else []
            for z in all_list:
                if isinstance(z, dict) and str(z.get("name") or "").lower() == domain:
                    zones = [z]
                    break
            if not zones and len(all_list) == 1:
                zones = all_list
        if not zones:
            errs = z_resp.get("errors") or []
            print(f"Не удалось найти зону {domain} в Cloudflare: {errs or 'проверьте права ключа'}")
            return 1
        zone_id = str(zones[0].get("id") or "").strip()
        domain = str(zones[0].get("name") or domain)

    print(f"Зона найдена: {domain} ({zone_id})")
    settings_to_apply = [
        ("ech", "off", "TLS ECH (Encrypted Client Hello)"),
        ("rocket_loader", "off", "Rocket Loader (совместимость со строгим CSP nonce)"),
        ("email_obfuscation", "off", "Email Obfuscation (защита целостности скриптов)"),
        ("http3", "on", "HTTP/3 (QUIC поверх UDP)"),
        ("0rtt", "on", "0-RTT Connection Resumption"),
    ]
    ech_ok = False
    for setting_id, target_val, label in settings_to_apply:
        res = _cf_api_call(
            "PATCH",
            f"https://api.cloudflare.com/client/v4/zones/{zone_id}/settings/{setting_id}",
            auth_headers,
            {"id": setting_id, "value": target_val},
        )
        if res.get("success"):
            actual = (res.get("result") or {}).get("value", target_val)
            print(f"  [OK] {label}: {actual}")
            if setting_id == "ech" and actual == "off":
                ech_ok = True
        else:
            errs = res.get("errors") or []
            print(f"  [!] {label}: не удалось изменить ({errs})")

    if ech_ok:
        print("\nГотово! ECH отключён. Запись HTTPS в DNS обновится за 1–3 минуты.")
        print("Чтобы проверить сразу без ожидания кэша браузера — откройте сайт в режиме Инкогнито")
        print("или очистите DNS-кэш браузера (chrome://net-internals/#dns → Clear host cache).")
        return 0
    print("\nВнимание: параметр ECH не изменился. Попробуйте использовать Global API Key (Email + Global API Key).")
    return 1


if __name__ == "__main__":
    with contextlib.suppress(OSError):
        os.chdir(ROOT)
    _configure_stdio_utf8()
    if "--disable-cf-ech" in sys.argv[1:]:
        _idx = sys.argv.index("--disable-cf-ech")
        raise SystemExit(_disable_cloudflare_ech_cli(sys.argv[_idx + 1 :]))
    for _fast_flag in ("--stop", "--status", "--uninstall-autostart"):
        if _fast_flag in sys.argv[1:]:
            raise SystemExit(_manage_autostart_cli(_CLI_MGMT_FLAGS[_fast_flag]))
    _maybe_run_startup_auto_update()
    for _upd_flag in ("--install-autostart", "--restart"):
        if _upd_flag in sys.argv[1:]:
            raise SystemExit(_manage_autostart_cli(_CLI_MGMT_FLAGS[_upd_flag]))


@contextlib.asynccontextmanager
async def _app_lifespan(_app: FastAPI):
    _maybe_run_startup_auto_update()
    if sys.platform == "win32":
        loop = asyncio.get_running_loop()
        prev_handler = loop.get_exception_handler()

        def _win32_loop_exception_handler(lp: asyncio.AbstractEventLoop, ctx: dict[str, Any]) -> None:
            exc = ctx.get("exception")
            if isinstance(exc, ConnectionResetError) or (
                isinstance(exc, OSError) and getattr(exc, "winerror", None) in (10053, 10054)
            ):
                return
            if prev_handler is not None:
                prev_handler(lp, ctx)
            else:
                lp.default_exception_handler(ctx)

        loop.set_exception_handler(_win32_loop_exception_handler)
    _start_analytics_flusher()
    _geo_startup_warnings()
    _start_ipinfo_updater()
    _start_drive_vods_poller()
    try:
        yield
    finally:
        with contextlib.suppress(Exception):
            flush_analytics_buffer()


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=_app_lifespan)

VALID_TYPES = {"anime", "movie", "series"}
VALID_STATUSES = {"watched", "planned", "dropped"}
VALID_GAME_STATUSES = {"completed", "playing", "dropped", "wishlist", "paused"}
VALID_SETUP_SECTIONS = {"pc", "gear"}
VALID_SETUP_ICONS = {
    "cpu", "gpu", "ram", "board", "ssd", "hdd", "cooler", "psu", "case",
    "audio", "headphones", "monitor", "keyboard", "mouse", "mic", "chair", "other",
}
VALID_WISH_CATEGORIES = {"hardware", "peripheral", "other"}
VALID_WISH_STATUSES = {"want", "saving", "bought"}
VALID_CURRENCIES = {"RUB", "USD", "EUR"}
VALID_WISH_PRIORITIES = {1, 2, 3}
ITEM_ID_PATTERN = re.compile(r"^[A-Za-z0-9:_-]{1,120}$")

# Расширения коллекций: теги, избранное, оценки и рецензии watchlist
MAX_TAGS = 12
MAX_TAG_LEN = 28
MAX_WATCHLIST_RATING = 10

MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".xml": "application/xml; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
    ".mov": "video/quicktime",
    ".jsonl": "application/x-ndjson; charset=utf-8",
}

LIST_SPECS = {
    "watchlist": {
        "legacy_file": "watchlist.json",
        "normalizer": "watchlist",
        "max_items": 500,
        "cache_ttl": 300,
    },
    "gamelist": {
        "legacy_file": "gamelist.json",
        "normalizer": "gamelist",
        "max_items": 500,
        "cache_ttl": 300,
    },
    "setup": {
        "legacy_file": "setup.json",
        "normalizer": "setup",
        "max_items": 120,
        "cache_ttl": 300,
    },
    "wishlist": {
        "legacy_file": "wishlist.json",
        "normalizer": "wishlist",
        "max_items": 500,
        "cache_ttl": 300,
    },
}

_db_lock = threading.Lock()
_list_cache: dict[str, dict[str, Any]] = {}
_api_cache_lock = threading.Lock()
_api_cache: dict[str, tuple[float, int, dict[str, Any]]] = {}
_rate_lock = threading.Lock()
_rate_buckets: dict[str, list[float]] = {}
_twitch_token: dict[str, Any] = {}
_igdb_token: dict[str, Any] = {}


# ----------------------------- SQLite хранилище -----------------------------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS collection_meta (
    name TEXT PRIMARY KEY,
    revision INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS watchlist_items (
    pos INTEGER NOT NULL,
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    title_ru TEXT,
    year TEXT NOT NULL,
    type TEXT NOT NULL,
    status TEXT NOT NULL,
    poster TEXT,
    rating INTEGER NOT NULL DEFAULT 0,
    review TEXT NOT NULL DEFAULT '',
    tags_json TEXT NOT NULL DEFAULT '[]',
    favorite INTEGER NOT NULL DEFAULT 0,
    added_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS gamelist_items (
    pos INTEGER NOT NULL,
    id TEXT PRIMARY KEY,
    igdb_id INTEGER,
    title TEXT NOT NULL,
    year TEXT NOT NULL,
    status TEXT NOT NULL,
    rating REAL NOT NULL DEFAULT 0,
    review TEXT NOT NULL DEFAULT '',
    cover TEXT,
    genres_json TEXT NOT NULL DEFAULT '[]',
    platforms_json TEXT NOT NULL DEFAULT '[]',
    category INTEGER NOT NULL DEFAULT 0,
    tags_json TEXT NOT NULL DEFAULT '[]',
    favorite INTEGER NOT NULL DEFAULT 0,
    added_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS setup_items (
    pos INTEGER NOT NULL,
    id TEXT PRIMARY KEY,
    item_group TEXT NOT NULL,
    category TEXT NOT NULL,
    name TEXT NOT NULL,
    icon TEXT NOT NULL DEFAULT 'other',
    note TEXT NOT NULL DEFAULT '',
    link TEXT NOT NULL DEFAULT '',
    sort_order INTEGER NOT NULL DEFAULT 0,
    added_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS wishlist_items (
    pos INTEGER NOT NULL,
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    category TEXT NOT NULL,
    status TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 2,
    price REAL NOT NULL DEFAULT 0,
    currency TEXT NOT NULL DEFAULT 'RUB',
    note TEXT NOT NULL DEFAULT '',
    link TEXT NOT NULL DEFAULT '',
    image TEXT NOT NULL DEFAULT '',
    tags_json TEXT NOT NULL DEFAULT '[]',
    favorite INTEGER NOT NULL DEFAULT 0,
    added_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS drive_vods (
    id TEXT PRIMARY KEY,
    drive_id TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    size_label TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    sort_key TEXT NOT NULL,
    folder_drive_id TEXT NOT NULL DEFAULT '',
    chat_drive_id TEXT NOT NULL DEFAULT '',
    thumb_drive_id TEXT NOT NULL DEFAULT '',
    meta_drive_id TEXT NOT NULL DEFAULT '',
    game TEXT NOT NULL DEFAULT '',
    duration_s INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL DEFAULT ''
);
"""


@contextlib.contextmanager
def db_connect():
    conn = sqlite3.connect(DB_PATH, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    try:
        yield conn
    finally:
        conn.close()


@contextlib.contextmanager
def auth_db_connect():
    is_new = not AUTH_DB_PATH.exists()
    conn = sqlite3.connect(AUTH_DB_PATH, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    if is_new:
        try:
            os.chmod(AUTH_DB_PATH, 0o600)
        except OSError:
            pass
    try:
        yield conn
    finally:
        conn.close()


def _normalize_legacy_list(value: Any) -> dict[str, Any]:
    if isinstance(value, list):
        return {"revision": 0, "items": value}
    if isinstance(value, dict) and isinstance(value.get("items"), list):
        rev = value.get("revision")
        if not isinstance(rev, int) or rev < 0:
            rev = 0
        return {"revision": rev, "items": value["items"]}
    return {"revision": 0, "items": []}


def _load_tags(raw: Any) -> list[str]:
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return []
    if not isinstance(value, list):
        return []
    return [str(t) for t in value if isinstance(t, str) and t.strip()][:MAX_TAGS]


def _select_collection_items(conn: sqlite3.Connection, name: str) -> list[dict[str, Any]]:
    if name == "watchlist":
        rows = conn.execute("SELECT * FROM watchlist_items ORDER BY pos ASC").fetchall()
        return [
            {
                "id": r["id"],
                "title": r["title"],
                "titleRu": r["title_ru"] or None,
                "year": r["year"],
                "type": r["type"],
                "status": r["status"],
                "poster": r["poster"] or None,
                "rating": int(r["rating"] or 0),
                "review": r["review"] or "",
                "tags": _load_tags(r["tags_json"]),
                "favorite": bool(r["favorite"]),
                "addedAt": int(r["added_at"]),
                "updatedAt": int(r["updated_at"]),
            }
            for r in rows
        ]
    if name == "gamelist":
        rows = conn.execute("SELECT * FROM gamelist_items ORDER BY pos ASC").fetchall()
        items: list[dict[str, Any]] = []
        for r in rows:
            try:
                genres = json.loads(r["genres_json"])
            except Exception:
                genres = []
            try:
                platforms = json.loads(r["platforms_json"])
            except Exception:
                platforms = []
            items.append(
                {
                    "id": r["id"],
                    "igdbId": r["igdb_id"],
                    "title": r["title"],
                    "year": r["year"],
                    "status": r["status"],
                    "rating": float(r["rating"]),
                    "review": r["review"] or "",
                    "cover": r["cover"] or None,
                    "genres": genres if isinstance(genres, list) else [],
                    "platforms": platforms if isinstance(platforms, list) else [],
                    "category": int(r["category"] or 0),
                    "tags": _load_tags(r["tags_json"]),
                    "favorite": bool(r["favorite"]),
                    "addedAt": int(r["added_at"]),
                    "updatedAt": int(r["updated_at"]),
                }
            )
        return items
    if name == "setup":
        rows = conn.execute("SELECT * FROM setup_items ORDER BY pos ASC").fetchall()
        return [
            {
                "id": r["id"],
                "group": r["item_group"],
                "category": r["category"],
                "name": r["name"],
                "icon": r["icon"],
                "note": r["note"] or "",
                "link": r["link"] or "",
                "sort": int(r["sort_order"]),
                "addedAt": int(r["added_at"]),
                "updatedAt": int(r["updated_at"]),
            }
            for r in rows
        ]
    if name == "wishlist":
        rows = conn.execute("SELECT * FROM wishlist_items ORDER BY pos ASC").fetchall()
        return [
            {
                "id": r["id"],
                "title": r["title"],
                "category": r["category"],
                "status": r["status"],
                "priority": int(r["priority"]),
                "price": float(r["price"]) if float(r["price"] or 0) > 0 else 0,
                "currency": r["currency"],
                "note": r["note"] or "",
                "link": r["link"] or "",
                "image": r["image"] or "",
                "tags": _load_tags(r["tags_json"]),
                "favorite": bool(r["favorite"]),
                "addedAt": int(r["added_at"]),
                "updatedAt": int(r["updated_at"]),
            }
            for r in rows
        ]
    return []


def _write_collection_items(conn: sqlite3.Connection, name: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen_ids: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for it in items:
        iid = str(it.get("id") or "")
        if not iid or iid in seen_ids:
            continue
        seen_ids.add(iid)
        deduped.append(it)

    if name == "watchlist":
        conn.execute("DELETE FROM watchlist_items")
        conn.executemany(
            """
            INSERT INTO watchlist_items (
                pos, id, title, title_ru, year, type, status, poster,
                rating, review, tags_json, favorite, added_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    pos,
                    it["id"],
                    it["title"],
                    it.get("titleRu"),
                    it.get("year") or "-",
                    it["type"],
                    it["status"],
                    it.get("poster"),
                    max(0, min(10, int(it.get("rating") or 0))),
                    it.get("review") or "",
                    json.dumps(it.get("tags") or [], ensure_ascii=False, separators=(",", ":")),
                    1 if it.get("favorite") else 0,
                    int(it.get("addedAt") or 0),
                    int(it.get("updatedAt") or 0),
                )
                for pos, it in enumerate(deduped)
            ],
        )
    elif name == "gamelist":
        conn.execute("DELETE FROM gamelist_items")
        conn.executemany(
            """
            INSERT INTO gamelist_items (
                pos, id, igdb_id, title, year, status, rating, review, cover,
                genres_json, platforms_json, category, tags_json, favorite, added_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    pos,
                    it["id"],
                    it.get("igdbId"),
                    it["title"],
                    it.get("year") or "-",
                    it["status"],
                    float(it.get("rating") or 0),
                    it.get("review") or "",
                    it.get("cover"),
                    json.dumps(it.get("genres") or [], ensure_ascii=False, separators=(",", ":")),
                    json.dumps(it.get("platforms") or [], ensure_ascii=False, separators=(",", ":")),
                    int(it.get("category") or 0),
                    json.dumps(it.get("tags") or [], ensure_ascii=False, separators=(",", ":")),
                    1 if it.get("favorite") else 0,
                    int(it.get("addedAt") or 0),
                    int(it.get("updatedAt") or 0),
                )
                for pos, it in enumerate(deduped)
            ],
        )
    elif name == "setup":
        conn.execute("DELETE FROM setup_items")
        conn.executemany(
            """
            INSERT INTO setup_items (pos, id, item_group, category, name, icon, note, link, sort_order, added_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    pos,
                    it["id"],
                    it.get("group") or "pc",
                    it["category"],
                    it["name"],
                    it.get("icon") or "other",
                    it.get("note") or "",
                    it.get("link") or "",
                    int(it.get("sort") or 0),
                    int(it.get("addedAt") or 0),
                    int(it.get("updatedAt") or 0),
                )
                for pos, it in enumerate(deduped)
            ],
        )
    elif name == "wishlist":
        conn.execute("DELETE FROM wishlist_items")
        conn.executemany(
            """
            INSERT INTO wishlist_items (
                pos, id, title, category, status, priority, price, currency, note, link, image,
                tags_json, favorite, added_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    pos,
                    it["id"],
                    it["title"],
                    it["category"],
                    it["status"],
                    int(it.get("priority") or 2),
                    float(it.get("price") or 0),
                    it.get("currency") or "RUB",
                    it.get("note") or "",
                    it.get("link") or "",
                    it.get("image") or "",
                    json.dumps(it.get("tags") or [], ensure_ascii=False, separators=(",", ":")),
                    1 if it.get("favorite") else 0,
                    int(it.get("addedAt") or 0),
                    int(it.get("updatedAt") or 0),
                )
                for pos, it in enumerate(deduped)
            ],
        )
    return deduped


def kv_get(key: str, default: Any = None) -> Any:
    with _db_lock, auth_db_connect() as conn:
        row = conn.execute("SELECT value_json FROM kv WHERE key = ?", (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row["value_json"])
        except Exception:
            return default


def kv_set(key: str, value: Any) -> None:
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    now = int(time.time())
    with _db_lock, auth_db_connect() as conn:
        conn.execute(
            """
            INSERT INTO kv (key, value_json, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at
            """,
            (key, payload, now),
        )


def init_auth_db() -> None:
    now = int(time.time())
    with _db_lock, auth_db_connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS kv (
                key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """
        )
        legacy_tokens = DATA_DIR / "tokens.json"
        if legacy_tokens.is_file():
            existing = conn.execute("SELECT 1 FROM kv WHERE key = 'tokens'").fetchone()
            if not existing:
                try:
                    parsed = json.loads(legacy_tokens.read_text(encoding="utf-8"))
                    if isinstance(parsed, dict):
                        conn.execute(
                            "INSERT INTO kv (key, value_json, updated_at) VALUES ('tokens', ?, ?)",
                            (json.dumps(parsed, ensure_ascii=False, separators=(",", ":")), now),
                        )
                except Exception:
                    pass


init_auth_db()


def resolve_secret_key() -> str:
    env_secret = os.environ.get("SECRET_KEY", "").strip()
    if env_secret:
        return env_secret
    stored = kv_get("session_secret")
    if isinstance(stored, str) and len(stored) >= 32:
        return stored
    generated = secrets.token_hex(32)
    kv_set("session_secret", generated)
    return generated


SECRET_KEY = resolve_secret_key()


def read_list(name: str, use_cache: bool = True) -> dict[str, Any]:
    if use_cache:
        cached = _list_cache.get(name)
        if cached and cached["ts"] > time.time() - LIST_SPECS[name]["cache_ttl"]:
            return {"revision": cached["revision"], "items": list(cached["items"])}
    with _db_lock, db_connect() as conn:
        row = conn.execute("SELECT revision FROM collection_meta WHERE name = ?", (name,)).fetchone()
        rev = max(0, int(row["revision"] or 0)) if row else 0
        items = _select_collection_items(conn, name)
        state = {"revision": rev, "items": items}
        _list_cache[name] = {"ts": time.time(), "revision": rev, "items": list(items)}
        return state


def update_list_atomic(name: str, expected_revision: int, items: list[dict[str, Any]]) -> tuple[bool, dict[str, Any]]:
    now = int(time.time())
    with _db_lock, db_connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT revision FROM collection_meta WHERE name = ?", (name,)).fetchone()
            current_rev = max(0, int(row["revision"] or 0)) if row else 0
            if expected_revision != current_rev:
                current_items = _select_collection_items(conn, name)
                conn.execute("ROLLBACK")
                state = {"revision": current_rev, "items": current_items}
                _list_cache[name] = {"ts": time.time(), "revision": current_rev, "items": list(current_items)}
                return False, state

            next_rev = current_rev + 1
            stored_items = _write_collection_items(conn, name, items)
            conn.execute(
                """
                INSERT INTO collection_meta (name, revision, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    revision = excluded.revision,
                    updated_at = excluded.updated_at
                """,
                (name, next_rev, now),
            )
            conn.execute("COMMIT")
            state = {"revision": next_rev, "items": stored_items}
            _list_cache[name] = {"ts": time.time(), "revision": next_rev, "items": list(stored_items)}
            return True, state
        except Exception:
            conn.execute("ROLLBACK")
            raise


def read_tokens() -> dict[str, Any]:
    val = kv_get("tokens", {})
    return val if isinstance(val, dict) else {}


def write_tokens(value: dict[str, Any]) -> None:
    kv_set("tokens", value)


# ----------------------------- утилиты и безопасность -----------------------------


def text(value: Any, max_len: int = 200) -> str:
    return str(value or "").strip()[:max_len]


def clean_url(value: Any, max_len: int = 600) -> str | None:
    url = text(value, max_len)
    if not url or not url.startswith("https://"):
        return None
    if any(ch in url for ch in ("\r", "\n", "\t", " ", "<", ">", '"', "'")):
        return None
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.netloc:
        return None
    return url


def year_from(value: Any) -> str:
    match = re.search(r"\d{4}", str(value or ""))
    return match.group(0) if match else "-"


def year_from_unix(value: Any) -> str:
    try:
        ts = int(value)
        if ts <= 0:
            return "-"
        return str(time.gmtime(ts).tm_year)
    except Exception:
        return "-"


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _session_hmac_key() -> bytes:
    pwd_fp = hashlib.sha256(ADMIN_PASSWORD.encode("utf-8")).hexdigest() if ADMIN_PASSWORD else ""
    return f"{SECRET_KEY}:{pwd_fp}".encode("utf-8")


def sign(value: str) -> str:
    return b64url(hmac.new(_session_hmac_key(), value.encode(), hashlib.sha256).digest())


def constant_time_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


_revoked_sessions_lock = threading.Lock()
_revoked_sessions: dict[str, int] = {}


def create_session() -> str:
    payload = b64url(
        json.dumps(
            {
                "exp": int(time.time()) + SESSION_TTL_SECONDS,
                "nonce": secrets.token_hex(16),
            },
            separators=(",", ":"),
        ).encode()
    )
    return f"{payload}.{sign(payload)}"


def verify_session(token: str | None) -> bool:
    if not token or not ADMIN_PASSWORD or len(token) > 512:
        return False
    try:
        payload, signature = token.split(".", 1)
        if not constant_time_equal(signature, sign(payload)):
            return False
        data = json.loads(b64url_decode(payload))
        now = int(time.time())
        exp = int(data.get("exp", 0))
        nonce = str(data.get("nonce") or "")
        if exp <= now or not nonce:
            return False
        with _revoked_sessions_lock:
            if nonce in _revoked_sessions:
                return False
        return True
    except Exception:
        return False


def revoke_session(token: str | None) -> None:
    if not token or len(token) > 512 or "." not in token:
        return
    try:
        payload, signature = token.split(".", 1)
        if not constant_time_equal(signature, sign(payload)):
            return
        data = json.loads(b64url_decode(payload))
        nonce = str(data.get("nonce") or "")
        exp = int(data.get("exp", 0))
        now = int(time.time())
        if not nonce or exp <= now:
            return
        with _revoked_sessions_lock:
            expired = [k for k, v in _revoked_sessions.items() if v <= now]
            for k in expired:
                _revoked_sessions.pop(k, None)
            if len(_revoked_sessions) >= 2048:
                _revoked_sessions.pop(next(iter(_revoked_sessions)))
            _revoked_sessions[nonce] = exp
    except Exception:
        return


def require_admin(request: Request) -> None:
    if not verify_session(request.cookies.get(SESSION_COOKIE)):
        raise HTTPException(status_code=401, detail={"error": "unauthorized"})


_CLOUDFLARE_NETS = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "173.245.48.0/20",
        "103.21.244.0/22",
        "103.22.200.0/22",
        "103.31.4.0/22",
        "141.101.64.0/18",
        "108.162.192.0/18",
        "190.93.240.0/20",
        "188.114.96.0/20",
        "197.234.240.0/22",
        "198.41.128.0/17",
        "162.158.0.0/15",
        "104.16.0.0/13",
        "104.24.0.0/14",
        "172.64.0.0/13",
        "131.0.72.0/22",
        "2400:cb00::/32",
        "2606:4700::/32",
        "2803:f800::/32",
        "2405:b500::/32",
        "2405:8100::/32",
        "2a06:98c0::/29",
        "2c0f:f248::/32",
    )
)


def _parse_extra_trusted_nets(raw: str) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    if not raw or raw in {"0", "1", "true", "false", "yes", "no", "on", "off", "auto"}:
        return ()
    nets: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for part in raw.split(","):
        item = part.strip()
        if not item:
            continue
        with contextlib.suppress(ValueError):
            nets.append(ipaddress.ip_network(item, strict=False))
    return tuple(nets)


_EXTRA_TRUSTED_PROXY_NETS = _parse_extra_trusted_nets(TRUST_PROXY_MODE)


def _is_trusted_proxy_peer(request: Request) -> bool:
    if TRUST_PROXY_MODE in {"0", "false", "no", "off"}:
        return False
    if TRUST_PROXY_MODE in {"1", "true", "yes", "on"}:
        return True
    peer = request.client.host if request.client else ""
    if not peer:
        return False
    if peer in {"testclient", "localhost"}:
        return True
    try:
        ip = ipaddress.ip_address(peer)
        if _EXTRA_TRUSTED_PROXY_NETS:
            return any(ip in net for net in _EXTRA_TRUSTED_PROXY_NETS) or ip.is_loopback
        if ip.is_loopback or ip.is_private or ip.is_link_local:
            return True
        return any(ip in net for net in _CLOUDFLARE_NETS)
    except ValueError:
        return False


_SAFE_HOST_RE = re.compile(r"^[A-Za-z0-9.-]{1,253}(?::\d{1,5})?$")


def _effective_request_host(request: Request) -> str:
    candidates: list[str] = []
    if _is_trusted_proxy_peer(request):
        xfh = (request.headers.get("x-forwarded-host") or "").split(",")[0].strip()
        if xfh:
            candidates.append(xfh)
    raw_host = (request.headers.get("host") or request.url.netloc or "").split(",")[0].strip()
    if raw_host:
        candidates.append(raw_host)
    for h in candidates:
        if _SAFE_HOST_RE.fullmatch(h):
            return h.lower()
    return f"localhost:{PORT}"


def _is_e2b_preview_request(request: Request) -> bool:
    host_only = _effective_request_host(request).split(":", 1)[0]
    return host_only.endswith(".e2b.app")


def is_https_request(request: Request) -> bool:
    if _is_trusted_proxy_peer(request):
        proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
        if proto:
            return proto == "https"
    return request.url.scheme == "https"


def require_same_origin(request: Request) -> None:
    """Защита от CSRF для state-changing запросов."""
    allow_e2b_iframe = _is_e2b_preview_request(request)
    sec_site = (request.headers.get("sec-fetch-site") or "").strip().lower()
    if sec_site and sec_site not in ("same-origin", "none") and not allow_e2b_iframe:
        raise HTTPException(status_code=403, detail={"error": "csrf_blocked"})

    origin = (request.headers.get("origin") or "").strip()
    if origin and origin != "null":
        parsed_origin = urlsplit(origin)
        origin_host = (parsed_origin.netloc or "").strip().lower()
        allowed_hosts = {_effective_request_host(request)}
        if PUBLIC_ORIGIN:
            pub_host = (urlsplit(PUBLIC_ORIGIN).netloc or "").strip().lower()
            if pub_host:
                allowed_hosts.add(pub_host)
        if not origin_host or (origin_host not in allowed_hosts and not allow_e2b_iframe):
            raise HTTPException(status_code=403, detail={"error": "csrf_blocked"})


def cookie_header(request: Request, name: str, value: str, max_age: int, path: str = "/") -> str:
    https = is_https_request(request)
    if SECURE_COOKIES_MODE == "1":
        secure = True
    elif SECURE_COOKIES_MODE == "0":
        secure = False
    else:
        secure = https

    in_iframe = request.headers.get("sec-fetch-dest", "").lower() == "iframe"
    same_site = "None" if (in_iframe and secure and _is_e2b_preview_request(request)) else "Lax"

    parts = [f"{name}={value}", f"Max-Age={max_age}", f"Path={path}", "HttpOnly", f"SameSite={same_site}"]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def client_ip(request: Request) -> str:
    if _is_trusted_proxy_peer(request):
        forwarded = (
            request.headers.get("cf-connecting-ip")
            or request.headers.get("x-real-ip")
            or request.headers.get("x-forwarded-for")
            or ""
        )
        if forwarded:
            cand = forwarded.split(",")[0].strip()[:80]
            with contextlib.suppress(ValueError):
                return str(ipaddress.ip_address(cand))
    return request.client.host if request.client else "unknown"


_MAX_RATE_BUCKETS = 4096


def _prune_rate_buckets_locked(now: float, max_age: float = 300.0) -> None:
    if len(_rate_buckets) > 2048:
        cutoff = now - max_age
        stale = [k for k, v in _rate_buckets.items() if not v or v[-1] <= cutoff]
        for k in stale:
            _rate_buckets.pop(k, None)
    if len(_rate_buckets) >= _MAX_RATE_BUCKETS:
        # Защита памяти при массовом переборе ключей: вытесняем самые старые корзины
        by_age = sorted(_rate_buckets.items(), key=lambda kv: kv[1][-1] if kv[1] else 0.0)
        for k, _ in by_age[:1024]:
            _rate_buckets.pop(k, None)


def rate_limit(request: Request, name: str, limit: int = 60, period: int = 60) -> None:
    key = f"{name}:{client_ip(request)}"
    now = time.time()
    with _rate_lock:
        _prune_rate_buckets_locked(now, max(period, 300))
        bucket = [ts for ts in _rate_buckets.get(key, []) if ts > now - period]
        if len(bucket) >= limit:
            raise HTTPException(status_code=429, detail={"error": "rate_limited"})
        bucket.append(now)
        _rate_buckets[key] = bucket


def _check_admin_failed_login_guard(request: Request) -> None:
    ip_key = f"admin_fail_ip:{client_ip(request)}"
    global_key = "admin_fail_global:all"
    now = time.time()
    with _rate_lock:
        ip_fails = [ts for ts in _rate_buckets.get(ip_key, []) if ts > now - 300]
        global_fails = [ts for ts in _rate_buckets.get(global_key, []) if ts > now - 60]
        if len(ip_fails) >= 8 or len(global_fails) >= 20:
            raise HTTPException(status_code=429, detail={"error": "rate_limited"})


def _record_admin_failed_login(request: Request) -> None:
    ip_key = f"admin_fail_ip:{client_ip(request)}"
    global_key = "admin_fail_global:all"
    now = time.time()
    with _rate_lock:
        _prune_rate_buckets_locked(now, 300)
        for key, window in ((ip_key, 300), (global_key, 60)):
            bucket = [ts for ts in _rate_buckets.get(key, []) if ts > now - window]
            bucket.append(now)
            _rate_buckets[key] = bucket


def j(body: Any, status: int = 200, headers: dict[str, str] | None = None) -> JSONResponse:
    response = JSONResponse(body, status_code=status)
    response.headers["Cache-Control"] = "no-store"
    if headers:
        for key, value in headers.items():
            response.headers[key] = value
    return response


def cached_api_get(key: str) -> tuple[int, dict[str, Any]] | None:
    now = time.time()
    with _api_cache_lock:
        entry = _api_cache.get(key)
        if entry and entry[0] > now:
            return entry[1], entry[2]
    return None


def cached_api_set(key: str, ttl: float, status: int, payload: dict[str, Any]) -> None:
    now = time.time()
    with _api_cache_lock:
        if len(_api_cache) > 512:
            expired = [k for k, v in _api_cache.items() if v[0] <= now]
            for k in expired:
                _api_cache.pop(k, None)
        _api_cache[key] = (now + ttl, status, payload)


# ----------------------------- нормализация списков -----------------------------


def _safe_int(value: Any, default: int = 0) -> int:
    if value is None or value == "" or isinstance(value, bool):
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default


def clean_string_array(value: Any, max_items: int, max_text: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return [text(item, max_text) for item in value if text(item, max_text)][:max_items]


def clean_tags(value: Any, max_items: int = MAX_TAGS, max_len: int = MAX_TAG_LEN) -> list[str]:
    """Теги: обрезаем, схлопываем пробелы, убираем дубли без учёта регистра.

    Принимаем только строки: числа, вложенные массивы и объекты из JSON отбрасываются,
    чтобы в коллекцию не попадали артефакты вроде "['x']".
    """
    if not isinstance(value, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, str):
            continue
        tag = " ".join(raw.split())[:max_len].strip()
        key = tag.casefold()
        if not tag or key in seen:
            continue
        seen.add(key)
        result.append(tag)
        if len(result) >= max_items:
            break
    return result


def normalize_watchlist_item(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    item_id = text(item.get("id"), 120)
    title = text(item.get("title"), 180)
    item_type = text(item.get("type"), 20)
    status = text(item.get("status"), 20)
    if not ITEM_ID_PATTERN.fullmatch(item_id) or not title or item_type not in VALID_TYPES or status not in VALID_STATUSES:
        return None
    review = text(item.get("review"), 5000)
    now = int(time.time() * 1000)
    return {
        "id": item_id,
        "title": title,
        "titleRu": text(item.get("titleRu"), 180) or None,
        "year": text(item.get("year"), 24) or "-",
        "type": item_type,
        "status": status,
        "poster": clean_url(item.get("poster")),
        "rating": max(0, min(MAX_WATCHLIST_RATING, _safe_int(item.get("rating"), 0))),
        "review": review,
        "tags": clean_tags(item.get("tags")),
        "favorite": bool(item.get("favorite")),
        "addedAt": _safe_int(item.get("addedAt"), now) or now,
        "updatedAt": _safe_int(item.get("updatedAt"), now) or now,
    }


def normalize_gamelist_item(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    item_id = text(item.get("id"), 120)
    title = text(item.get("title"), 180)
    status = text(item.get("status"), 20)
    try:
        rating = max(0.0, min(10.0, float(item.get("rating") or 0)))
    except Exception:
        rating = 0.0
    if not ITEM_ID_PATTERN.fullmatch(item_id) or not title or status not in VALID_GAME_STATUSES:
        return None
    now = int(time.time() * 1000)
    return {
        "id": item_id,
        "igdbId": int(item.get("igdbId")) if str(item.get("igdbId") or "").isdigit() else None,
        "title": title,
        "year": text(item.get("year"), 24) or "-",
        "status": status,
        "rating": round(rating * 10) / 10,
        "review": text(item.get("review"), 5000),
        "cover": clean_url(item.get("cover")),
        "genres": clean_string_array(item.get("genres"), 5, 32),
        "platforms": clean_string_array(item.get("platforms"), 8, 32),
        "category": max(0, _safe_int(item.get("category"), 0)),
        "tags": clean_tags(item.get("tags")),
        "favorite": bool(item.get("favorite")),
        "addedAt": _safe_int(item.get("addedAt"), now) or now,
        "updatedAt": _safe_int(item.get("updatedAt"), now) or now,
    }


def normalize_setup_item(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    item_id = text(item.get("id"), 120)
    group = text(item.get("group") or item.get("section"), 20)
    category = text(item.get("category") or item.get("slot"), 80)
    name = text(item.get("name") or item.get("title"), 180)
    if not ITEM_ID_PATTERN.fullmatch(item_id) or group not in VALID_SETUP_SECTIONS or not category or not name:
        return None
    icon = text(item.get("icon"), 20) or "other"
    raw_sort = item.get("sort") if item.get("sort") is not None else item.get("order")
    try:
        sort_value = max(0, min(999, round(float(raw_sort))))
    except Exception:
        sort_value = 0
    now = int(time.time() * 1000)
    return {
        "id": item_id,
        "group": group,
        "category": category,
        "name": name,
        "icon": icon if icon in VALID_SETUP_ICONS else "other",
        "note": text(item.get("note"), 300) or "",
        "link": clean_url(item.get("link")) or "",
        "sort": sort_value,
        "addedAt": _safe_int(item.get("addedAt"), now) or now,
        "updatedAt": _safe_int(item.get("updatedAt"), now) or now,
    }


def normalize_wishlist_item(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    item_id = text(item.get("id"), 120)
    title = text(item.get("title"), 180)
    category = text(item.get("category"), 20)
    status = text(item.get("status"), 20)
    if not ITEM_ID_PATTERN.fullmatch(item_id) or not title or category not in VALID_WISH_CATEGORIES or status not in VALID_WISH_STATUSES:
        return None
    currency = text(item.get("currency"), 8).upper() or "RUB"
    try:
        price = float(item.get("price"))
        price = min(1_000_000_000, round(price * 100) / 100) if price > 0 else 0
    except Exception:
        price = 0
    try:
        priority = int(item.get("priority"))
    except Exception:
        priority = 2
    now = int(time.time() * 1000)
    return {
        "id": item_id,
        "title": title,
        "category": category,
        "status": status,
        "priority": priority if priority in VALID_WISH_PRIORITIES else 2,
        "price": price,
        "currency": currency if currency in VALID_CURRENCIES else "RUB",
        "note": text(item.get("note"), 2000) or "",
        "link": clean_url(item.get("link")) or "",
        "image": clean_url(item.get("image")) or "",
        "tags": clean_tags(item.get("tags")),
        "favorite": bool(item.get("favorite")),
        "addedAt": _safe_int(item.get("addedAt"), now) or now,
        "updatedAt": _safe_int(item.get("updatedAt"), now) or now,
    }


NORMALIZERS = {
    "watchlist": normalize_watchlist_item,
    "gamelist": normalize_gamelist_item,
    "setup": normalize_setup_item,
    "wishlist": normalize_wishlist_item,
}


DEFAULT_DRIVE_VODS = [
    ("2026-09-03_17-56", "1_SRlYEQ9E0hF1dWmoS3segy5qSAV7rG3", "мама в 16 (я) ｜ !конкурс ｜ 2К ДОНАТ И ОФФАЮ КОМПЬЮТЕР ✌ (БЕЗ ШУТОК) ｜ биб боб", "7.21 ГБ", "", "2026-09-03_17-56"),
    ("2026-09-02_12-15", "1td-vOnVhyvsT6VjzROhhdIV7asxvJImR", "ну допустим мяу ｜ !конкурс ｜ 2К ДОНАТ И ОФФАЮ КОМПЬЮТЕР ✌ (БЕЗ ШУТОК) ｜ биб боб", "12.32 ГБ", "", "2026-09-02_12-15"),
    ("2026-08-31_13-44", "1u8ZrnJPIASjUEI5EeYS1v_X7C63YnDeJ", "ну допустим мяу ｜ 2К ДОНАТ И ОФФАЮ КОМПЬЮТЕР ✌ (БЕЗ ШУТОК) ｜ биб боб", "7.48 ГБ", "", "2026-08-31_13-44"),
    ("2026-08-30_13-04", "1igadvmUfhI_XOjcmGsE_2rMaxYB7_4Be", "мяу ｜ 2К ДОНАТ И ОФФАЮ КОМПЬЮТЕР ✌ (БЕЗ ШУТОК) ｜ биб боб", "1.84 ГБ", "", "2026-08-30_13-04"),
    ("2026-08-30_01-01", "1mLVCiVyC6yBVkX-E3YJafLY7EJhGeu-k", "мяу ｜ 2К ДОНАТ И ОФФАЮ КОМПЬЮТЕР ✌ (БЕЗ ШУТОК) ｜ биб боб", "3.65 ГБ", "", "2026-08-30_01-01"),
    ("2026-08-27_17-23", "1KyokELlAV_-f703pnSjEtRpaKyWDH2mk", "щавелевый персик ｜ 2К ДОНАТ И ОФФАЮ КОМПЬЮТЕР ✌ (БЕЗ ШУТОК) ｜ биб боб", "7.5 ГБ", "", "2026-08-27_17-23"),
    ("2026-08-25_14-52", "13maPLTBIWghOtGDDhdtG5iY9MUFdeXeE", "щавелевый персик ｜ 2К ДОНАТ И ОФФАЮ КОМПЬЮТЕР ✌ (БЕЗ ШУТОК) ｜ биб боб", "13.25 ГБ", "эта запись лагающая — не исправить", "2026-08-25_14-52"),
    ("2026-08-23_11-50", "1QPUp9NYaHEdvS8P-cndGpks1LivR-YkP", "щавелевый персик ｜ 2К ДОНАТ И ОФФАЮ КОМПЬЮТЕР ✌ (БЕЗ ШУТОК) ｜ биб боб", "17.27 ГБ", "", "2026-08-23_11-50"),
]


def _ensure_columns(conn: sqlite3.Connection, table: str, columns: tuple[tuple[str, str], ...]) -> None:
    """Мягкая миграция: добавляет недостающие колонки в существующую таблицу."""
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for col_name, col_ddl in columns:
        if col_name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_ddl}")


# Колонки, появившиеся позже базовой схемы (оценки, рецензии, теги, избранное)
_COLLECTION_EXTRA_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "watchlist_items": (
        ("rating", "INTEGER NOT NULL DEFAULT 0"),
        ("review", "TEXT NOT NULL DEFAULT ''"),
        ("tags_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("favorite", "INTEGER NOT NULL DEFAULT 0"),
    ),
    "gamelist_items": (
        ("tags_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("favorite", "INTEGER NOT NULL DEFAULT 0"),
    ),
    "wishlist_items": (
        ("tags_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("favorite", "INTEGER NOT NULL DEFAULT 0"),
    ),
}


def init_db() -> None:
    now = int(time.time())
    with _db_lock, db_connect() as conn:
        conn.executescript(SCHEMA_SQL)
        for table, columns in _COLLECTION_EXTRA_COLUMNS.items():
            _ensure_columns(conn, table, columns)

        existing_vod_cols = {
            row["name"] for row in conn.execute("PRAGMA table_info(drive_vods)").fetchall()
        }
        for col_name, col_ddl in (
            ("folder_drive_id", "TEXT NOT NULL DEFAULT ''"),
            ("chat_drive_id", "TEXT NOT NULL DEFAULT ''"),
            ("thumb_drive_id", "TEXT NOT NULL DEFAULT ''"),
            ("meta_drive_id", "TEXT NOT NULL DEFAULT ''"),
            ("game", "TEXT NOT NULL DEFAULT ''"),
            ("duration_s", "INTEGER NOT NULL DEFAULT 0"),
            ("started_at", "TEXT NOT NULL DEFAULT ''"),
        ):
            if col_name not in existing_vod_cols:
                conn.execute(f"ALTER TABLE drive_vods ADD COLUMN {col_name} {col_ddl}")

        meta_count = conn.execute("SELECT COUNT(*) AS cnt FROM collection_meta").fetchone()["cnt"]
        has_legacy_json = any((DATA_DIR / spec["legacy_file"]).is_file() for spec in LIST_SPECS.values())

        if meta_count == 0 and not has_legacy_json and SEED_SQL_PATH.is_file():
            conn.executescript(SEED_SQL_PATH.read_text(encoding="utf-8"))

        drive_count = conn.execute("SELECT COUNT(*) AS cnt FROM drive_vods").fetchone()["cnt"]
        if drive_count == 0 and GDRIVE_FOLDER_ID == "1e417_q9s6SjcZHyqMYzbVs8OHqfY5Ifk":
            conn.executemany(
                "INSERT OR IGNORE INTO drive_vods (id, drive_id, title, size_label, note, sort_key) VALUES (?, ?, ?, ?, ?, ?)",
                DEFAULT_DRIVE_VODS,
            )

        for name, spec in LIST_SPECS.items():
            row = conn.execute("SELECT 1 FROM collection_meta WHERE name = ?", (name,)).fetchone()
            if row:
                continue
            legacy_path = DATA_DIR / spec["legacy_file"]
            initial_rev = 0
            normalized_items: list[dict[str, Any]] = []
            if legacy_path.is_file():
                try:
                    raw = _normalize_legacy_list(json.loads(legacy_path.read_text(encoding="utf-8")))
                    initial_rev = raw["revision"]
                    norm_fn = NORMALIZERS[spec["normalizer"]]
                    for raw_item in raw["items"]:
                        clean = norm_fn(raw_item)
                        if clean is not None:
                            normalized_items.append(clean)
                except Exception:
                    initial_rev = 0
                    normalized_items = []
            conn.execute("BEGIN IMMEDIATE")
            try:
                _write_collection_items(conn, name, normalized_items[: spec["max_items"]])
                conn.execute(
                    "INSERT INTO collection_meta (name, revision, updated_at) VALUES (?, ?, ?)",
                    (name, initial_rev, now),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise


init_db()


# ----------------------------- Телеметрия и статистика (analytics.db) -----------------------------

ANALYTICS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS daily_traffic (
    day TEXT PRIMARY KEY,
    views INTEGER NOT NULL DEFAULT 0,
    api_hits INTEGER NOT NULL DEFAULT 0,
    bot_hits INTEGER NOT NULL DEFAULT 0,
    vod_plays INTEGER NOT NULL DEFAULT 0,
    vod_downloads INTEGER NOT NULL DEFAULT 0,
    bytes_sent INTEGER NOT NULL DEFAULT 0,
    status_2xx INTEGER NOT NULL DEFAULT 0,
    status_3xx INTEGER NOT NULL DEFAULT 0,
    status_4xx INTEGER NOT NULL DEFAULT 0,
    status_5xx INTEGER NOT NULL DEFAULT 0,
    blocked_hits INTEGER NOT NULL DEFAULT 0,
    latency_sum_ms REAL NOT NULL DEFAULT 0,
    req_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS hourly_traffic (
    hour_bucket INTEGER PRIMARY KEY,
    views INTEGER NOT NULL DEFAULT 0,
    api_hits INTEGER NOT NULL DEFAULT 0,
    vod_plays INTEGER NOT NULL DEFAULT 0,
    bytes_sent INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS daily_visitors (
    day TEXT NOT NULL,
    vhash TEXT NOT NULL,
    first_ts INTEGER NOT NULL,
    last_ts INTEGER NOT NULL,
    hits INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (day, vhash)
);

CREATE TABLE IF NOT EXISTS hourly_visitors (
    hour_bucket INTEGER NOT NULL,
    vhash TEXT NOT NULL,
    PRIMARY KEY (hour_bucket, vhash)
);

CREATE TABLE IF NOT EXISTS page_stats (
    day TEXT NOT NULL,
    page TEXT NOT NULL,
    views INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, page)
);

CREATE TABLE IF NOT EXISTS referrer_stats (
    day TEXT NOT NULL,
    source TEXT NOT NULL,
    views INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, source)
);

CREATE TABLE IF NOT EXISTS device_stats (
    day TEXT NOT NULL,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    views INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, kind, name)
);

CREATE TABLE IF NOT EXISTS vod_analytics (
    vod_id TEXT PRIMARY KEY,
    plays INTEGER NOT NULL DEFAULT 0,
    downloads INTEGER NOT NULL DEFAULT 0,
    chat_loads INTEGER NOT NULL DEFAULT 0,
    bytes_streamed INTEGER NOT NULL DEFAULT 0,
    last_played_at INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS admin_audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    action TEXT NOT NULL,
    status TEXT NOT NULL,
    ip TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);
"""

_analytics_db_lock = threading.Lock()
_analytics_buf_lock = threading.Lock()
_analytics_flusher_started = False
_analytics_last_prune_ts = 0.0

_TRACKED_PAGES: dict[str, tuple[str, str]] = {
    "/": ("/", "Главная"),
    "/index.html": ("/", "Главная"),
    "/watchlist": ("/watchlist", "Watchlist"),
    "/watchlist.html": ("/watchlist", "Watchlist"),
    "/gamelist": ("/gamelist", "Gamelist"),
    "/gamelist.html": ("/gamelist", "Gamelist"),
    "/setup": ("/setup", "Setup"),
    "/setup.html": ("/setup", "Setup"),
    "/wishlist": ("/wishlist", "Wishlist"),
    "/wishlist.html": ("/wishlist", "Wishlist"),
    "/tasks": ("/tasks", "Tasks"),
    "/tasks.html": ("/tasks", "Tasks"),
    "/danonsha": ("/danonsha", "danonsha"),
    "/danonsha.html": ("/danonsha", "danonsha"),
}

_BOT_UA_RE = re.compile(
    r"(bot|spider|crawl|slurp|mediapartners|curl/|wget/|python-requests|httpx|aiohttp|"
    r"headlesschrome|lighthouse|uptimerobot|pingdom|statuscake|go-http-client|facebookexternalhit|"
    r"discordbot|telegrambot|twitterbot|whatsapp|yandexbot|googlebot|bingbot|applebot|semrush|ahrefs)",
    flags=re.IGNORECASE,
)

_COUNTRY_LABELS = {
    "RU": "Россия",
    "BY": "Беларусь",
    "KZ": "Казахстан",
    "UA": "Украина",
    "DE": "Германия",
    "NL": "Нидерланды",
    "FI": "Финляндия",
    "US": "США",
    "GB": "Великобритания",
    "PL": "Польша",
    "FR": "Франция",
    "TR": "Турция",
    "AM": "Армения",
    "GE": "Грузия",
    "UZ": "Узбекистан",
    "EE": "Эстония",
    "LV": "Латвия",
    "LT": "Литва",
    "SE": "Швеция",
}

# In-memory агрегационные буферы до сброса в SQLite
_buf_daily: dict[str, list[float]] = {}
_buf_hourly: dict[int, list[int]] = {}
_buf_visitors: dict[tuple[str, str], list[int]] = {}
_buf_hourly_visitors: set[tuple[int, str]] = set()
_buf_pages: dict[tuple[str, str], int] = {}
_buf_referrers: dict[tuple[str, str], int] = {}
_buf_devices: dict[tuple[str, str, str], int] = {}
_buf_vods: dict[str, list[int]] = {}
_active_visitors: dict[str, tuple[float, str]] = {}
_recent_vod_events: dict[tuple[str, str, str], float] = {}


@contextlib.contextmanager
def analytics_db_connect():
    is_new = not ANALYTICS_DB_PATH.exists()
    conn = sqlite3.connect(ANALYTICS_DB_PATH, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    if is_new:
        with contextlib.suppress(OSError):
            os.chmod(ANALYTICS_DB_PATH, 0o600)
    try:
        yield conn
    finally:
        conn.close()


def init_analytics_db() -> None:
    with _analytics_db_lock, analytics_db_connect() as conn:
        conn.executescript(ANALYTICS_SCHEMA_SQL)


with contextlib.suppress(Exception):
    init_analytics_db()


def read_vod_overrides() -> dict[str, dict[str, Any]]:
    val = kv_get("vod_overrides", {})
    return val if isinstance(val, dict) else {}


def write_vod_overrides(overrides: dict[str, dict[str, Any]]) -> None:
    kv_set("vod_overrides", overrides)


def _visitor_daily_hash(day: str, ip: str, ua: str) -> str:
    salt = SECRET_KEY.encode("utf-8")
    msg = f"{day}|{ip}|{ua[:160]}".encode("utf-8", errors="replace")
    return hmac.new(salt, msg, hashlib.sha256).hexdigest()[:20]


def _classify_user_agent(ua: str) -> tuple[str, str, str]:
    u = ua.lower()
    if any(k in u for k in ("ipad", "tablet", "playbook", "silk")):
        device = "Планшет"
    elif any(k in u for k in ("mobi", "iphone", "ipod", "android")):
        device = "Смартфон"
    else:
        device = "ПК"

    if "telegram" in u:
        browser = "Telegram"
    elif "yabrowser" in u or "yandex" in u:
        browser = "Яндекс"
    elif "edg/" in u or "edga/" in u or "edgios/" in u:
        browser = "Edge"
    elif "opr/" in u or "opera" in u:
        browser = "Opera"
    elif "firefox/" in u or "fxios/" in u:
        browser = "Firefox"
    elif "chrome/" in u or "crios/" in u:
        browser = "Chrome"
    elif "safari/" in u:
        browser = "Safari"
    else:
        browser = "Другой"

    if "windows" in u:
        os_name = "Windows"
    elif "android" in u:
        os_name = "Android"
    elif "iphone" in u or "ipad" in u or "cpu os" in u:
        os_name = "iOS"
    elif "mac os x" in u or "macintosh" in u:
        os_name = "macOS"
    elif "linux" in u:
        os_name = "Linux"
    else:
        os_name = "Другая"
    return device, browser, os_name


def _classify_referrer(ref: str, self_host: str) -> str:
    raw = (ref or "").strip()
    if not raw:
        return "Прямой заход"
    try:
        parsed = urlsplit(raw)
        host = (parsed.hostname or "").lower().rstrip(".")
    except Exception:
        return "Прямой заход"
    if not host:
        return "Прямой заход"
    clean_self = (self_host or "").split(":", 1)[0].lower()
    if host == clean_self or (PUBLIC_ORIGIN and host == (urlsplit(PUBLIC_ORIGIN).hostname or "").lower()):
        return ""
    if host.startswith("www."):
        host = host[4:]
    if host in ("t.me", "telegram.me", "telegram.org") or host.endswith(".t.me"):
        return "Telegram"
    if "twitch.tv" in host:
        return "Twitch"
    if "discord.com" in host or "discord.gg" in host:
        return "Discord"
    if "youtube.com" in host or host == "youtu.be":
        return "YouTube"
    if "yandex." in host or host == "ya.ru":
        return "Яндекс"
    if "google." in host:
        return "Google"
    if host in ("vk.com", "vk.ru"):
        return "VK"
    if "github.com" in host:
        return "GitHub"
    if "boosty.to" in host:
        return "Boosty"
    return host[:60]


# --- География посетителей без Cloudflare ----------------------------------
# Основной источник: IPinfo Lite (MMDB, обновляется ежедневно), нужен IPINFO_TOKEN.
# Резерв (и работа без токена): встроенная база geoip2fast.
# Лицензия IPinfo Lite: CC BY-SA 4.0, см. README.
try:  # необязательная зависимость: без неё используется только резерв
    import maxminddb as _maxminddb
except ImportError:  # pragma: no cover
    _maxminddb = None
try:  # необязательная зависимость: без неё страна определяется только через Cloudflare
    from geoip2fast import GeoIP2Fast as _GeoIP2Fast
except ImportError:  # pragma: no cover
    _GeoIP2Fast = None

IPINFO_TOKEN = (os.environ.get("IPINFO_TOKEN") or "").strip()
IPINFO_DB_PATH = DATA_DIR / "ipinfo_lite.mmdb"
IPINFO_URL_TEMPLATE = "https://ipinfo.io/data/ipinfo_lite.mmdb?token={token}"
IPINFO_MAX_AGE_SEC = 24 * 3600
IPINFO_CHECK_SEC = 3600
_ipinfo_reader: Any = None
_ipinfo_reader_mtime = 0.0
_ipinfo_lock = threading.Lock()
_ipinfo_updater_started = False
_geoip_db: Any = None
_geoip_lock = threading.Lock()


_geo_stats_lock = threading.Lock()
_geo_source_counts: dict[str, int] = {}
_ipinfo_last: dict[str, Any] = {"attempt_ts": 0.0, "ok": None, "error": ""}
# Счётчики для диагностики: откуда пришёл адрес посетителя (без самих адресов)
_geo_conn_counts: dict[str, int] = {
    "total": 0, "trusted_peer": 0, "public_untrusted_peer": 0, "private_untrusted_peer": 0,
    "hdr_x_real_ip": 0, "hdr_x_forwarded_for": 0, "hdr_cf_connecting_ip": 0, "hdr_cf_ipcountry": 0,
}


def _geo_mask(message: str) -> str:
    if IPINFO_TOKEN:
        message = message.replace(IPINFO_TOKEN, "***")
    return message


def _geo_log(message: str) -> None:
    print(f"[geo] {_geo_mask(message)}", flush=True)


def _geo_count(source: str) -> None:
    with _geo_stats_lock:
        _geo_source_counts[source] = _geo_source_counts.get(source, 0) + 1


def _geo_note_connection(request: Request) -> None:
    trusted = _is_trusted_proxy_peer(request)
    peer = request.client.host if request.client else ""
    try:
        peer_public = not ipaddress.ip_address(peer).is_private and not ipaddress.ip_address(peer).is_loopback
    except ValueError:
        peer_public = False
    with _geo_stats_lock:
        c = _geo_conn_counts
        c["total"] += 1
        if trusted:
            c["trusted_peer"] += 1
        elif peer_public:
            c["public_untrusted_peer"] += 1
        else:
            c["private_untrusted_peer"] += 1
        for key, header in (
            ("hdr_x_real_ip", "x-real-ip"),
            ("hdr_x_forwarded_for", "x-forwarded-for"),
            ("hdr_cf_connecting_ip", "cf-connecting-ip"),
            ("hdr_cf_ipcountry", "cf-ipcountry"),
        ):
            if request.headers.get(header):
                c[key] += 1


def geo_status() -> dict[str, Any]:
    """Состояние географии для админки. Токен не возвращается, только его наличие."""
    try:
        st = IPINFO_DB_PATH.stat()
        db_exists, db_bytes, db_mtime = True, st.st_size, int(st.st_mtime)
    except OSError:
        db_exists, db_bytes, db_mtime = False, 0, 0
    with _geo_stats_lock:
        counts = dict(_geo_source_counts)
        conn = dict(_geo_conn_counts)
    return {
        "maxminddb_installed": _maxminddb is not None,
        "geoip2fast_installed": _GeoIP2Fast is not None,
        "ipinfo_token_set": bool(IPINFO_TOKEN),
        "ipinfo_db_exists": db_exists,
        "ipinfo_db_bytes": db_bytes,
        "ipinfo_db_updated_ts": db_mtime,
        "ipinfo_last_attempt_ts": int(_ipinfo_last["attempt_ts"] or 0),
        "ipinfo_last_ok": _ipinfo_last["ok"],
        "ipinfo_last_error": _ipinfo_last["error"],
        "sources": counts,
        "connections": conn,
    }


def _geo_startup_warnings() -> None:
    if _maxminddb is None:
        _geo_log("не установлен maxminddb: выполните pip install -r requirements.txt")
    if _GeoIP2Fast is None:
        _geo_log("не установлен geoip2fast: выполните pip install -r requirements.txt")
    if not IPINFO_TOKEN:
        _geo_log("IPINFO_TOKEN не задан: база IPinfo Lite не загружается, используется резерв")


def _ipinfo_get_reader() -> Any:
    global _ipinfo_reader, _ipinfo_reader_mtime
    if _maxminddb is None:
        return None
    try:
        mtime = IPINFO_DB_PATH.stat().st_mtime
    except OSError:
        return None
    if _ipinfo_reader is not None and mtime == _ipinfo_reader_mtime:
        return _ipinfo_reader
    with _ipinfo_lock:
        if _ipinfo_reader is not None and mtime == _ipinfo_reader_mtime:
            return _ipinfo_reader
        try:
            _ipinfo_reader = _maxminddb.open_database(str(IPINFO_DB_PATH))
            _ipinfo_reader_mtime = mtime
        except Exception as exc:
            _geo_log(f"не удалось открыть {IPINFO_DB_PATH.name}: {type(exc).__name__}")
    return _ipinfo_reader


def _ipinfo_fail(message: str) -> None:
    _ipinfo_last["ok"] = False
    _ipinfo_last["error"] = _geo_mask(message)[:200]
    _geo_log(f"IPinfo Lite: ошибка загрузки ({_geo_mask(message)})")


def _ipinfo_download_once() -> bool:
    """Скачивает ipinfo_lite.mmdb по токену (302 → CDN) и атомарно заменяет файл."""
    if not IPINFO_TOKEN or _maxminddb is None:
        return False
    tmp = IPINFO_DB_PATH.with_name(IPINFO_DB_PATH.name + ".part")
    url = IPINFO_URL_TEMPLATE.format(token=quote(IPINFO_TOKEN, safe=""))
    _ipinfo_last["attempt_ts"] = time.time()
    try:
        # requests следует за редиректом и не передаёт токен на CDN
        with requests.get(url, stream=True, timeout=(10, 180), headers={"User-Agent": "krafen-geo/1.0"}) as resp:
            if resp.status_code != 200:
                _ipinfo_fail(f"HTTP {resp.status_code} (проверьте IPINFO_TOKEN)")
                return False
            with open(tmp, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=1 << 16):
                    if chunk:
                        fh.write(chunk)
        check = _maxminddb.open_database(str(tmp))
        try:
            if not check.get("8.8.8.8"):
                raise ValueError("база пустая или повреждена")
        finally:
            check.close()
        os.replace(tmp, IPINFO_DB_PATH)
        _ipinfo_last["ok"] = True
        _ipinfo_last["error"] = ""
        _geo_log("IPinfo Lite: база обновлена")
        return True
    except Exception as exc:
        _ipinfo_fail(f"{type(exc).__name__}: {str(exc)[:160]}")
        with contextlib.suppress(OSError):
            tmp.unlink()
        return False


def _ipinfo_needs_update() -> bool:
    try:
        return time.time() - IPINFO_DB_PATH.stat().st_mtime >= IPINFO_MAX_AGE_SEC
    except OSError:
        return True


def _start_ipinfo_updater() -> None:
    global _ipinfo_updater_started
    if _ipinfo_updater_started or not IPINFO_TOKEN:
        return
    _ipinfo_updater_started = True

    def _worker() -> None:
        while True:
            if _ipinfo_needs_update():
                _ipinfo_download_once()
            time.sleep(IPINFO_CHECK_SEC)

    threading.Thread(target=_worker, name="krafen-ipinfo-updater", daemon=True).start()


def _country_from_ip_db(ip: str) -> str:
    """Страна по IP локально (IPinfo Lite, затем geoip2fast). Внешних запросов нет."""
    global _geoip_db
    valid = lambda cc: len(cc) == 2 and cc.isalpha() and cc != "XX"  # noqa: E731
    reader = _ipinfo_get_reader()
    if reader is not None:
        try:
            rec = reader.get(ip)
            cc = str(rec.get("country_code") or "").strip().upper() if isinstance(rec, dict) else ""
        except Exception:
            cc = ""
        if valid(cc):
            _geo_count("ipinfo")
            return cc
    if _GeoIP2Fast is None:
        return ""
    if _geoip_db is None:
        with _geoip_lock:
            if _geoip_db is None:
                try:
                    _geoip_db = _GeoIP2Fast()
                except Exception:
                    _geoip_db = False
    if not _geoip_db:
        return ""
    try:
        cc = str(getattr(_geoip_db.lookup(ip), "country_code", "") or "").strip().upper()
    except Exception:
        return ""
    if valid(cc):
        _geo_count("geoip2fast")
        return cc
    return ""


def _classify_country(request: Request, ip: str) -> str:
    _geo_note_connection(request)
    if _is_trusted_proxy_peer(request):
        cc = (request.headers.get("cf-ipcountry") or "").strip().upper()
        if len(cc) == 2 and cc.isalpha() and cc != "XX":
            _geo_count("cloudflare")
            return cc
    with contextlib.suppress(ValueError):
        addr = ipaddress.ip_address(ip)
        if addr.is_loopback or addr.is_private:
            _geo_count("local")
            return "LOCAL"
    cc = _country_from_ip_db(ip)
    if not cc:
        _geo_count("unresolved")
    return cc or "DIRECT"


def record_vod_stream_bytes(vod_id: str, byte_count: int) -> None:
    if byte_count <= 0 or not vod_id:
        return
    now = time.time()
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    hbucket = int(now // 3600)
    with _analytics_buf_lock:
        d = _buf_daily.setdefault(day, [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0.0, 0])
        d[5] += byte_count
        hb = _buf_hourly.setdefault(hbucket, [0, 0, 0, 0])
        hb[3] += byte_count
        if len(_buf_vods) < 1024 or vod_id in _buf_vods:
            vb = _buf_vods.setdefault(vod_id, [0, 0, 0, 0, 0])
            vb[3] += byte_count


def record_request_telemetry(
    request: Request,
    status_code: int,
    latency_ms: float,
    resp_bytes: int,
) -> None:
    path = request.url.path or "/"
    if path.startswith("/api/admin"):
        return

    now = time.time()
    now_i = int(now)
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    hbucket = int(now // 3600)
    ua = (request.headers.get("user-agent") or "")[:300]
    ip = client_ip(request)
    is_bot = bool(_BOT_UA_RE.search(ua)) if ua else False
    purpose = (
        request.headers.get("sec-purpose")
        or request.headers.get("purpose")
        or request.headers.get("x-moz")
        or ""
    ).lower()
    is_prefetch = "prefetch" in purpose or "prerender" in purpose

    is_api = path.startswith("/api/")
    canonical_page = ""
    if request.method == "GET" and status_code == 200 and not is_api:
        norm_p = path.rstrip("/") or "/"
        tracked = _TRACKED_PAGES.get(norm_p)
        if tracked:
            canonical_page = tracked[0]

    # Определяем события VOD
    vod_event_id = ""
    vod_event_kind = ""  # "play" | "download" | "chat"
    if path.startswith("/api/vods/") and status_code in (200, 206):
        rest = path[len("/api/vods/") :]
        if rest.endswith("/stream") and request.method == "GET":
            raw_vid = rest[: -len("/stream")].strip("/")
            if safe_name(raw_vid):
                vod_event_id = raw_vid
                want_dl = (request.query_params.get("download") or "").strip() == "1"
                rng = (request.headers.get("range") or "").strip().lower()
                is_initial_range = (not rng) or rng.startswith("bytes=0-")
                if want_dl and is_initial_range:
                    vod_event_kind = "download"
                elif not want_dl:
                    # Исключаем короткий 192-байтный контейнерный probe (bytes=0-191)
                    if rng != "bytes=0-191":
                        vod_event_kind = "play"
        elif rest.endswith("/chat") and request.method == "GET" and status_code == 200:
            raw_vid = rest[: -len("/chat")].strip("/")
            if safe_name(raw_vid):
                vod_event_id = raw_vid
                vod_event_kind = "chat"
    elif path.startswith("/vods/") and request.method == "GET" and status_code in (200, 206):
        parts = [p for p in path.split("/") if p]
        if len(parts) == 3 and parts[2].lower().endswith((".mp4", ".webm", ".mkv", ".mov")) and safe_name(parts[1]):
            rng = (request.headers.get("range") or "").strip().lower()
            if rng != "bytes=0-191":
                vod_event_id = parts[1]
                vod_event_kind = "play"

    vhash = _visitor_daily_hash(day, ip, ua) if not is_bot else ""

    with _analytics_buf_lock:
        # [views, api_hits, bot_hits, vod_plays, vod_downloads, bytes_sent,
        #  s2xx, s3xx, s4xx, s5xx, blocked_hits, latency_sum_ms, req_count]
        d = _buf_daily.setdefault(day, [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0.0, 0])
        hb = _buf_hourly.setdefault(hbucket, [0, 0, 0, 0])  # [views, api_hits, vod_plays, bytes_sent]

        d[5] += max(0, resp_bytes)
        hb[3] += max(0, resp_bytes)
        d[11] += max(0.0, latency_ms)
        d[12] += 1

        if 200 <= status_code < 300:
            d[6] += 1
        elif 300 <= status_code < 400:
            d[7] += 1
        elif 400 <= status_code < 500:
            d[8] += 1
            if status_code in (401, 403, 413, 429):
                d[10] += 1
        elif status_code >= 500:
            d[9] += 1

        if is_bot:
            d[2] += 1
            return

        if is_api:
            d[1] += 1
            hb[1] += 1

        if canonical_page and not is_prefetch:
            d[0] += 1
            hb[0] += 1
            if len(_buf_pages) < 512:
                _buf_pages[(day, canonical_page)] = _buf_pages.get((day, canonical_page), 0) + 1

            if vhash:
                if len(_buf_visitors) < 8192:
                    vis = _buf_visitors.get((day, vhash))
                    if vis is None:
                        _buf_visitors[(day, vhash)] = [now_i, now_i, 1]
                    else:
                        vis[1] = now_i
                        vis[2] += 1
                if len(_buf_hourly_visitors) < 8192:
                    _buf_hourly_visitors.add((hbucket, vhash))

                # Живой онлайн за последние 5 минут
                if len(_active_visitors) > 2048:
                    cutoff_act = now - 300.0
                    stale_v = [k for k, v in _active_visitors.items() if v[0] < cutoff_act]
                    for k in stale_v:
                        _active_visitors.pop(k, None)
                if len(_active_visitors) < 2048:
                    _active_visitors[vhash] = (now, canonical_page)

            ref_src = _classify_referrer(request.headers.get("referer") or "", _effective_request_host(request))
            if ref_src and len(_buf_referrers) < 1024:
                _buf_referrers[(day, ref_src)] = _buf_referrers.get((day, ref_src), 0) + 1

            dev, brw, os_name = _classify_user_agent(ua)
            cc = _classify_country(request, ip)
            if len(_buf_devices) < 1024:
                for kind, val in (("device", dev), ("browser", brw), ("os", os_name), ("country", cc)):
                    _buf_devices[(day, kind, val)] = _buf_devices.get((day, kind, val), 0) + 1

        if vod_event_id and vod_event_kind and vhash:
            if len(_recent_vod_events) > 4096:
                cutoff_vod = now - 900.0
                stale_vod = [k for k, ts_v in _recent_vod_events.items() if ts_v < cutoff_vod]
                for k in stale_vod:
                    _recent_vod_events.pop(k, None)
            dedup_key = (vhash, vod_event_id, vod_event_kind)
            prev_ts = _recent_vod_events.get(dedup_key, 0.0)
            if now - prev_ts >= 900.0:
                _recent_vod_events[dedup_key] = now
                if len(_buf_vods) < 1024 or vod_event_id in _buf_vods:
                    # [plays, downloads, chat_loads, bytes_streamed, last_played_at]
                    vb = _buf_vods.setdefault(vod_event_id, [0, 0, 0, 0, 0])
                    if vod_event_kind == "play":
                        d[3] += 1
                        hb[2] += 1
                        vb[0] += 1
                        vb[4] = now_i
                    elif vod_event_kind == "download":
                        d[4] += 1
                        vb[1] += 1
                        vb[4] = now_i
                    elif vod_event_kind == "chat":
                        vb[2] += 1


def log_admin_event(request: Request | None, action: str, status: str = "ok", detail: str = "") -> None:
    ip = client_ip(request) if request is not None else "system"
    now_i = int(time.time())
    clean_act = text(action, 60)
    clean_st = text(status, 24)
    clean_det = text(detail, 260)
    try:
        with _analytics_db_lock, analytics_db_connect() as conn:
            conn.execute(
                "INSERT INTO admin_audit_log (ts, action, status, ip, detail) VALUES (?, ?, ?, ?, ?)",
                (now_i, clean_act, clean_st, ip[:80], clean_det),
            )
            conn.execute(
                "DELETE FROM admin_audit_log WHERE id NOT IN (SELECT id FROM admin_audit_log ORDER BY id DESC LIMIT 500)"
            )
    except Exception:
        pass


def flush_analytics_buffer() -> None:
    global _analytics_last_prune_ts
    with _analytics_buf_lock:
        if (
            not _buf_daily
            and not _buf_hourly
            and not _buf_visitors
            and not _buf_hourly_visitors
            and not _buf_pages
            and not _buf_referrers
            and not _buf_devices
            and not _buf_vods
        ):
            return
        snap_daily = dict(_buf_daily)
        _buf_daily.clear()
        snap_hourly = dict(_buf_hourly)
        _buf_hourly.clear()
        snap_visitors = dict(_buf_visitors)
        _buf_visitors.clear()
        snap_hvisitors = list(_buf_hourly_visitors)
        _buf_hourly_visitors.clear()
        snap_pages = dict(_buf_pages)
        _buf_pages.clear()
        snap_refs = dict(_buf_referrers)
        _buf_referrers.clear()
        snap_devs = dict(_buf_devices)
        _buf_devices.clear()
        snap_vods = dict(_buf_vods)
        _buf_vods.clear()

    now = time.time()
    with _analytics_db_lock, analytics_db_connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for day, vals in snap_daily.items():
                conn.execute(
                    """
                    INSERT INTO daily_traffic (
                        day, views, api_hits, bot_hits, vod_plays, vod_downloads, bytes_sent,
                        status_2xx, status_3xx, status_4xx, status_5xx, blocked_hits, latency_sum_ms, req_count
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(day) DO UPDATE SET
                        views = views + excluded.views,
                        api_hits = api_hits + excluded.api_hits,
                        bot_hits = bot_hits + excluded.bot_hits,
                        vod_plays = vod_plays + excluded.vod_plays,
                        vod_downloads = vod_downloads + excluded.vod_downloads,
                        bytes_sent = bytes_sent + excluded.bytes_sent,
                        status_2xx = status_2xx + excluded.status_2xx,
                        status_3xx = status_3xx + excluded.status_3xx,
                        status_4xx = status_4xx + excluded.status_4xx,
                        status_5xx = status_5xx + excluded.status_5xx,
                        blocked_hits = blocked_hits + excluded.blocked_hits,
                        latency_sum_ms = latency_sum_ms + excluded.latency_sum_ms,
                        req_count = req_count + excluded.req_count
                    """,
                    (day, *vals),
                )

            for hbucket, hvals in snap_hourly.items():
                conn.execute(
                    """
                    INSERT INTO hourly_traffic (hour_bucket, views, api_hits, vod_plays, bytes_sent)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(hour_bucket) DO UPDATE SET
                        views = views + excluded.views,
                        api_hits = api_hits + excluded.api_hits,
                        vod_plays = vod_plays + excluded.vod_plays,
                        bytes_sent = bytes_sent + excluded.bytes_sent
                    """,
                    (hbucket, *hvals),
                )

            for (day, vhash), vvals in snap_visitors.items():
                conn.execute(
                    """
                    INSERT INTO daily_visitors (day, vhash, first_ts, last_ts, hits)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(day, vhash) DO UPDATE SET
                        last_ts = MAX(last_ts, excluded.last_ts),
                        hits = hits + excluded.hits
                    """,
                    (day, vhash, vvals[0], vvals[1], vvals[2]),
                )

            if snap_hvisitors:
                conn.executemany(
                    "INSERT OR IGNORE INTO hourly_visitors (hour_bucket, vhash) VALUES (?, ?)",
                    snap_hvisitors,
                )

            for (day, page), cnt in snap_pages.items():
                conn.execute(
                    """
                    INSERT INTO page_stats (day, page, views) VALUES (?, ?, ?)
                    ON CONFLICT(day, page) DO UPDATE SET views = views + excluded.views
                    """,
                    (day, page, cnt),
                )

            for (day, src), cnt in snap_refs.items():
                conn.execute(
                    """
                    INSERT INTO referrer_stats (day, source, views) VALUES (?, ?, ?)
                    ON CONFLICT(day, source) DO UPDATE SET views = views + excluded.views
                    """,
                    (day, src, cnt),
                )

            for (day, kind, name), cnt in snap_devs.items():
                conn.execute(
                    """
                    INSERT INTO device_stats (day, kind, name, views) VALUES (?, ?, ?, ?)
                    ON CONFLICT(day, kind, name) DO UPDATE SET views = views + excluded.views
                    """,
                    (day, kind, name, cnt),
                )

            for vod_id, vvals in snap_vods.items():
                conn.execute(
                    """
                    INSERT INTO vod_analytics (vod_id, plays, downloads, chat_loads, bytes_streamed, last_played_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(vod_id) DO UPDATE SET
                        plays = plays + excluded.plays,
                        downloads = downloads + excluded.downloads,
                        chat_loads = chat_loads + excluded.chat_loads,
                        bytes_streamed = bytes_streamed + excluded.bytes_streamed,
                        last_played_at = MAX(last_played_at, excluded.last_played_at)
                    """,
                    (vod_id, vvals[0], vvals[1], vvals[2], vvals[3], vvals[4]),
                )

            if now - _analytics_last_prune_ts > 3600.0:
                _analytics_last_prune_ts = now
                cur_hb = int(now // 3600)
                cutoff_90d = time.strftime("%Y-%m-%d", time.localtime(now - 90 * 86400))
                conn.execute("DELETE FROM hourly_traffic WHERE hour_bucket < ?", (cur_hb - 24 * 14,))
                conn.execute("DELETE FROM hourly_visitors WHERE hour_bucket < ?", (cur_hb - 72,))
                conn.execute("DELETE FROM daily_visitors WHERE day < ?", (cutoff_90d,))

            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")


def _start_analytics_flusher() -> None:
    global _analytics_flusher_started
    if _analytics_flusher_started:
        return
    _analytics_flusher_started = True

    def _worker() -> None:
        while True:
            time.sleep(4.0)
            with contextlib.suppress(Exception):
                flush_analytics_buffer()

    threading.Thread(target=_worker, name="krafen-analytics-flusher", daemon=True).start()


_MAX_BODY_DEFAULT = 1_048_576  # 1 МБ для прочих state-changing запросов
_MAX_BODY_COLLECTION = 8_388_608  # 8 МБ: 500 записей с рецензиями до 5000 символов
_MAX_BODY_ADMIN = 8_192        # 8 КБ для /api/admin*
_MAX_BODY_RESTORE = 16_777_216  # 16 МБ для импорта JSON-бэкапа всех коллекций
_MAX_BODY_THUMB = 524_288      # 512 КБ для JPEG-превью


class _PayloadTooLargeError(Exception):
    pass


_COLLECTION_PUT_PATHS = frozenset(f"/api/{_name}" for _name in LIST_SPECS)


def _max_body_bytes_for_path(path: str) -> int:
    if path == "/api/admin/restore":
        return _MAX_BODY_RESTORE
    if path.startswith("/api/admin"):
        return _MAX_BODY_ADMIN
    if path in _COLLECTION_PUT_PATHS:
        return _MAX_BODY_COLLECTION
    if path.startswith("/api/vods/") and path.endswith("/thumb"):
        return _MAX_BODY_THUMB
    return _MAX_BODY_DEFAULT


def security_headers(
    nonce: str | None = None,
    *,
    is_https: bool = False,
    allow_frame: bool = False,
) -> dict[str, str]:
    script_src = "script-src 'self'"
    if nonce:
        script_src += f" 'nonce-{nonce}'"
    style_src = "'self' 'unsafe-inline' https://fonts.googleapis.com"
    csp_parts = [
        "default-src 'self'",
        "base-uri 'self'",
        "object-src 'none'",
        "form-action 'self'",
        script_src,
        f"style-src {style_src}",
        f"style-src-elem {style_src}",
        "style-src-attr 'unsafe-inline'",
        "font-src 'self' https://fonts.gstatic.com data:",
        "img-src 'self' data: https:",
        "media-src 'self' blob: https:",
        "worker-src 'self' blob:",
        "frame-src 'self' https://drive.google.com https://*.google.com https://*.googleusercontent.com",
        "connect-src 'self'",
    ]
    if not allow_frame:
        csp_parts.append("frame-ancestors 'self'")
    out = {
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "strict-origin-when-cross-origin",
        "Cross-Origin-Opener-Policy": "same-origin-allow-popups",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
        "Content-Security-Policy": "; ".join(csp_parts),
    }
    if not allow_frame:
        out["X-Frame-Options"] = "SAMEORIGIN"
    if is_https:
        out["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return out


def _accepts_gzip(request: Request) -> bool:
    enc = (request.headers.get("accept-encoding") or "").lower()
    return "gzip" in enc


_GZIPPABLE_CONTENT_TYPES = (
    "application/json",
    "application/x-ndjson",
    "text/html",
    "text/css",
    "application/javascript",
    "text/javascript",
    "image/svg+xml",
    "application/xml",
    "text/plain",
)


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    is_https = is_https_request(request)
    allow_frame = _is_e2b_preview_request(request)
    hdrs = security_headers(is_https=is_https, allow_frame=allow_frame)

    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        max_bytes = _max_body_bytes_for_path(request.url.path)
        cl_raw = (request.headers.get("content-length") or "").strip()
        if cl_raw:
            try:
                if int(cl_raw) > max_bytes:
                    resp = j({"error": "payload_too_large"}, 413)
                    for k, v in hdrs.items():
                        resp.headers.setdefault(k, v)
                    return resp
            except ValueError:
                resp = j({"error": "invalid_content_length"}, 400)
                for k, v in hdrs.items():
                    resp.headers.setdefault(k, v)
                return resp

        orig_receive = request._receive
        seen_bytes = 0

        async def _limited_receive():
            nonlocal seen_bytes
            msg = await orig_receive()
            if msg.get("type") == "http.request":
                seen_bytes += len(msg.get("body") or b"")
                if seen_bytes > max_bytes:
                    raise _PayloadTooLargeError()
            return msg

        request._receive = _limited_receive

    t0 = time.perf_counter()
    try:
        response = await call_next(request)
    except _PayloadTooLargeError:
        response = j({"error": "payload_too_large"}, 413)
    latency_ms = (time.perf_counter() - t0) * 1000.0

    # Сжимаем JSON/текстовые ответы в памяти (не трогая потоковое видео и Range 206)
    if (
        request.method == "GET"
        and getattr(response, "status_code", 0) == 200
        and not isinstance(response, (StreamingResponse, FileResponse))
        and "content-encoding" not in response.headers
    ):
        ctype = (response.headers.get("content-type") or "").lower()
        body_bytes = getattr(response, "body", None)
        if isinstance(body_bytes, (bytes, bytearray)) and any(ctype.startswith(t) for t in _GZIPPABLE_CONTENT_TYPES):
            prev_vary = response.headers.get("vary") or ""
            if "accept-encoding" not in prev_vary.lower():
                response.headers["Vary"] = f"{prev_vary}, Accept-Encoding" if prev_vary else "Accept-Encoding"
            if len(body_bytes) >= 512 and _accepts_gzip(request):
                compressed = gzip.compress(bytes(body_bytes), compresslevel=5)
                if len(compressed) < len(body_bytes):
                    response.body = compressed
                    response.headers["Content-Encoding"] = "gzip"
                    response.headers["Content-Length"] = str(len(compressed))

    for key, value in hdrs.items():
        response.headers.setdefault(key, value)

    with contextlib.suppress(Exception):
        cl_hdr = response.headers.get("content-length")
        resp_len = int(cl_hdr) if (cl_hdr and cl_hdr.isdigit()) else len(getattr(response, "body", b"") or b"")
        # Для StreamingResponse видео байты считаются отдельно в генераторе чанков
        if isinstance(response, StreamingResponse):
            resp_len = 0
        record_request_telemetry(request, int(getattr(response, "status_code", 200) or 200), latency_ms, resp_len)

    return response


# ----------------------------- API коллекций и админки -----------------------------


def make_get_list(name: str):
    def endpoint() -> JSONResponse:
        try:
            state = read_list(name)
            return j({"items": state["items"], "revision": state["revision"]})
        except Exception:
            return j({"error": "storage_error", "items": [], "revision": 0}, 500)

    endpoint.__name__ = f"get_{name}"
    return endpoint


def make_put_list(name: str):
    def endpoint(request: Request, body: dict[str, Any]) -> JSONResponse:
        require_same_origin(request)
        require_admin(request)
        rate_limit(request, f"write_{name}", 60, 60)
        try:
            revision = int(body.get("revision"))
        except Exception:
            revision = -1
        if revision < 0:
            state = read_list(name, use_cache=False)
            return j({"error": "revision_required", "items": state["items"], "revision": state["revision"]}, 409)

        normalizer = NORMALIZERS[name]
        raw_items = body.get("items")
        # Некорректный items не должен молча затирать коллекцию: пустой список — это
        # осознанное удаление всех записей, а строка/объект/null — ошибка клиента.
        if not isinstance(raw_items, list):
            state = read_list(name, use_cache=False)
            return j({"error": "items_required", "items": state["items"], "revision": state["revision"]}, 400)
        items = [normalizer(item) for item in raw_items]
        items = [item for item in items if item][: LIST_SPECS[name]["max_items"]]

        ok, next_state = update_list_atomic(name, revision, items)
        if not ok:
            return j({"error": "revision_conflict", "items": next_state["items"], "revision": next_state["revision"]}, 409)
        log_admin_event(request, f"save_{name}", "ok", f"items={len(next_state['items'])}, rev={next_state['revision']}")
        return j({"ok": True, "items": next_state["items"], "revision": next_state["revision"]})

    endpoint.__name__ = f"put_{name}"
    return endpoint


for _name in LIST_SPECS:
    app.add_api_route(f"/api/{_name}", make_get_list(_name), methods=["GET"])
    app.add_api_route(f"/api/{_name}", make_put_list(_name), methods=["PUT"])


@app.get("/api/health")
def health() -> JSONResponse:
    return j({"ok": True})


@app.get("/api/admin")
def admin_check(request: Request) -> JSONResponse:
    tokens = read_tokens()
    return j(
        {
            "ok": verify_session(request.cookies.get(SESSION_COOKIE)),
            "configured": bool(ADMIN_PASSWORD),
            "spotifyConnected": bool(tokens.get("spotify_refresh_token")),
        }
    )


@app.delete("/api/admin")
def admin_logout(request: Request) -> JSONResponse:
    require_same_origin(request)
    was_admin = verify_session(request.cookies.get(SESSION_COOKIE))
    revoke_session(request.cookies.get(SESSION_COOKIE))
    if was_admin:
        log_admin_event(request, "logout", "ok", "Выход из админ-сессии")
    response = j({"ok": True})
    response.headers["Set-Cookie"] = cookie_header(request, SESSION_COOKIE, "", 0)
    return response


@app.post("/api/admin")
def admin_login(request: Request, body: dict[str, Any]) -> JSONResponse:
    require_same_origin(request)
    rate_limit(request, "admin_login", 10, 60)
    _check_admin_failed_login_guard(request)
    if not ADMIN_PASSWORD:
        return j({"error": "admin_password_missing"}, 503)
    if not constant_time_equal(text(body.get("password"), 512), ADMIN_PASSWORD):
        _record_admin_failed_login(request)
        log_admin_event(request, "login", "denied", "Неверный пароль администратора")
        return j({"error": "invalid_password"}, 401)
    log_admin_event(request, "login", "ok", "Успешный вход в админ-панель")
    response = j({"ok": True})
    response.headers["Set-Cookie"] = cookie_header(request, SESSION_COOKIE, create_session(), SESSION_TTL_SECONDS)
    return response


@app.post("/api/admin/update")
def admin_trigger_update(request: Request) -> JSONResponse:
    require_same_origin(request)
    require_admin(request)
    rate_limit(request, "admin_update", 6, 60)
    result = perform_github_update(force=True, allow_zip_fallback_on_api_error=True)
    status = 200 if result.get("ok") else 502
    log_admin_event(
        request,
        "github_update",
        "ok" if result.get("ok") else "error",
        f"updated={result.get('updated_files', 0)}, sha={str(result.get('sha') or '')[:8]}",
    )
    if result.get("ok") and result.get("server_changed"):
        threading.Timer(1.0, _reexec_current_process).start()
    return j(result, status)


def _dir_usage_stats(directory: Path, pattern: str = "*") -> dict[str, int]:
    count = 0
    size_bytes = 0
    if not directory.is_dir():
        return {"count": 0, "bytes": 0}
    with contextlib.suppress(OSError):
        for p in directory.glob(pattern):
            if p.is_file() and not p.name.startswith(".tmp_"):
                count += 1
                with contextlib.suppress(OSError):
                    size_bytes += p.stat().st_size
    return {"count": count, "bytes": size_bytes}


def _file_size_safe(path: Path) -> int:
    try:
        total = path.stat().st_size if path.is_file() else 0
        for suffix in ("-wal", "-shm"):
            side = Path(str(path) + suffix)
            if side.is_file():
                total += side.stat().st_size
        return total
    except OSError:
        return 0


def _process_rss_bytes() -> int:
    # Linux /proc/self/status
    status_file = Path("/proc/self/status")
    if status_file.is_file():
        with contextlib.suppress(OSError, ValueError):
            for line in status_file.read_text(encoding="utf-8", errors="ignore").splitlines():
                if line.startswith("VmRSS:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        return int(parts[1]) * 1024
    # Windows GetProcessMemoryInfo
    if sys.platform == "win32":
        with contextlib.suppress(Exception):
            import ctypes
            from ctypes import wintypes

            class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            pmc = _PROCESS_MEMORY_COUNTERS()
            pmc.cb = ctypes.sizeof(_PROCESS_MEMORY_COUNTERS)
            hproc = ctypes.windll.kernel32.GetCurrentProcess()
            if ctypes.windll.psapi.GetProcessMemoryInfo(hproc, ctypes.byref(pmc), pmc.cb):
                return int(pmc.WorkingSetSize)
    # POSIX resource fallback
    with contextlib.suppress(Exception):
        import resource

        rss = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        return rss if sys.platform == "darwin" else rss * 1024
    return 0


_RU_SHORT_MONTHS = ("янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек")


def _fmt_short_day_ru(ymd: str) -> str:
    parts = ymd.split("-")
    if len(parts) != 3:
        return ymd
    with contextlib.suppress(ValueError):
        m = max(1, min(12, int(parts[1])))
        d = int(parts[2])
        return f"{d} {_RU_SHORT_MONTHS[m - 1]}"
    return ymd


@app.get("/api/admin/overview")
def admin_overview(request: Request, days: int = 14) -> JSONResponse:
    require_admin(request)
    rate_limit(request, "admin_overview", 120, 60)
    span_days = max(7, min(90, int(days or 14)))

    with contextlib.suppress(Exception):
        flush_analytics_buffer()

    now = time.time()
    today_ymd = time.strftime("%Y-%m-%d", time.localtime(now))
    start_ymd = time.strftime("%Y-%m-%d", time.localtime(now - (span_days - 1) * 86400))
    cur_hbucket = int(now // 3600)
    start_hbucket = cur_hbucket - 23

    # Живой онлайн (за последние 5 минут)
    active_cutoff = now - 300.0
    active_by_page: dict[str, int] = {}
    online_now = 0
    with _analytics_buf_lock:
        stale_keys = [k for k, v in _active_visitors.items() if v[0] < active_cutoff]
        for k in stale_keys:
            _active_visitors.pop(k, None)
        online_now = len(_active_visitors)
        for _, pg in _active_visitors.values():
            active_by_page[pg] = active_by_page.get(pg, 0) + 1

    # Чтение агрегатов из analytics.db
    with _analytics_db_lock, analytics_db_connect() as aconn:
        today_row = aconn.execute("SELECT * FROM daily_traffic WHERE day = ?", (today_ymd,)).fetchone()
        today_uniques = int(
            aconn.execute("SELECT COUNT(*) AS c FROM daily_visitors WHERE day = ?", (today_ymd,)).fetchone()["c"]
        )
        period_uniques = int(
            aconn.execute(
                "SELECT COUNT(DISTINCT vhash) AS c FROM daily_visitors WHERE day >= ?",
                (start_ymd,),
            ).fetchone()["c"]
        )
        all_uniques = int(aconn.execute("SELECT COUNT(DISTINCT vhash) AS c FROM daily_visitors").fetchone()["c"])

        period_sum = aconn.execute(
            """
            SELECT
                COALESCE(SUM(views), 0) AS views,
                COALESCE(SUM(api_hits), 0) AS api_hits,
                COALESCE(SUM(bot_hits), 0) AS bot_hits,
                COALESCE(SUM(vod_plays), 0) AS vod_plays,
                COALESCE(SUM(vod_downloads), 0) AS vod_downloads,
                COALESCE(SUM(bytes_sent), 0) AS bytes_sent,
                COALESCE(SUM(status_2xx), 0) AS s2xx,
                COALESCE(SUM(status_3xx), 0) AS s3xx,
                COALESCE(SUM(status_4xx), 0) AS s4xx,
                COALESCE(SUM(status_5xx), 0) AS s5xx,
                COALESCE(SUM(blocked_hits), 0) AS blocked_hits,
                COALESCE(SUM(latency_sum_ms), 0.0) AS lat_sum,
                COALESCE(SUM(req_count), 0) AS req_cnt
            FROM daily_traffic WHERE day >= ?
            """,
            (start_ymd,),
        ).fetchone()

        all_sum = aconn.execute(
            """
            SELECT
                COALESCE(SUM(views), 0) AS views,
                COALESCE(SUM(api_hits), 0) AS api_hits,
                COALESCE(SUM(bot_hits), 0) AS bot_hits,
                COALESCE(SUM(vod_plays), 0) AS vod_plays,
                COALESCE(SUM(vod_downloads), 0) AS vod_downloads,
                COALESCE(SUM(bytes_sent), 0) AS bytes_sent,
                COALESCE(SUM(blocked_hits), 0) AS blocked_hits
            FROM daily_traffic
            """
        ).fetchone()

        daily_map = {
            r["day"]: dict(r)
            for r in aconn.execute("SELECT * FROM daily_traffic WHERE day >= ?", (start_ymd,)).fetchall()
        }
        daily_uniq_map = {
            r["day"]: int(r["c"])
            for r in aconn.execute(
                "SELECT day, COUNT(*) AS c FROM daily_visitors WHERE day >= ? GROUP BY day",
                (start_ymd,),
            ).fetchall()
        }

        hourly_map = {
            int(r["hour_bucket"]): dict(r)
            for r in aconn.execute(
                "SELECT * FROM hourly_traffic WHERE hour_bucket >= ?",
                (start_hbucket,),
            ).fetchall()
        }
        hourly_uniq_map = {
            int(r["hour_bucket"]): int(r["c"])
            for r in aconn.execute(
                "SELECT hour_bucket, COUNT(*) AS c FROM hourly_visitors WHERE hour_bucket >= ? GROUP BY hour_bucket",
                (start_hbucket,),
            ).fetchall()
        }

        page_rows = aconn.execute(
            "SELECT page, SUM(views) AS v FROM page_stats WHERE day >= ? GROUP BY page ORDER BY v DESC",
            (start_ymd,),
        ).fetchall()
        ref_rows = aconn.execute(
            "SELECT source, SUM(views) AS v FROM referrer_stats WHERE day >= ? GROUP BY source ORDER BY v DESC LIMIT 10",
            (start_ymd,),
        ).fetchall()
        dev_rows = aconn.execute(
            "SELECT kind, name, SUM(views) AS v FROM device_stats WHERE day >= ? GROUP BY kind, name ORDER BY v DESC",
            (start_ymd,),
        ).fetchall()
        vod_stat_map = {
            r["vod_id"]: dict(r)
            for r in aconn.execute("SELECT * FROM vod_analytics").fetchall()
        }
        audit_rows = [
            dict(r)
            for r in aconn.execute("SELECT * FROM admin_audit_log ORDER BY id DESC LIMIT 60").fetchall()
        ]

    # Почасовой ряд за последние 24ч
    hourly_24h: list[dict[str, Any]] = []
    for hb in range(start_hbucket, cur_hbucket + 1):
        hr = hourly_map.get(hb, {})
        ts_h = hb * 3600
        hourly_24h.append(
            {
                "ts": ts_h,
                "label": time.strftime("%H:00", time.localtime(ts_h)),
                "views": int(hr.get("views") or 0),
                "uniques": int(hourly_uniq_map.get(hb, 0)),
                "vod_plays": int(hr.get("vod_plays") or 0),
                "api_hits": int(hr.get("api_hits") or 0),
            }
        )

    # Посуточный ряд за выбранный период
    daily_series: list[dict[str, Any]] = []
    for offset in range(span_days - 1, -1, -1):
        d_ts = now - offset * 86400
        ymd = time.strftime("%Y-%m-%d", time.localtime(d_ts))
        dr = daily_map.get(ymd, {})
        daily_series.append(
            {
                "date": ymd,
                "label": _fmt_short_day_ru(ymd),
                "views": int(dr.get("views") or 0),
                "uniques": int(daily_uniq_map.get(ymd, 0)),
                "vod_plays": int(dr.get("vod_plays") or 0),
                "bytes_sent": int(dr.get("bytes_sent") or 0),
            }
        )

    page_labels = {v[0]: v[1] for v in _TRACKED_PAGES.values()}
    total_pg_views = sum(int(r["v"] or 0) for r in page_rows) or 1
    pages_breakdown = [
        {
            "path": r["page"],
            "label": page_labels.get(r["page"], r["page"]),
            "views": int(r["v"] or 0),
            "pct": round(int(r["v"] or 0) * 100.0 / total_pg_views, 1),
        }
        for r in page_rows
    ]

    total_refs = sum(int(r["v"] or 0) for r in ref_rows) or 1
    referrers_breakdown = [
        {
            "source": r["source"],
            "views": int(r["v"] or 0),
            "pct": round(int(r["v"] or 0) * 100.0 / total_refs, 1),
        }
        for r in ref_rows
    ]

    grouped_devs: dict[str, list[dict[str, Any]]] = {"device": [], "browser": [], "os": [], "country": []}
    kind_totals: dict[str, int] = {"device": 0, "browser": 0, "os": 0, "country": 0}
    for r in dev_rows:
        k = r["kind"]
        if k in kind_totals:
            kind_totals[k] += int(r["v"] or 0)
    for r in dev_rows:
        k = r["kind"]
        if k not in grouped_devs or len(grouped_devs[k]) >= 8:
            continue
        v = int(r["v"] or 0)
        raw_name = str(r["name"] or "")
        label = (
            ("Локальная сеть" if raw_name == "LOCAL" else ("Прямое подключение" if raw_name == "DIRECT" else _COUNTRY_LABELS.get(raw_name, raw_name)))
            if k == "country"
            else raw_name
        )
        grouped_devs[k].append(
            {
                "code": raw_name,
                "label": label,
                "views": v,
                "pct": round(v * 100.0 / max(1, kind_totals[k]), 1),
            }
        )

    # Статистика коллекций
    with _db_lock, db_connect() as conn:
        meta_rows = {
            r["name"]: {"revision": int(r["revision"] or 0), "updated_at": int(r["updated_at"] or 0)}
            for r in conn.execute("SELECT name, revision, updated_at FROM collection_meta").fetchall()
        }

    wl_state = read_list("watchlist")
    gl_state = read_list("gamelist")
    st_state = read_list("setup")
    ws_state = read_list("wishlist")

    wl_items = wl_state["items"]
    gl_items = gl_state["items"]
    st_items = st_state["items"]
    ws_items = ws_state["items"]

    rated_games = [float(g.get("rating") or 0) for g in gl_items if float(g.get("rating") or 0) > 0]
    avg_game_rating = round(sum(rated_games) / len(rated_games), 2) if rated_games else 0.0
    rated_titles = [float(i.get("rating") or 0) for i in wl_items if float(i.get("rating") or 0) > 0]
    avg_title_rating = round(sum(rated_titles) / len(rated_titles), 2) if rated_titles else 0.0
    wl_tags: dict[str, int] = {}
    for item in wl_items:
        for tag in item.get("tags") or []:
            wl_tags[tag] = wl_tags.get(tag, 0) + 1
    gl_tags: dict[str, int] = {}
    for item in gl_items:
        for tag in item.get("tags") or []:
            gl_tags[tag] = gl_tags.get(tag, 0) + 1
    ws_tags: dict[str, int] = {}
    for item in ws_items:
        for tag in item.get("tags") or []:
            ws_tags[tag] = ws_tags.get(tag, 0) + 1

    def _top_tags(counter: dict[str, int], limit: int = 6) -> list[dict[str, Any]]:
        return [
            {"tag": tag, "count": count}
            for tag, count in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
        ]
    wish_budget: dict[str, float] = {"RUB": 0.0, "USD": 0.0, "EUR": 0.0}
    for w in ws_items:
        if w.get("status") != "bought":
            cur = w.get("currency") if w.get("currency") in wish_budget else "RUB"
            price_val = float(w.get("price") or 0)
            if price_val > 0:
                wish_budget[cur] += price_val

    collections_summary = {
        "total_items": len(wl_items) + len(gl_items) + len(st_items) + len(ws_items),
        "watchlist": {
            "total": len(wl_items),
            "revision": wl_state["revision"],
            "updated_at": meta_rows.get("watchlist", {}).get("updated_at", 0),
            "by_type": {
                "anime": sum(1 for i in wl_items if i.get("type") == "anime"),
                "movie": sum(1 for i in wl_items if i.get("type") == "movie"),
                "series": sum(1 for i in wl_items if i.get("type") == "series"),
            },
            "by_status": {
                "watched": sum(1 for i in wl_items if i.get("status") == "watched"),
                "planned": sum(1 for i in wl_items if i.get("status") == "planned"),
                "dropped": sum(1 for i in wl_items if i.get("status") == "dropped"),
            },
            "avg_rating": avg_title_rating,
            "rated_count": len(rated_titles),
            "reviews_count": sum(1 for i in wl_items if str(i.get("review") or "").strip()),
            "favorites": sum(1 for i in wl_items if i.get("favorite")),
            "top_tags": _top_tags(wl_tags),
        },
        "gamelist": {
            "total": len(gl_items),
            "revision": gl_state["revision"],
            "updated_at": meta_rows.get("gamelist", {}).get("updated_at", 0),
            "avg_rating": avg_game_rating,
            "reviews_count": sum(1 for i in gl_items if str(i.get("review") or "").strip()),
            "rated_count": len(rated_games),
            "favorites": sum(1 for i in gl_items if i.get("favorite")),
            "top_tags": _top_tags(gl_tags),
            "by_status": {
                "completed": sum(1 for i in gl_items if i.get("status") == "completed"),
                "playing": sum(1 for i in gl_items if i.get("status") == "playing"),
                "paused": sum(1 for i in gl_items if i.get("status") == "paused"),
                "wishlist": sum(1 for i in gl_items if i.get("status") == "wishlist"),
                "dropped": sum(1 for i in gl_items if i.get("status") == "dropped"),
            },
        },
        "setup": {
            "total": len(st_items),
            "revision": st_state["revision"],
            "updated_at": meta_rows.get("setup", {}).get("updated_at", 0),
            "pc_count": sum(1 for i in st_items if i.get("group") == "pc"),
            "gear_count": sum(1 for i in st_items if i.get("group") == "gear"),
        },
        "wishlist": {
            "total": len(ws_items),
            "revision": ws_state["revision"],
            "updated_at": meta_rows.get("wishlist", {}).get("updated_at", 0),
            "by_status": {
                "want": sum(1 for i in ws_items if i.get("status") == "want"),
                "saving": sum(1 for i in ws_items if i.get("status") == "saving"),
                "bought": sum(1 for i in ws_items if i.get("status") == "bought"),
            },
            "budget": {k: round(v, 2) for k, v in wish_budget.items()},
            "favorites": sum(1 for i in ws_items if i.get("favorite")),
            "tagged": sum(1 for i in ws_items if i.get("tags")),
            "top_tags": _top_tags(ws_tags),
        },
    }

    # Статистика VOD-архива
    all_vods = list_vods(force_sync=False, include_hidden=True)
    enriched_vods: list[dict[str, Any]] = []
    tot_vod_plays = 0
    tot_vod_dls = 0
    tot_vod_bytes = 0
    for v in all_vods:
        vid = str(v.get("id") or "")
        vst = vod_stat_map.get(vid, {})
        plays = int(vst.get("plays") or 0)
        dls = int(vst.get("downloads") or 0)
        chats = int(vst.get("chat_loads") or 0)
        b_streamed = int(vst.get("bytes_streamed") or 0)
        tot_vod_plays += plays
        tot_vod_dls += dls
        tot_vod_bytes += b_streamed
        stem = _vod_cache_stem(vid)
        has_cached_thumb = (THUMB_CACHE_DIR / f"thumb_{stem}.jpg").is_file()
        enriched_vods.append(
            {
                "id": vid,
                "source": v.get("source"),
                "title": v.get("title"),
                "orig_title": v.get("orig_title") or v.get("title"),
                "custom_title": v.get("custom_title") or "",
                "note": v.get("note") or "",
                "orig_note": v.get("orig_note") or "",
                "custom_note": v.get("custom_note") or "",
                "hidden": bool(v.get("hidden")),
                "game": v.get("game") or "",
                "size_label": v.get("size_label") or "",
                "duration_s": int(v.get("duration_s") or 0),
                "has_chat": bool(v.get("has_chat")),
                "has_cached_thumb": has_cached_thumb,
                "thumb_url": v.get("thumb_url"),
                "view_url": v.get("view_url") or "",
                "plays": plays,
                "downloads": dls,
                "chat_loads": chats,
                "bytes_streamed": b_streamed,
                "last_played_at": int(vst.get("last_played_at") or 0),
            }
        )

    # Кэши и файловая система
    thumb_stats = _dir_usage_stats(THUMB_CACHE_DIR, "thumb_*.jpg")
    chat_stats = _dir_usage_stats(CHAT_CACHE_DIR, "chat_*.jsonl")
    img_stats = _dir_usage_stats(IMG_CACHE_DIR, "img_*.*")
    with _api_cache_lock:
        api_cache_count = sum(1 for exp, _, _ in _api_cache.values() if exp > now)
    with _rate_lock:
        rate_buckets_count = len(_rate_buckets)

    disk_total, disk_used, disk_free = 0, 0, 0
    with contextlib.suppress(OSError):
        du = shutil.disk_usage(DATA_DIR)
        disk_total, disk_used, disk_free = int(du.total), int(du.used), int(du.free)

    load_1m = None
    if hasattr(os, "getloadavg"):
        with contextlib.suppress(OSError):
            load_1m = round(os.getloadavg()[0], 2)

    cur_sha = ""
    with contextlib.suppress(OSError):
        if UPDATE_SHA_PATH.is_file():
            cur_sha = UPDATE_SHA_PATH.read_text(encoding="utf-8").strip()[:12]

    tokens = read_tokens()
    rec_enabled = os.environ.get("RECORDER") == "1"
    rec_alive = bool(_ACTIVE_REC_PROC is not None and _ACTIVE_REC_PROC.poll() is None)

    req_cnt_period = int(period_sum["req_cnt"] or 0)
    avg_lat_ms = round(float(period_sum["lat_sum"] or 0.0) / req_cnt_period, 1) if req_cnt_period > 0 else 0.0
    today_req_cnt = int(today_row["req_count"] or 0) if today_row else 0
    today_lat_ms = (
        round(float(today_row["latency_sum_ms"] or 0.0) / today_req_cnt, 1)
        if (today_row and today_req_cnt > 0)
        else 0.0
    )

    return j(
        {
            "ok": True,
            "span_days": span_days,
            "realtime": {
                "online_now": online_now,
                "active_pages": active_by_page,
                "server_time": int(now),
                "uptime_s": max(0, int(now - _SERVER_STARTED_AT)),
            },
            "kpis": {
                "today": {
                    "views": int(today_row["views"] or 0) if today_row else 0,
                    "uniques": today_uniques,
                    "vod_plays": int(today_row["vod_plays"] or 0) if today_row else 0,
                    "vod_downloads": int(today_row["vod_downloads"] or 0) if today_row else 0,
                    "api_hits": int(today_row["api_hits"] or 0) if today_row else 0,
                    "bot_hits": int(today_row["bot_hits"] or 0) if today_row else 0,
                    "blocked_hits": int(today_row["blocked_hits"] or 0) if today_row else 0,
                    "bytes_sent": int(today_row["bytes_sent"] or 0) if today_row else 0,
                    "avg_latency_ms": today_lat_ms,
                },
                "period": {
                    "views": int(period_sum["views"] or 0),
                    "uniques": period_uniques,
                    "vod_plays": int(period_sum["vod_plays"] or 0),
                    "vod_downloads": int(period_sum["vod_downloads"] or 0),
                    "api_hits": int(period_sum["api_hits"] or 0),
                    "bot_hits": int(period_sum["bot_hits"] or 0),
                    "blocked_hits": int(period_sum["blocked_hits"] or 0),
                    "bytes_sent": int(period_sum["bytes_sent"] or 0),
                    "avg_latency_ms": avg_lat_ms,
                    "status_2xx": int(period_sum["s2xx"] or 0),
                    "status_3xx": int(period_sum["s3xx"] or 0),
                    "status_4xx": int(period_sum["s4xx"] or 0),
                    "status_5xx": int(period_sum["s5xx"] or 0),
                },
                "all_time": {
                    "views": int(all_sum["views"] or 0),
                    "uniques": all_uniques,
                    "vod_plays": int(all_sum["vod_plays"] or 0),
                    "vod_downloads": int(all_sum["vod_downloads"] or 0),
                    "api_hits": int(all_sum["api_hits"] or 0),
                    "bot_hits": int(all_sum["bot_hits"] or 0),
                    "blocked_hits": int(all_sum["blocked_hits"] or 0),
                    "bytes_sent": int(all_sum["bytes_sent"] or 0),
                },
            },
            "charts": {
                "hourly_24h": hourly_24h,
                "daily": daily_series,
            },
            "breakdowns": {
                "pages": pages_breakdown,
                "referrers": referrers_breakdown,
                "devices": grouped_devs["device"],
                "browsers": grouped_devs["browser"],
                "os": grouped_devs["os"],
                "countries": grouped_devs["country"],
            },
            "collections": collections_summary,
            "vods": {
                "total": len(enriched_vods),
                "visible_count": sum(1 for v in enriched_vods if not v["hidden"]),
                "hidden_count": sum(1 for v in enriched_vods if v["hidden"]),
                "gdrive_count": sum(1 for v in enriched_vods if v["source"] == "gdrive"),
                "local_count": sum(1 for v in enriched_vods if v["source"] == "local"),
                "total_plays": tot_vod_plays,
                "total_downloads": tot_vod_dls,
                "total_bytes_streamed": tot_vod_bytes,
                "last_sync_ts": int(_drive_sync_ts or 0),
                "folder_id": GDRIVE_FOLDER_ID,
                "folder_url": f"https://drive.google.com/drive/folders/{GDRIVE_FOLDER_ID}" if GDRIVE_FOLDER_ID else "",
                "items": enriched_vods,
            },
            "caches": {
                "vod_thumbs": thumb_stats,
                "vod_chats": chat_stats,
                "img_proxy": img_stats,
                "api_mem_entries": api_cache_count,
                "rate_buckets": rate_buckets_count,
            },
            "geo": geo_status(),
            "system": {
                "python": platform.python_version(),
                "platform": f"{platform.system()} {platform.release()} ({platform.machine()})",
                "pid": os.getpid(),
                "cpu_count": os.cpu_count() or 1,
                "load_1m": load_1m,
                "rss_bytes": _process_rss_bytes(),
                "disk_total_bytes": disk_total,
                "disk_used_bytes": disk_used,
                "disk_free_bytes": disk_free,
                "disk_used_pct": round(disk_used * 100.0 / disk_total, 1) if disk_total > 0 else 0.0,
                "db_sizes": {
                    "krafen_db": _file_size_safe(DB_PATH),
                    "auth_db": _file_size_safe(AUTH_DB_PATH),
                    "analytics_db": _file_size_safe(ANALYTICS_DB_PATH),
                    "server_log": _file_size_safe(HEADLESS_LOG_PATH),
                },
                "is_pterodactyl": _is_pterodactyl_env(),
                "auto_update_enabled": is_auto_update_enabled(),
                "auto_update_repo": f"{AUTO_UPDATE_REPO}@{AUTO_UPDATE_BRANCH}",
                "current_sha": cur_sha,
            },
            "integrations": [
                {
                    "id": "admin_auth",
                    "name": "Админ-защита и сессии",
                    "status": "ok" if ADMIN_PASSWORD else "error",
                    "detail": "Пароль задан, HMAC + CSRF активны" if ADMIN_PASSWORD else "Пароль не задан или равен change-me",
                },
                {
                    "id": "public_origin",
                    "name": "PUBLIC_ORIGIN",
                    "status": "ok" if PUBLIC_ORIGIN else "warn",
                    "detail": PUBLIC_ORIGIN or "Автоопределение по заголовку Host",
                },
                {
                    "id": "spotify",
                    "name": "Spotify Web API",
                    "status": (
                        "ok"
                        if (SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET and tokens.get("spotify_refresh_token"))
                        else ("warn" if (SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET) else "muted")
                    ),
                    "detail": (
                        "Аккаунт привязан (живой трек активен)"
                        if tokens.get("spotify_refresh_token")
                        else ("Ключи есть, требуется привязка OAuth" if SPOTIFY_CLIENT_ID else "Не настроено (работает Last.fm)")
                    ),
                    "connected": bool(tokens.get("spotify_refresh_token")),
                },
                {
                    "id": "lastfm",
                    "name": "Last.fm Scrobbler",
                    "status": "ok" if LASTFM_API_KEY else "muted",
                    "detail": f"Профиль @{LASTFM_USER}" if LASTFM_API_KEY else "Ключ LASTFM_API_KEY не указан",
                },
                {
                    "id": "twitch",
                    "name": "Twitch Helix & IGDB",
                    "status": "ok" if (TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET) else "warn",
                    "detail": f"Канал @{TWITCH_CHANNEL}" if TWITCH_CLIENT_ID else "Ключи TWITCH_CLIENT_ID/SECRET не заданы",
                },
                {
                    "id": "tmdb",
                    "name": "TMDB + Shikimori",
                    "status": "ok" if (TMDB_ACCESS_TOKEN or TMDB_API_KEY) else "warn",
                    "detail": "Поиск фильмов, сериалов и аниме готов" if (TMDB_ACCESS_TOKEN or TMDB_API_KEY) else "Shikimori работает, TMDB без ключа",
                },
                {
                    "id": "discord",
                    "name": "Discord Виджет",
                    "status": "ok" if DISCORD_INVITE_CODE else "muted",
                    "detail": f"Инвайт discord.gg/{DISCORD_INVITE_CODE}" if DISCORD_INVITE_CODE else "Не задан код приглашения",
                },
                {
                    "id": "gdrive",
                    "name": "Google Drive VOD-архив",
                    "status": "ok" if GDRIVE_FOLDER_ID else "muted",
                    "detail": (
                        f"Папка подключена ({'API v3' if GDRIVE_API_KEY else 'публичный режим'})"
                        if GDRIVE_FOLDER_ID
                        else "Папка не задана"
                    ),
                },
                {
                    "id": "github_update",
                    "name": "Автообновление GitHub",
                    "status": "ok" if is_auto_update_enabled() else "muted",
                    "detail": (
                        f"Включено ({'токен задан' if _resolve_github_token() else 'публичный доступ'})"
                        if is_auto_update_enabled()
                        else "Отключено (локальная git-рабочая копия)"
                    ),
                },
                {
                    "id": "recorder",
                    "name": "Рекордер стримов",
                    "status": "ok" if rec_alive else ("warn" if rec_enabled else "muted"),
                    "detail": (
                        f"Активен (@{os.environ.get('RECORDER_CHANNEL', 'danonsha')})"
                        if rec_alive
                        else ("Включён в .env, но процесс остановлен" if rec_enabled else "Выключен на этом узле (RECORDER=0)")
                    ),
                },
            ],
            "audit_log": audit_rows,
        }
    )


@app.post("/api/admin/vods/sync")
def admin_sync_vods(request: Request) -> JSONResponse:
    global _drive_sync_ts
    require_same_origin(request)
    require_admin(request)
    rate_limit(request, "admin_vod_sync", 12, 60)
    if not GDRIVE_FOLDER_ID:
        return j({"error": "gdrive_not_configured"}, 400)
    with _drive_sync_lock:
        _drive_sync_ts = time.time()
    _sync_drive_vods_worker(GDRIVE_FOLDER_ID, force=True)
    vods = list_vods(force_sync=False, include_hidden=True)
    log_admin_event(request, "vod_sync", "ok", f"Синхронизировано записей: {len(vods)}")
    return j({"ok": True, "count": len(vods)})


@app.post("/api/admin/vods/override")
def admin_vod_override(request: Request, body: dict[str, Any]) -> JSONResponse:
    require_same_origin(request)
    require_admin(request)
    rate_limit(request, "admin_vod_override", 60, 60)
    vod_id = text(body.get("vod_id"), 160)
    if not safe_name(vod_id):
        return j({"error": "invalid_vod_id"}, 400)

    overrides = read_vod_overrides()
    if body.get("reset"):
        overrides.pop(vod_id, None)
        write_vod_overrides(overrides)
        log_admin_event(request, "vod_override", "ok", f"Сброшены правки VOD {vod_id}")
        return j({"ok": True, "vod_id": vod_id, "override": None})

    custom_title = text(body.get("custom_title"), 260)
    custom_note = text(body.get("custom_note"), 180)
    hidden = bool(body.get("hidden"))
    if not custom_title and not custom_note and not hidden:
        overrides.pop(vod_id, None)
    else:
        overrides[vod_id] = {
            "custom_title": custom_title,
            "custom_note": custom_note,
            "hidden": hidden,
            "updated_at": int(time.time()),
        }
    write_vod_overrides(overrides)
    log_admin_event(
        request,
        "vod_override",
        "ok",
        f"VOD {vod_id}: hidden={hidden}, note={bool(custom_note)}, title={bool(custom_title)}",
    )
    return j({"ok": True, "vod_id": vod_id, "override": overrides.get(vod_id)})


@app.post("/api/admin/vods/reset-thumb")
def admin_vod_reset_thumb(request: Request, body: dict[str, Any]) -> JSONResponse:
    require_same_origin(request)
    require_admin(request)
    rate_limit(request, "admin_reset_thumb", 30, 60)
    target = text(body.get("vod_id"), 160)
    removed = 0
    if target == "all":
        if THUMB_CACHE_DIR.is_dir():
            for p in THUMB_CACHE_DIR.glob("thumb_*.jpg"):
                with contextlib.suppress(OSError):
                    p.unlink()
                    removed += 1
        log_admin_event(request, "reset_thumb", "ok", f"Сброшены все превью ({removed} шт.)")
        return j({"ok": True, "removed": removed})

    if not safe_name(target):
        return j({"error": "invalid_vod_id"}, 400)
    stem = _vod_cache_stem(target)
    tpath = THUMB_CACHE_DIR / f"thumb_{stem}.jpg"
    if tpath.is_file():
        with contextlib.suppress(OSError):
            tpath.unlink()
            removed = 1
    log_admin_event(request, "reset_thumb", "ok", f"Сброшено превью {target}")
    return j({"ok": True, "removed": removed})


@app.post("/api/admin/cache/clear")
def admin_clear_cache(request: Request, body: dict[str, Any]) -> JSONResponse:
    require_same_origin(request)
    require_admin(request)
    rate_limit(request, "admin_cache_clear", 20, 60)
    target = text(body.get("target"), 40) or "api"
    freed_bytes = 0
    removed_files = 0

    def _purge_dir(directory: Path, pattern: str) -> None:
        nonlocal freed_bytes, removed_files
        if not directory.is_dir():
            return
        for p in directory.glob(pattern):
            if p.is_file():
                with contextlib.suppress(OSError):
                    freed_bytes += p.stat().st_size
                    p.unlink()
                    removed_files += 1

    if target in ("api", "all_caches"):
        with _api_cache_lock:
            removed_files += len(_api_cache)
            _api_cache.clear()
        _list_cache.clear()
    if target in ("img", "all_caches"):
        _purge_dir(IMG_CACHE_DIR, "img_*.*")
    if target in ("thumbs", "all_caches"):
        _purge_dir(THUMB_CACHE_DIR, "thumb_*.jpg")
    if target in ("chats", "all_caches"):
        _purge_dir(CHAT_CACHE_DIR, "chat_*.jsonl")
    if target == "analytics":
        with _analytics_buf_lock:
            _buf_daily.clear()
            _buf_hourly.clear()
            _buf_visitors.clear()
            _buf_hourly_visitors.clear()
            _buf_pages.clear()
            _buf_referrers.clear()
            _buf_devices.clear()
            _buf_vods.clear()
            _active_visitors.clear()
            _recent_vod_events.clear()
        with _analytics_db_lock, analytics_db_connect() as aconn:
            for tbl in (
                "daily_traffic",
                "hourly_traffic",
                "daily_visitors",
                "hourly_visitors",
                "page_stats",
                "referrer_stats",
                "device_stats",
                "vod_analytics",
            ):
                aconn.execute(f"DELETE FROM {tbl}")

    log_admin_event(request, "cache_clear", "ok", f"target={target}, removed={removed_files}, bytes={freed_bytes}")
    return j({"ok": True, "target": target, "removed": removed_files, "freed_bytes": freed_bytes})


@app.post("/api/admin/spotify/disconnect")
def admin_spotify_disconnect(request: Request) -> JSONResponse:
    require_same_origin(request)
    require_admin(request)
    rate_limit(request, "admin_spotify_disc", 10, 60)
    tokens = read_tokens()
    tokens.pop("spotify_refresh_token", None)
    write_tokens(tokens)
    with _api_cache_lock:
        for key in ("spotify", "spotify_now", "lastfm"):
            _api_cache.pop(key, None)
    log_admin_event(request, "spotify_disconnect", "ok", "Отключён токен Spotify")
    return j({"ok": True})


@app.get("/api/admin/backup")
def admin_export_backup(request: Request) -> Response:
    require_admin(request)
    rate_limit(request, "admin_backup", 15, 60)
    snapshot = {
        "schema": "krafen-backup-v1",
        "exported_at": int(time.time()),
        "exported_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "collections": {name: read_list(name, use_cache=False) for name in LIST_SPECS},
        "vod_overrides": read_vod_overrides(),
    }
    raw = json.dumps(snapshot, ensure_ascii=False, indent=2).encode("utf-8")
    fname = f"krafen-backup-{time.strftime('%Y-%m-%d')}.json"
    log_admin_event(request, "backup_export", "ok", f"Экспорт {fname} ({len(raw)} байт)")
    return Response(
        content=raw,
        status_code=200,
        media_type="application/json; charset=utf-8",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": f'attachment; filename="{fname}"',
        },
    )


@app.post("/api/admin/restore")
def admin_import_backup(request: Request, body: dict[str, Any]) -> JSONResponse:
    require_same_origin(request)
    require_admin(request)
    rate_limit(request, "admin_restore", 6, 60)
    cols = body.get("collections")
    if not isinstance(cols, dict):
        return j({"error": "invalid_backup_format"}, 400)

    restored_counts: dict[str, int] = {}
    now = int(time.time())
    with _db_lock, db_connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for name, spec in LIST_SPECS.items():
                if name not in cols:
                    continue
                entry = cols[name]
                raw_items = entry.get("items") if isinstance(entry, dict) else (entry if isinstance(entry, list) else None)
                if not isinstance(raw_items, list):
                    continue
                norm_fn = NORMALIZERS[spec["normalizer"]]
                cleaned = [x for x in (norm_fn(it) for it in raw_items) if x is not None][: spec["max_items"]]
                # Непустой бэкап, из которого не выжила ни одна запись, — это повреждённый
                # файл, а не намеренная очистка: иначе импорт молча стирает коллекцию.
                if raw_items and not cleaned:
                    conn.execute("ROLLBACK")
                    return j({"error": "invalid_items", "collection": name}, 400)
                row = conn.execute("SELECT revision FROM collection_meta WHERE name = ?", (name,)).fetchone()
                next_rev = (max(0, int(row["revision"] or 0)) if row else 0) + 1
                stored = _write_collection_items(conn, name, cleaned)
                conn.execute(
                    """
                    INSERT INTO collection_meta (name, revision, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(name) DO UPDATE SET revision = excluded.revision, updated_at = excluded.updated_at
                    """,
                    (name, next_rev, now),
                )
                _list_cache[name] = {"ts": time.time(), "revision": next_rev, "items": list(stored)}
                restored_counts[name] = len(stored)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            return j({"error": "restore_failed"}, 500)

    raw_overrides = body.get("vod_overrides")
    if isinstance(raw_overrides, dict):
        safe_ov: dict[str, dict[str, Any]] = {}
        for k, v in list(raw_overrides.items())[:500]:
            vid = text(k, 160)
            if safe_name(vid) and isinstance(v, dict):
                safe_ov[vid] = {
                    "custom_title": text(v.get("custom_title"), 260),
                    "custom_note": text(v.get("custom_note"), 180),
                    "hidden": bool(v.get("hidden")),
                    "updated_at": now,
                }
        write_vod_overrides(safe_ov)

    summary_str = ", ".join(f"{k}:{v}" for k, v in restored_counts.items()) or "пусто"
    log_admin_event(request, "backup_restore", "ok", f"Восстановлено ({summary_str})")
    return j({"ok": True, "restored": restored_counts})


_LOG_REDACT_RE = re.compile(
    r"(Bearer\s+[A-Za-z0-9._~+/=-]{8,}|github_pat_[A-Za-z0-9_]+|gh[pousr]_[A-Za-z0-9_]+)",
    flags=re.IGNORECASE,
)


@app.get("/api/admin/logs")
def admin_read_logs(request: Request, lines: int = 150) -> JSONResponse:
    require_admin(request)
    rate_limit(request, "admin_logs", 60, 60)
    max_lines = max(20, min(600, int(lines or 150)))
    if not HEADLESS_LOG_PATH.is_file():
        return j({"ok": True, "exists": False, "size_bytes": 0, "lines": []})
    try:
        st_size = HEADLESS_LOG_PATH.stat().st_size
        with open(HEADLESS_LOG_PATH, "rb") as fh:
            if st_size > 196_608:
                fh.seek(-196_608, os.SEEK_END)
            raw_tail = fh.read().decode("utf-8", errors="replace")
        tail_lines = [
            _LOG_REDACT_RE.sub("[REDACTED]", ln)
            for ln in raw_tail.splitlines()[-max_lines:]
        ]
        return j({"ok": True, "exists": True, "size_bytes": st_size, "lines": tail_lines})
    except OSError:
        return j({"ok": True, "exists": False, "size_bytes": 0, "lines": []})


# ----------------------------- Поиск (TMDB / Shikimori / IGDB) -----------------------------


def tmdb_poster(path: Any) -> str | None:
    value = text(path, 120)
    return f"{TMDB_IMAGE_BASE}{value}" if re.fullmatch(r"/[A-Za-z0-9_.-]+", value) else None


def igdb_cover(image_id: Any) -> str | None:
    value = text(image_id, 80)
    return f"https://images.igdb.com/igdb/image/upload/t_cover_big/{value}.jpg" if re.fullmatch(r"[A-Za-z0-9_-]+", value) else None


def search_tmdb(query: str, item_type: str) -> JSONResponse:
    if not TMDB_ACCESS_TOKEN and not TMDB_API_KEY:
        return j({"error": "tmdb_key_missing"}, 503)
    endpoint = "/3/search/tv" if item_type == "series" else "/3/search/movie"
    params: dict[str, Any] = {"query": query, "language": "ru-RU", "include_adult": "false", "page": 1}
    headers = {"Accept": "application/json"}
    if TMDB_ACCESS_TOKEN:
        headers["Authorization"] = f"Bearer {TMDB_ACCESS_TOKEN.removeprefix('Bearer ')}"
    else:
        params["api_key"] = TMDB_API_KEY
    try:
        res = requests.get(f"{TMDB_BASE}{endpoint}", params=params, headers=headers, timeout=15)
        data = res.json() if res.content else {}
        if not res.ok:
            return j({"error": "tmdb_unavailable"}, 502)
        results = []
        for item in (data.get("results") or [])[:10]:
            localized = text(item.get("name") if item_type == "series" else item.get("title"))
            original = text(item.get("original_name") if item_type == "series" else item.get("original_title")) or localized
            if not original:
                continue
            results.append(
                {
                    "id": f"tmdb_{'tv' if item_type == 'series' else 'movie'}_{text(item.get('id'), 40)}",
                    "title": original,
                    "titleRu": localized if localized and localized != original else None,
                    "year": year_from(item.get("first_air_date") if item_type == "series" else item.get("release_date")),
                    "type": item_type,
                    "poster": clean_url(tmdb_poster(item.get("poster_path"))),
                }
            )
        return j({"results": results})
    except Exception:
        return j({"error": "tmdb_unavailable"}, 502)


def search_shikimori(query: str) -> JSONResponse:
    try:
        res = requests.get(
            f"{SHIKIMORI_BASE}/api/animes",
            params={"search": query, "limit": 10, "order": "popularity"},
            headers={"Accept": "application/json", "User-Agent": "krafen-site/2.0"},
            timeout=15,
        )
        data = res.json() if res.content else []
        if not res.ok or not isinstance(data, list):
            return j({"error": "shikimori_unavailable"}, 502)
        results = []
        for item in data:
            item_id = text(item.get("id"), 40)
            title = text(item.get("name"))
            if not item_id or not title:
                continue
            image = item.get("image") if isinstance(item.get("image"), dict) else {}
            original = text(image.get("original"), 300)
            poster_url = None
            if original:
                poster_url = clean_url(original if original.startswith("https://") else f"{SHIKIMORI_BASE}{original}")
            results.append(
                {
                    "id": f"sh_{item_id}",
                    "title": title,
                    "titleRu": text(item.get("russian")) or None,
                    "year": year_from(item.get("aired_on")),
                    "type": "anime",
                    "poster": poster_url,
                }
            )
        return j({"results": results})
    except Exception:
        return j({"error": "shikimori_unavailable"}, 502)


@app.get("/api/search")
def search(request: Request, q: str = "", type: str = "") -> JSONResponse:
    rate_limit(request, "search", 60, 60)
    query = text(q, 80)
    item_type = text(type, 20)
    if len(query) < 2:
        return j({"results": []})
    if item_type == "anime":
        return search_shikimori(query)
    if item_type in {"movie", "series"}:
        return search_tmdb(query, item_type)
    return j({"error": "invalid_type"}, 400)


def get_igdb_token(force_refresh: bool = False) -> str:
    global _igdb_token
    if IGDB_ACCESS_TOKEN:
        return IGDB_ACCESS_TOKEN.removeprefix("Bearer ")
    if not force_refresh and _igdb_token.get("access_token") and _igdb_token.get("expires_at", 0) > time.time() + 60:
        return _igdb_token["access_token"]
    if not IGDB_CLIENT_ID or not IGDB_CLIENT_SECRET:
        return ""
    res = requests.post(
        TWITCH_TOKEN_URL,
        data={
            "client_id": IGDB_CLIENT_ID,
            "client_secret": IGDB_CLIENT_SECRET,
            "grant_type": "client_credentials",
        },
        timeout=15,
    )
    data = res.json() if res.content else {}
    if not res.ok or not data.get("access_token"):
        return ""
    expires_in = int(data.get("expires_in") or 3600)
    _igdb_token = {"access_token": data["access_token"], "expires_at": time.time() + expires_in - 300}
    return data["access_token"]


def search_igdb(query: str) -> JSONResponse:
    client_id = IGDB_CLIENT_ID
    if not client_id or (not IGDB_ACCESS_TOKEN and not IGDB_CLIENT_SECRET):
        return j({"error": "igdb_key_missing"}, 503)
    try:
        token = get_igdb_token()
        if not token:
            return j({"error": "igdb_unavailable"}, 502)
        safe_query = text(query, 80).replace("\\", "\\\\").replace('"', '\\"')
        body = (
            "fields name,cover.image_id,genres.name,platforms.abbreviation,platforms.name,first_release_date,category; "
            f'search "{safe_query}"; where version_parent = null; limit 10;'
        )
        res = requests.post(
            f"{IGDB_BASE}/games",
            data=body,
            headers={"Accept": "application/json", "Client-ID": client_id, "Authorization": f"Bearer {token}"},
            timeout=15,
        )
        if res.status_code == 401 and not IGDB_ACCESS_TOKEN:
            token = get_igdb_token(force_refresh=True)
            if token:
                res = requests.post(
                    f"{IGDB_BASE}/games",
                    data=body,
                    headers={"Accept": "application/json", "Client-ID": client_id, "Authorization": f"Bearer {token}"},
                    timeout=15,
                )
        data = res.json() if res.content else []
        if not res.ok or not isinstance(data, list):
            return j({"error": "igdb_unavailable"}, 502)
        results = []
        for item in data:
            platforms = [
                text((p or {}).get("abbreviation") or (p or {}).get("name"), 24)
                for p in (item.get("platforms") or [])
                if isinstance(p, dict)
            ]
            genres = [text((g or {}).get("name"), 28) for g in (item.get("genres") or []) if isinstance(g, dict)]
            cover = item.get("cover") if isinstance(item.get("cover"), dict) else {}
            raw_id = text(item.get("id"), 40)
            title = text(item.get("name"), 180)
            if not raw_id or not title:
                continue
            results.append(
                {
                    "id": f"igdb_{raw_id}",
                    "igdbId": int(raw_id) if raw_id.isdigit() else None,
                    "title": title,
                    "year": year_from_unix(item.get("first_release_date")),
                    "type": "game",
                    "cover": igdb_cover(cover.get("image_id")),
                    "genres": [value for value in genres if value][:3],
                    "platforms": [value for value in platforms if value][:4],
                    "category": int(item.get("category") or 0),
                }
            )
        return j({"results": results})
    except Exception:
        return j({"error": "igdb_unavailable"}, 502)


@app.get("/api/game-search")
def game_search(request: Request, q: str = "") -> JSONResponse:
    rate_limit(request, "game_search", 60, 60)
    query = text(q, 80)
    if len(query) < 2:
        return j({"results": []})
    return search_igdb(query)


# ----------------------------- Музыка (Spotify + Last.fm) -----------------------------


@app.get("/api/lastfm")
def lastfm(request: Request) -> JSONResponse:
    rate_limit(request, "lastfm", 60, 60)
    if not LASTFM_API_KEY or not LASTFM_USER:
        return j({"error": "lastfm_key_missing"}, 503)

    hit = cached_api_get("lastfm")
    if hit:
        return j(hit[1], hit[0])

    try:
        res = requests.get(
            LASTFM_API_BASE,
            params={
                "method": "user.getrecenttracks",
                "user": LASTFM_USER,
                "api_key": LASTFM_API_KEY,
                "format": "json",
                "limit": 1,
            },
            timeout=15,
        )
        data = res.json() if res.content else {}
        if not res.ok or data.get("error"):
            return j({"error": "lastfm_unavailable"}, 502)
        recent = data.get("recenttracks") if isinstance(data.get("recenttracks"), dict) else {}
        raw_list = recent.get("track")
        raw_track = raw_list[0] if isinstance(raw_list, list) and raw_list else (raw_list if isinstance(raw_list, dict) else None)
        track = None
        if isinstance(raw_track, dict):
            artist = raw_track.get("artist") if isinstance(raw_track.get("artist"), dict) else {}
            album = raw_track.get("album") if isinstance(raw_track.get("album"), dict) else {}
            images = raw_track.get("image") if isinstance(raw_track.get("image"), list) else []
            image = None
            for image_item in reversed(images):
                if isinstance(image_item, dict) and image_item.get("#text"):
                    image = clean_url(image_item.get("#text"))
                    break
            attrs = raw_track.get("@attr") if isinstance(raw_track.get("@attr"), dict) else {}
            track = {
                "name": text(raw_track.get("name"), 200),
                "artist": text(artist.get("#text"), 200),
                "album": text(album.get("#text"), 200),
                "image": image,
                "nowPlaying": attrs.get("nowplaying") == "true",
            }
        payload = {"track": track}
        cached_api_set("lastfm", 5.0, 200, payload)
        return j(payload)
    except Exception:
        return j({"error": "lastfm_unavailable"}, 502)


def spotify_basic_auth() -> str:
    raw = f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def request_origin(request: Request) -> str:
    if PUBLIC_ORIGIN:
        return PUBLIC_ORIGIN
    proto = "https" if is_https_request(request) else "http"
    host = _effective_request_host(request)
    return f"{proto}://{host}"


def spotify_callback_url(request: Request) -> str:
    return f"{request_origin(request)}/api/spotify/callback"


@app.get("/api/spotify/auth")
def spotify_auth(request: Request):
    require_admin(request)
    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        return j({"error": "spotify_credentials_missing"}, 503)
    state = b64url(secrets.token_bytes(18))
    callback = spotify_callback_url(request)
    query = urlencode(
        {
            "response_type": "code",
            "client_id": SPOTIFY_CLIENT_ID,
            "redirect_uri": callback,
            "scope": "user-read-playback-state user-read-recently-played",
            "state": state,
        }
    )
    response = RedirectResponse(f"{SPOTIFY_AUTH_URL}?{query}", status_code=302)
    response.headers["Set-Cookie"] = cookie_header(request, SPOTIFY_STATE_COOKIE, state, 600, "/api/spotify/callback")
    return response


@app.get("/api/spotify/callback")
def spotify_callback(request: Request, state: str = "", code: str = "", error: str = "") -> Response:
    cookie_state = request.cookies.get(SPOTIFY_STATE_COOKIE, "")
    clear_cookie = cookie_header(request, SPOTIFY_STATE_COOKIE, "", 0, "/api/spotify/callback")
    if not cookie_state or not state or not constant_time_equal(cookie_state, state):
        return PlainTextResponse(
            "Проверка состояния авторизации Spotify не прошла. Вернитесь на сайт и попробуйте снова.",
            status_code=400,
            headers={"Set-Cookie": clear_cookie},
        )
    if error:
        response = RedirectResponse("/?spotify=error", status_code=303)
        response.headers["Set-Cookie"] = clear_cookie
        return response
    if not code or not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        return PlainTextResponse("Не заданы SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET.", status_code=503, headers={"Set-Cookie": clear_cookie})
    try:
        res = requests.post(
            SPOTIFY_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": spotify_callback_url(request),
            },
            headers={"Authorization": spotify_basic_auth(), "Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
        data = res.json() if res.content else {}
        if not res.ok or not data.get("refresh_token"):
            return PlainTextResponse(
                "Не удалось подключить Spotify. Проверьте Redirect URI и секреты в .env.",
                status_code=502,
                headers={"Set-Cookie": clear_cookie},
            )
        tokens = read_tokens()
        tokens["spotify_refresh_token"] = data["refresh_token"]
        tokens["spotify_access_token"] = {
            "access_token": data["access_token"],
            "expires_at": time.time() + int(data.get("expires_in") or 3600),
        }
        write_tokens(tokens)
        response = RedirectResponse("/?spotify=connected", status_code=303)
        response.headers["Set-Cookie"] = clear_cookie
        return response
    except Exception:
        return PlainTextResponse(
            "Ошибка соединения при подключении Spotify.",
            status_code=502,
            headers={"Set-Cookie": clear_cookie},
        )


def get_spotify_access_token(force_refresh: bool = False) -> str:
    tokens = read_tokens()
    cached = tokens.get("spotify_access_token") if isinstance(tokens.get("spotify_access_token"), dict) else None
    if not force_refresh and cached and cached.get("access_token") and int(cached.get("expires_at") or 0) > time.time() + 60:
        return cached["access_token"]
    refresh_token = text(tokens.get("spotify_refresh_token"), 4000)
    if not refresh_token or not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        return ""
    res = requests.post(
        SPOTIFY_TOKEN_URL,
        data={"grant_type": "refresh_token", "refresh_token": refresh_token},
        headers={
            "Authorization": spotify_basic_auth(),
            "Content-Type": "application/x-www-form-urlencoded",
        },
        timeout=15,
    )
    data = res.json() if res.content else {}
    if not res.ok or not data.get("access_token"):
        return ""
    if data.get("refresh_token"):
        tokens["spotify_refresh_token"] = data["refresh_token"]
    tokens["spotify_access_token"] = {
        "access_token": data["access_token"],
        "expires_at": time.time() + int(data.get("expires_in") or 3600),
    }
    write_tokens(tokens)
    return data["access_token"]


@app.get("/api/spotify")
def spotify(request: Request) -> JSONResponse:
    rate_limit(request, "spotify", 60, 60)

    hit = cached_api_get("spotify")
    if hit:
        return j(hit[1], hit[0])

    try:
        token = get_spotify_access_token()
    except Exception:
        token = ""
    if not token:
        return j({"error": "spotify_not_connected"}, 503)

    try:
        res = requests.get(f"{SPOTIFY_API_BASE}/me/player", headers={"Authorization": f"Bearer {token}"}, timeout=15)
        if res.status_code == 401:
            token = get_spotify_access_token(force_refresh=True)
            if token:
                res = requests.get(f"{SPOTIFY_API_BASE}/me/player", headers={"Authorization": f"Bearer {token}"}, timeout=15)

        item = None
        now_playing = False
        current_playback = False
        progress_ms = None
        if res.status_code == 200:
            data = res.json() if res.content else {}
            item = data.get("item") if isinstance(data.get("item"), dict) else None
            now_playing = bool(data.get("is_playing"))
            current_playback = bool(item)
            progress_ms = data.get("progress_ms")
        elif res.status_code == 204:
            recent_res = requests.get(
                f"{SPOTIFY_API_BASE}/me/player/recently-played?limit=1",
                headers={"Authorization": f"Bearer {token}"},
                timeout=15,
            )
            recent = recent_res.json() if recent_res.content else {}
            if not recent_res.ok:
                return j({"error": "spotify_recently_played_http_error", "status": recent_res.status_code}, 502)
            items = recent.get("items") if isinstance(recent.get("items"), list) else []
            item = items[0].get("track") if items and isinstance(items[0], dict) and isinstance(items[0].get("track"), dict) else None
        else:
            err = "spotify_reauthorization_required" if res.status_code == 401 else "spotify_playback_http_error"
            return j({"error": err, "status": res.status_code}, 502)

        track = None
        if item:
            album = item.get("album") if isinstance(item.get("album"), dict) else {}
            artists = item.get("artists") if isinstance(item.get("artists"), list) else []
            images = album.get("images") if isinstance(album.get("images"), list) else []
            track = {
                "name": text(item.get("name"), 200),
                "artist": text(", ".join(a.get("name") for a in artists if isinstance(a, dict) and a.get("name")), 200),
                "album": text(album.get("name"), 200),
                "image": clean_url(images[0].get("url")) if images and isinstance(images[0], dict) else None,
                "nowPlaying": now_playing,
                "currentPlayback": current_playback,
                "progressMs": progress_ms,
                "durationMs": item.get("duration_ms"),
            }
        payload = {"track": track}
        cached_api_set("spotify", 3.0, 200, payload)
        return j(payload)
    except Exception:
        return j({"error": "spotify_failed"}, 502)


# ----------------------------- Twitch & Discord -----------------------------


def twitch_app_token(force_refresh: bool = False) -> str:
    global _twitch_token
    if not force_refresh and _twitch_token.get("token") and _twitch_token.get("expires_at", 0) > time.time() + 60:
        return _twitch_token["token"]
    res = requests.post(
        TWITCH_TOKEN_URL,
        data={
            "client_id": TWITCH_CLIENT_ID,
            "client_secret": TWITCH_CLIENT_SECRET,
            "grant_type": "client_credentials",
        },
        timeout=15,
    )
    data = res.json() if res.content else {}
    if not res.ok or not data.get("access_token"):
        raise RuntimeError("twitch_token_failed")
    _twitch_token = {"token": data["access_token"], "expires_at": time.time() + int(data.get("expires_in") or 3600)}
    return data["access_token"]


def helix(path: str, **params: Any) -> list[dict[str, Any]]:
    token = twitch_app_token()
    res = requests.get(
        f"https://api.twitch.tv/helix{path}",
        params=params,
        headers={"Client-ID": TWITCH_CLIENT_ID, "Authorization": f"Bearer {token}"},
        timeout=15,
    )
    if res.status_code == 401:
        token = twitch_app_token(force_refresh=True)
        res = requests.get(
            f"https://api.twitch.tv/helix{path}",
            params=params,
            headers={"Client-ID": TWITCH_CLIENT_ID, "Authorization": f"Bearer {token}"},
            timeout=15,
        )
    if not res.ok:
        raise RuntimeError(f"twitch_http_{res.status_code}")
    data = res.json() if res.content else {}
    return data.get("data") if isinstance(data.get("data"), list) else []


def twitch_status(login: str) -> tuple[int, dict[str, Any]]:
    if not TWITCH_CLIENT_ID or not TWITCH_CLIENT_SECRET:
        return 503, {"error": "twitch_key_missing"}
    cache_key = f"twitch:{login.lower()}"
    hit = cached_api_get(cache_key)
    if hit:
        return hit
    try:
        users = helix("/users", login=login)
        streams = helix("/streams", user_login=login)
        user = users[0] if users else {}
        stream = streams[0] if streams else None
        payload = {
            "live": bool(stream),
            "login": user.get("login", login),
            "displayName": user.get("display_name", login),
            "description": user.get("description", ""),
            "profileImage": clean_url(user.get("profile_image_url")) or "",
            "viewCount": user.get("view_count", 0),
            "url": f"https://www.twitch.tv/{login}",
            "stream": (
                {
                    "title": stream.get("title"),
                    "gameName": stream.get("game_name"),
                    "viewerCount": stream.get("viewer_count", 0),
                    "startedAt": stream.get("started_at"),
                }
                if stream
                else None
            ),
        }
        cached_api_set(cache_key, 60.0, 200, payload)
        return 200, payload
    except Exception:
        return 502, {"error": "twitch_failed"}


@app.get("/api/twitch")
def twitch(request: Request, login: str = TWITCH_CHANNEL) -> JSONResponse:
    rate_limit(request, "twitch", 60, 60)
    clean_login = re.sub(r"[^A-Za-z0-9_]", "", text(login, 40)) or TWITCH_CHANNEL
    status_code, payload = twitch_status(clean_login)
    return j(payload, status_code)


@app.get("/api/discord")
def discord_invite(request: Request, code: str = DISCORD_INVITE_CODE) -> JSONResponse:
    rate_limit(request, "discord", 60, 60)
    clean_code = re.sub(r"[^A-Za-z0-9_-]", "", text(code, 40)) or DISCORD_INVITE_CODE
    if not clean_code:
        return j({"error": "discord_code_missing"}, 503)
    cache_key = f"discord:{clean_code}"
    hit = cached_api_get(cache_key)
    if hit:
        return j(hit[1], hit[0])
    try:
        res = requests.get(
            f"{DISCORD_INVITE_BASE}/{clean_code}",
            params={"with_counts": "true"},
            headers={"Accept": "application/json", "User-Agent": "krafen-site/2.0"},
            timeout=12,
        )
        data = res.json() if res.content else {}
        if not res.ok:
            return j({"error": "discord_unavailable"}, 502)
        guild = data.get("guild") if isinstance(data.get("guild"), dict) else {}
        icon_hash = text(guild.get("icon"), 80)
        guild_id = text(guild.get("id"), 40)
        valid_icon = re.fullmatch(r"[A-Za-z0-9_]+", icon_hash) and re.fullmatch(r"\d+", guild_id)
        raw_icon_url = f"https://cdn.discordapp.com/icons/{guild_id}/{icon_hash}.png?size=128" if valid_icon else None
        icon_url = f"/api/img?u={quote(raw_icon_url, safe='')}" if raw_icon_url else None
        payload = {
            "code": clean_code,
            "name": text(guild.get("name"), 120) or "Discord Server",
            "memberCount": int(data.get("approximate_member_count") or 0),
            "onlineCount": int(data.get("approximate_presence_count") or 0),
            "icon": icon_url,
            "url": f"https://discord.gg/{clean_code}",
        }
        cached_api_set(cache_key, 180.0, 200, payload)
        return j(payload)
    except Exception:
        return j({"error": "discord_unavailable"}, 502)


# ----------------------------- Прокси обложек для РФ (/api/img) -----------------------------

_PROXY_IMG_HOSTS = frozenset({
    "cdn.discordapp.com",
    "media.discordapp.net",
    "shikimori.one",
    "nyaa.shikimori.one",
    "desu.shikimori.one",
    "dere.shikimori.one",
    "kawai.shikimori.one",
    "image.tmdb.org",
    "images.igdb.com",
    "i.scdn.co",
    "lastfm.freetls.fastly.net",
    "lastfm-img2.akamaized.net",
    "static-cdn.jtvnw.net",
    "drive.google.com",
    "lh3.googleusercontent.com",
    "images.weserv.nl",
    "m.media-amazon.com",
})
_SAFE_IMG_MIMES = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/avif": ".avif",
}
_MAX_PROXY_IMG_BYTES = 4 * 1024 * 1024
_IMG_CACHE_PRUNE_LOCK = threading.Lock()


def _is_allowed_proxy_img_url(raw_url: str) -> bool:
    cleaned = clean_url(raw_url)
    if not cleaned:
        return False
    parsed = urlsplit(cleaned)
    host = (parsed.hostname or "").lower().rstrip(".")
    return parsed.scheme == "https" and host in _PROXY_IMG_HOSTS


def _prune_img_cache() -> None:
    if not _IMG_CACHE_PRUNE_LOCK.acquire(blocking=False):
        return
    try:
        files = [p for p in IMG_CACHE_DIR.glob("img_*.*") if p.is_file()]
        if len(files) <= 600:
            return
        files.sort(key=lambda p: p.stat().st_mtime)
        for old in files[:150]:
            with contextlib.suppress(OSError):
                old.unlink()
    except OSError:
        pass
    finally:
        _IMG_CACHE_PRUNE_LOCK.release()


@app.api_route("/api/img", methods=["GET", "HEAD"])
def proxy_external_image(request: Request, u: str = "") -> Response:
    rate_limit(request, "img_proxy", 240, 60)
    target = clean_url(u)
    if not target or not _is_allowed_proxy_img_url(target):
        return j({"error": "invalid_image_url"}, 400)

    digest = hashlib.sha256(target.encode("utf-8")).hexdigest()[:32]
    etag = f'"{digest}"'
    if (request.headers.get("if-none-match") or "").strip() == etag:
        return Response(
            status_code=304,
            headers={"ETag": etag, "Cache-Control": "public, max-age=604800, immutable"},
        )

    IMG_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    for ext, mime in (
        (".jpg", "image/jpeg"),
        (".webp", "image/webp"),
        (".png", "image/png"),
        (".gif", "image/gif"),
        (".avif", "image/avif"),
    ):
        cached_path = IMG_CACHE_DIR / f"img_{digest}{ext}"
        if cached_path.is_file():
            headers = {
                "Cache-Control": "public, max-age=604800, immutable",
                "ETag": etag,
                "X-Content-Type-Options": "nosniff",
            }
            if request.method == "HEAD":
                return Response(status_code=200, media_type=mime, headers=headers)
            return FileResponse(cached_path, media_type=mime, headers=headers)

    cur_url = target
    try:
        for _hop in range(4):
            if not _is_allowed_proxy_img_url(cur_url):
                return j({"error": "disallowed_redirect"}, 400)
            with requests.get(
                cur_url,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; KrafenImgProxy/1.0)",
                    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                },
                stream=True,
                timeout=(3.5, 10),
                allow_redirects=False,
            ) as resp:
                if resp.status_code in (301, 302, 303, 307, 308):
                    loc = (resp.headers.get("location") or "").strip()
                    if not loc:
                        return j({"error": "upstream_bad_redirect"}, 502)
                    if loc.startswith("/"):
                        p = urlsplit(cur_url)
                        loc = f"{p.scheme}://{p.netloc}{loc}"
                    cur_url = loc
                    continue
                if resp.status_code != 200:
                    return j({"error": "upstream_not_found"}, 404 if resp.status_code == 404 else 502)
                raw_ct = (resp.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
                ext = _SAFE_IMG_MIMES.get(raw_ct)
                if not ext:
                    return j({"error": "unsupported_media_type"}, 502)
                buf = io.BytesIO()
                total = 0
                for chunk in resp.iter_content(chunk_size=32 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > _MAX_PROXY_IMG_BYTES:
                        return j({"error": "image_too_large"}, 502)
                    buf.write(chunk)
                img_bytes = buf.getvalue()
                if not img_bytes:
                    return j({"error": "empty_image"}, 502)

                out_mime = "image/jpeg" if raw_ct == "image/jpg" else raw_ct
                final_path = IMG_CACHE_DIR / f"img_{digest}{ext}"
                tmp_path = IMG_CACHE_DIR / f".tmp_{digest}_{secrets.token_hex(4)}{ext}"
                with contextlib.suppress(OSError):
                    tmp_path.write_bytes(img_bytes)
                    os.replace(tmp_path, final_path)
                    _prune_img_cache()
                headers = {
                    "Cache-Control": "public, max-age=604800, immutable",
                    "ETag": etag,
                    "X-Content-Type-Options": "nosniff",
                    "Content-Length": str(len(img_bytes)),
                }
                if request.method == "HEAD":
                    return Response(status_code=200, media_type=out_mime, headers=headers)
                return Response(content=img_bytes, status_code=200, media_type=out_mime, headers=headers)
        return j({"error": "too_many_redirects"}, 502)
    except (requests.RequestException, OSError):
        return j({"error": "upstream_unreachable"}, 502)


# ----------------------------- VOD архив -----------------------------


def safe_name(value: str) -> bool:
    if not value or len(value) > 200 or "\x00" in value:
        return False
    if (
        value in {".", ".."}
        or ".." in value
        or "/" in value
        or "\\" in value
        or value.startswith((".", " "))
        or value.endswith((".", " "))
    ):
        return False
    return re.fullmatch(r"[\w. -]+", value, flags=re.UNICODE) is not None


def _vod_cache_stem(vod_id: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", vod_id) and ".." not in vod_id:
        return vod_id
    prefix = re.sub(r"[^A-Za-z0-9_-]+", "_", vod_id)[:48].strip("_") or "vod"
    digest = hashlib.sha1(vod_id.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{digest}"


def _format_size_bytes(num_bytes: int | float) -> str:
    n = float(num_bytes or 0)
    if n <= 0:
        return ""
    gb = n / (1024 ** 3)
    if gb >= 1.0:
        return f"{gb:.2f} GB"
    mb = n / (1024 ** 2)
    if mb >= 1.0:
        return f"{mb:.1f} MB"
    kb = max(1, round(n / 1024))
    return f"{kb} KB"


_drive_sync_lock = threading.Lock()
_drive_sync_worker_lock = threading.Lock()
_drive_sync_ts = 0.0
_FLIP_TITLE_RE = re.compile(r'<div[^>]*class="flip-entry-title"[^>]*>([\s\S]*?)</div>', re.IGNORECASE)
_FLIP_FOLDER_HREF_RE = re.compile(
    r'href="https://drive\.google\.com/(?:drive/(?:u/\d+/)?folders/|folderview\?id=)([A-Za-z0-9_-]{10,})',
    re.IGNORECASE,
)
_FLIP_FILE_HREF_RE = re.compile(
    r'href="https://(?:drive|docs)\.google\.com/(?:file/d/|open\?id=)([A-Za-z0-9_-]{10,})',
    re.IGNORECASE,
)


def parse_drive_filename(raw_name: str, fallback_key: str) -> dict[str, str]:
    clean = re.sub(r"\.(mp4|mkv|webm|mov)$", "", html.unescape(raw_name).strip(), flags=re.IGNORECASE).strip()
    date_match = re.search(r"\[(\d{4}-\d{2}-\d{2}_\d{2}-\d{2})\]", clean)
    note = ""
    title = clean
    if date_match:
        vod_id = date_match.group(1)
        prefix = clean[: date_match.start()].strip(" -—·")
        suffix = clean[date_match.end() :].strip()
        suffix = re.sub(r"^danonsha\s*-\s*", "", suffix, flags=re.IGNORECASE).strip()
        title = suffix or clean
        note = prefix
    else:
        stamp_match = re.match(r"^(\d{4}-\d{2}-\d{2}_\d{2}-\d{2})(?:[_-]+(.*))?$", clean)
        if stamp_match:
            sanitized = re.sub(r"\.{2,}", ".", re.sub(r"[^\w.-]+", "_", clean, flags=re.UNICODE)).strip("._ ")
            vod_id = sanitized or stamp_match.group(1)
            slug_tail = (stamp_match.group(2) or "").replace("-", " ").replace("_", " ").strip()
            title = slug_tail or "Запись стрима"
        else:
            vod_id = f"gdrive_{fallback_key}"
    return {
        "id": vod_id[:160],
        "title": title[:260] or "Запись стрима",
        "note": note[:180],
        "sort_key": vod_id[:160],
    }


def parse_drive_folder_name(raw_folder_name: str, fallback_key: str) -> dict[str, str]:
    clean = html.unescape(raw_folder_name or "").strip()
    sanitized_id = re.sub(r"\.{2,}", ".", re.sub(r"[^\w.-]+", "_", clean, flags=re.UNICODE)).strip("._ ")[:160]
    stamp_m = re.search(r"(\d{4}-\d{2}-\d{2}_\d{2}-\d{2})", clean)
    if stamp_m:
        stamp = stamp_m.group(1)
        vod_id = sanitized_id if sanitized_id else stamp
        tail = clean[stamp_m.end() :].lstrip("_- —·").strip()
        fallback_title = tail.replace("-", " ").replace("_", " ").strip() or "Запись стрима"
        sort_key = vod_id if vod_id.startswith(stamp) else f"{stamp}_{vod_id}"
    else:
        vod_id = sanitized_id or f"gdrive_{fallback_key}"
        fallback_title = clean or "Запись стрима"
        sort_key = vod_id
    return {
        "id": vod_id[:160],
        "fallback_title": fallback_title[:260],
        "sort_key": sort_key[:160],
    }


def _parse_embedded_folder_html(html_text: str) -> list[dict[str, Any]]:
    """Парсит файлы и подпапки из https://drive.google.com/embeddedfolderview?id=...#list."""
    entries: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    chunks = re.split(r"""<div[^>]+class=["'](?:[^"']*\s)?flip-entry(?:\s[^"']*)?["']""", html_text, flags=re.IGNORECASE)
    for chunk in chunks[1:]:
        title_m = _FLIP_TITLE_RE.search(chunk)
        if not title_m:
            continue
        plain_name = html.unescape(re.sub(r"<[^>]+>", "", title_m.group(1))).strip()
        if not plain_name:
            continue
        folder_m = _FLIP_FOLDER_HREF_RE.search(chunk)
        if folder_m:
            did = folder_m.group(1)
            if did not in seen_ids:
                seen_ids.add(did)
                entries.append({"id": did, "name": plain_name, "kind": "folder", "size": 0})
            continue
        file_m = _FLIP_FILE_HREF_RE.search(chunk)
        if file_m:
            did = file_m.group(1)
            if did not in seen_ids:
                seen_ids.add(did)
                entries.append({"id": did, "name": plain_name, "kind": "file", "size": 0})
    return entries


def _list_gdrive_folder(folder_id: str) -> tuple[bool, list[dict[str, Any]]]:
    """Возвращает (ok, entries), где каждый элемент: {id, name, kind: 'file'|'folder', size: int}."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{10,80}", folder_id):
        return False, []

    if GDRIVE_API_KEY:
        try:
            items: list[dict[str, Any]] = []
            page_token = ""
            for _ in range(10):
                params: dict[str, Any] = {
                    "q": f"'{folder_id}' in parents and trashed = false",
                    "fields": "nextPageToken,files(id,name,mimeType,size)",
                    "pageSize": 200,
                    "supportsAllDrives": "true",
                    "includeItemsFromAllDrives": "true",
                    "key": GDRIVE_API_KEY,
                }
                if page_token:
                    params["pageToken"] = page_token
                resp = requests.get(
                    "https://www.googleapis.com/drive/v3/files",
                    params=params,
                    headers={"User-Agent": "KrafenSite/1.0"},
                    timeout=10,
                )
                if not resp.ok:
                    break
                payload = resp.json()
                for f in payload.get("files") or []:
                    fid = str(f.get("id") or "").strip()
                    fname = str(f.get("name") or "").strip()
                    if not fid or not fname:
                        continue
                    mime = str(f.get("mimeType") or "")
                    kind = "folder" if mime == "application/vnd.google-apps.folder" else "file"
                    try:
                        fsize = int(f.get("size") or 0)
                    except (TypeError, ValueError):
                        fsize = 0
                    items.append({"id": fid, "name": fname, "kind": kind, "size": fsize})
                page_token = str(payload.get("nextPageToken") or "").strip()
                if not page_token:
                    return True, items
            if items:
                return True, items
        except Exception:
            pass

    try:
        res = requests.get(
            f"https://drive.google.com/embeddedfolderview?id={folder_id}#list",
            headers={"User-Agent": "Mozilla/5.0 (compatible; KrafenSite/1.0)"},
            timeout=10,
        )
        if not res.ok:
            return False, []
        body = res.text
        if "flip-entries" not in body and "flip-entry" not in body:
            return False, []
        return True, _parse_embedded_folder_html(body)
    except Exception:
        return False, []


def _read_response_prefix(resp: requests.Response, max_bytes: int = 131072) -> str:
    """Читает префикс тела ответа через iter_content (без необработанных urllib3-исключений)."""
    buf = bytearray()
    try:
        for chunk in resp.iter_content(chunk_size=16384):
            if not chunk:
                continue
            buf.extend(chunk)
            if len(buf) >= max_bytes:
                break
    except (requests.RequestException, OSError):
        pass
    return bytes(buf[:max_bytes]).decode("utf-8", errors="ignore")


_SAFE_GDRIVE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,80}$")
_SAFE_GDRIVE_UUID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_SAFE_STREAM_MIMES = {
    "video/mp4",
    "video/webm",
    "video/x-matroska",
    "video/quicktime",
    "video/mp2t",
    "application/octet-stream",
}


def _extract_safe_gdrive_uuid(sample: str) -> str:
    uuid_m = re.search(r'name="uuid"\s+value="([^"]+)"', sample)
    if not uuid_m:
        return ""
    cand = uuid_m.group(1).strip()
    return cand if _SAFE_GDRIVE_UUID_RE.fullmatch(cand) else ""


def _probe_gdrive_file_size(drive_id: str) -> int:
    """Определяет точный размер файла на Google Drive по 1-байтовому Range-запросу."""
    if not _SAFE_GDRIVE_ID_RE.fullmatch(drive_id):
        return 0
    base_url = "https://drive.usercontent.google.com/download"
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36",
        "Range": "bytes=0-0",
    }
    params: dict[str, str] = {"id": drive_id, "export": "download", "confirm": "t"}
    session = requests.Session()
    try:
        resp = session.get(base_url, params=params, headers=headers, stream=True, timeout=(3, 8))
        ctype = (resp.headers.get("content-type") or "").lower()
        if "text/html" in ctype:
            sample = _read_response_prefix(resp)
            resp.close()
            found_uuid = _extract_safe_gdrive_uuid(sample)
            if not found_uuid:
                return 0
            params["uuid"] = found_uuid
            resp = session.get(base_url, params=params, headers=headers, stream=True, timeout=(3, 8))
        cr = resp.headers.get("Content-Range") or ""
        cl = resp.headers.get("Content-Length") or ""
        resp.close()
        m = re.search(r"/(\d+)\s*$", cr)
        if m:
            return int(m.group(1))
        if resp.status_code == 200 and cl.isdigit():
            return int(cl)
    except Exception:
        return 0
    finally:
        session.close()
    return 0


def _download_gdrive_small_bytes(drive_id: str, max_bytes: int = 8 * 1024 * 1024) -> bytes | None:
    """Скачивает небольшой публичный файл с Google Drive (meta.json, chat.jsonl, thumb.jpg)."""
    if not _SAFE_GDRIVE_ID_RE.fullmatch(drive_id):
        return None
    base_url = "https://drive.usercontent.google.com/download"
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    }
    params: dict[str, str] = {"id": drive_id, "export": "download", "confirm": "t"}
    session = requests.Session()
    try:
        resp = session.get(base_url, params=params, headers=headers, stream=True, timeout=(4, 15))
        ctype = (resp.headers.get("content-type") or "").lower()
        if "text/html" in ctype:
            sample = _read_response_prefix(resp)
            resp.close()
            found_uuid = _extract_safe_gdrive_uuid(sample)
            if not found_uuid:
                return None
            params["uuid"] = found_uuid
            resp = session.get(base_url, params=params, headers=headers, stream=True, timeout=(4, 15))
            ctype = (resp.headers.get("content-type") or "").lower()

        if resp.status_code >= 400 or "text/html" in ctype:
            resp.close()
            return None

        buf = io.BytesIO()
        total = 0
        for chunk in resp.iter_content(chunk_size=32 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                resp.close()
                return None
            buf.write(chunk)
        resp.close()
        return buf.getvalue()
    except (requests.RequestException, OSError):
        return None
    finally:
        session.close()


def _upsert_drive_vod(conn: sqlite3.Connection, row_data: dict[str, Any]) -> None:
    # Удаляем конфликтующие дубли с тем же drive_id под другим id, если такие были
    conn.execute(
        "DELETE FROM drive_vods WHERE drive_id = ? AND id != ?",
        (row_data["drive_id"], row_data["id"]),
    )
    conn.execute(
        """
        INSERT INTO drive_vods (
            id, drive_id, title, size_label, note, sort_key,
            folder_drive_id, chat_drive_id, thumb_drive_id, meta_drive_id,
            game, duration_s, started_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            drive_id = excluded.drive_id,
            title = excluded.title,
            size_label = CASE WHEN excluded.size_label != '' THEN excluded.size_label ELSE drive_vods.size_label END,
            note = CASE WHEN drive_vods.note != '' AND excluded.note = '' THEN drive_vods.note ELSE excluded.note END,
            sort_key = excluded.sort_key,
            folder_drive_id = excluded.folder_drive_id,
            chat_drive_id = excluded.chat_drive_id,
            thumb_drive_id = excluded.thumb_drive_id,
            meta_drive_id = excluded.meta_drive_id,
            game = excluded.game,
            duration_s = CASE WHEN excluded.duration_s > 0 THEN excluded.duration_s ELSE drive_vods.duration_s END,
            started_at = CASE WHEN excluded.started_at != '' THEN excluded.started_at ELSE drive_vods.started_at END
        """,
        (
            row_data["id"],
            row_data["drive_id"],
            row_data["title"],
            row_data.get("size_label", ""),
            row_data.get("note", ""),
            row_data["sort_key"],
            row_data.get("folder_drive_id", ""),
            row_data.get("chat_drive_id", ""),
            row_data.get("thumb_drive_id", ""),
            row_data.get("meta_drive_id", ""),
            row_data.get("game", ""),
            int(row_data.get("duration_s") or 0),
            row_data.get("started_at", ""),
        ),
    )


def _inspect_drive_vod_subfolder(
    folder_drive_id: str,
    folder_name: str,
    existing: dict[str, Any] | None,
    force: bool = False,
) -> dict[str, Any] | None:
    """
    Читает структуру подпапки записи на Google Drive формата:
      <vod_folder>/
        ├── video.mp4
        ├── chat.jsonl
        ├── meta.json
        └── thumb.jpg
    """
    parsed_folder = parse_drive_folder_name(folder_name, folder_drive_id[:12])
    if (
        not force
        and existing
        and existing.get("drive_id")
        and existing.get("size_label")
        and (existing.get("meta_drive_id") or existing.get("thumb_drive_id"))
    ):
        return None

    ok, sub_entries = _list_gdrive_folder(folder_drive_id)
    if not ok or not sub_entries:
        return None

    files_by_lower: dict[str, dict[str, Any]] = {}
    all_files: list[dict[str, Any]] = []
    for item in sub_entries:
        if item["kind"] != "file":
            continue
        all_files.append(item)
        files_by_lower[item["name"].lower()] = item

    meta_entry = files_by_lower.get("meta.json")
    meta_obj: dict[str, Any] = {}
    if meta_entry:
        raw_meta = _download_gdrive_small_bytes(meta_entry["id"], max_bytes=256 * 1024)
        if raw_meta:
            try:
                loaded = json.loads(raw_meta.decode("utf-8", errors="replace"))
                if isinstance(loaded, dict):
                    meta_obj = loaded
            except Exception:
                meta_obj = {}

    preferred_video = str(meta_obj.get("video_file") or "video.mp4").strip().lower()
    preferred_chat = str(meta_obj.get("chat_file") or "chat.jsonl").strip().lower()
    preferred_thumb = str(meta_obj.get("thumb_file") or "thumb.jpg").strip().lower()

    video_entry = files_by_lower.get(preferred_video) or files_by_lower.get("video.mp4")
    if not video_entry:
        for f in all_files:
            low = f["name"].lower()
            if low.endswith((".mp4", ".mkv", ".webm", ".mov")) and not re.search(r"\.part\d+\.mp4$", low):
                video_entry = f
                break
    if not video_entry:
        return None

    chat_entry = files_by_lower.get(preferred_chat) or files_by_lower.get("chat.jsonl")
    if not chat_entry:
        for f in all_files:
            if f["name"].lower().endswith(".jsonl"):
                chat_entry = f
                break

    thumb_entry = files_by_lower.get(preferred_thumb) or files_by_lower.get("thumb.jpg")
    if not thumb_entry:
        for f in all_files:
            low = f["name"].lower()
            if low.startswith("thumb.") and low.endswith((".jpg", ".jpeg", ".png", ".webp")):
                thumb_entry = f
                break
        if not thumb_entry:
            for f in all_files:
                if f["name"].lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
                    thumb_entry = f
                    break

    raw_title = str(meta_obj.get("title") or "").strip()
    title = (raw_title or parsed_folder["fallback_title"] or "Запись стрима")[:260]
    game = str(meta_obj.get("game") or "").strip()[:120]
    note = str(meta_obj.get("note") or "").strip()[:180]
    started_at = str(meta_obj.get("started_at") or meta_obj.get("record_started_at") or "").strip()[:64]
    try:
        duration_s = max(0, int(float(meta_obj.get("duration_s") or 0)))
    except (TypeError, ValueError):
        duration_s = 0

    size_label = str(meta_obj.get("size_label") or "").strip()[:40]
    if not size_label:
        size_bytes = video_entry.get("size") or meta_obj.get("size_bytes") or 0
        try:
            size_bytes = int(size_bytes)
        except (TypeError, ValueError):
            size_bytes = 0
        if size_bytes <= 0:
            size_bytes = _probe_gdrive_file_size(video_entry["id"])
        size_label = _format_size_bytes(size_bytes)

    return {
        "id": parsed_folder["id"],
        "drive_id": video_entry["id"],
        "title": title,
        "size_label": size_label,
        "note": note,
        "sort_key": parsed_folder["sort_key"],
        "folder_drive_id": folder_drive_id,
        "chat_drive_id": chat_entry["id"] if chat_entry else "",
        "thumb_drive_id": thumb_entry["id"] if thumb_entry else "",
        "meta_drive_id": meta_entry["id"] if meta_entry else "",
        "game": game,
        "duration_s": duration_s,
        "started_at": started_at,
    }


def _sync_drive_vods_worker(folder_id: str, force: bool = False) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_-]{10,80}", folder_id):
        return
    if not _drive_sync_worker_lock.acquire(blocking=False):
        return
    try:
        ok, top_entries = _list_gdrive_folder(folder_id)
        if not ok:
            return

        with _db_lock, db_connect() as conn:
            existing_rows = {
                str(r["folder_drive_id"]): dict(r)
                for r in conn.execute("SELECT * FROM drive_vods WHERE folder_drive_id != ''").fetchall()
            }

        prepared_rows: list[dict[str, Any]] = []
        seen_folder_ids: set[str] = set()
        seen_flat_drive_ids: set[str] = set()

        for entry in top_entries:
            if entry["kind"] == "folder":
                f_id = entry["id"]
                seen_folder_ids.add(f_id)
                sub_row = _inspect_drive_vod_subfolder(
                    f_id,
                    entry["name"],
                    existing_rows.get(f_id),
                    force=force,
                )
                if sub_row:
                    prepared_rows.append(sub_row)
            elif entry["kind"] == "file":
                plain_title = entry["name"]
                if not plain_title.lower().endswith((".mp4", ".mkv", ".webm", ".mov")):
                    continue
                seen_flat_drive_ids.add(entry["id"])
                parsed = parse_drive_filename(plain_title, entry["id"][:12])
                prepared_rows.append(
                    {
                        "id": parsed["id"],
                        "drive_id": entry["id"],
                        "title": parsed["title"],
                        "size_label": _format_size_bytes(entry.get("size") or 0),
                        "note": parsed["note"],
                        "sort_key": parsed["sort_key"],
                        "folder_drive_id": "",
                        "chat_drive_id": "",
                        "thumb_drive_id": "",
                        "meta_drive_id": "",
                        "game": "",
                        "duration_s": 0,
                        "started_at": "",
                    }
                )

        if not top_entries and not GDRIVE_API_KEY:
            return

        with _db_lock, db_connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for row_data in prepared_rows:
                    _upsert_drive_vod(conn, row_data)
                if top_entries:
                    # Удаляем записи подпапок, которые были удалены из Google Drive
                    for old_folder_id, old_row in existing_rows.items():
                        if old_folder_id not in seen_folder_ids:
                            conn.execute("DELETE FROM drive_vods WHERE id = ?", (old_row["id"],))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
    except Exception:
        pass
    finally:
        _drive_sync_worker_lock.release()


_drive_poller_started = False


def _start_drive_vods_poller() -> None:
    global _drive_poller_started
    if _drive_poller_started or not GDRIVE_FOLDER_ID:
        return
    _drive_poller_started = True

    def _poller() -> None:
        maybe_sync_drive_vods(force=False)
        while True:
            time.sleep(90)
            maybe_sync_drive_vods(force=False)

    threading.Thread(target=_poller, name="gdrive-vods-poller", daemon=True).start()


def maybe_sync_drive_vods(force: bool = False) -> None:
    global _drive_sync_ts
    if not GDRIVE_FOLDER_ID:
        return
    now = time.time()
    min_interval = 12.0 if force else 60.0
    with _drive_sync_lock:
        if now - _drive_sync_ts < min_interval:
            return
        _drive_sync_ts = now
    if force:
        _sync_drive_vods_worker(GDRIVE_FOLDER_ID, force=True)
    else:
        threading.Thread(target=_sync_drive_vods_worker, args=(GDRIVE_FOLDER_ID, False), daemon=True).start()


def list_vods(force_sync: bool = False, *, include_hidden: bool = False) -> list[dict[str, Any]]:
    if force_sync:
        maybe_sync_drive_vods(force=True)

    vods_by_id: dict[str, dict[str, Any]] = {}

    if RECORD_DIR.exists():
        for folder in sorted(RECORD_DIR.iterdir(), reverse=True):
            if not folder.is_dir() or not safe_name(folder.name):
                continue
            meta_file = folder / "meta.json"
            if not meta_file.is_file():
                continue
            try:
                raw_meta = json.loads(meta_file.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(raw_meta, dict):
                continue
            raw_vfile = text(raw_meta.get("video_file"), 120) or "video.mp4"
            video_file = raw_vfile if safe_name(raw_vfile) else "video.mp4"
            raw_tfile = text(raw_meta.get("thumb_file"), 120) or "thumb.jpg"
            thumb_file = raw_tfile if safe_name(raw_tfile) else "thumb.jpg"
            q_id = quote(folder.name, safe="")
            vods_by_id[folder.name] = {
                "id": folder.name,
                "source": "local",
                "title": text(raw_meta.get("title"), 260) or folder.name,
                "game": text(raw_meta.get("game"), 120),
                "duration_s": max(0, _safe_int(raw_meta.get("duration_s"), 0)),
                "started_at": text(raw_meta.get("started_at") or raw_meta.get("record_started_at"), 64),
                "size_label": text(raw_meta.get("size_label"), 40),
                "note": text(raw_meta.get("note"), 180),
                "video_file": video_file,
                "has_chat": (folder / "chat.jsonl").is_file(),
                "has_video": (folder / video_file).is_file() or (folder / "video.mp4").is_file(),
                "has_thumb": (folder / thumb_file).is_file() or (folder / "thumb.jpg").is_file(),
                "thumb_url": f"/api/vods/{q_id}/thumb",
                "thumb_fallbacks": [],
                "sort_key": folder.name,
            }

    with _db_lock, db_connect() as conn:
        rows = conn.execute("SELECT * FROM drive_vods ORDER BY sort_key DESC").fetchall()
        for r in rows:
            vid = r["id"]
            if vid in vods_by_id:
                continue
            q_vid = quote(vid, safe="")
            drive_id = r["drive_id"]
            thumb_drive_id = r["thumb_drive_id"] or ""
            chat_drive_id = r["chat_drive_id"] or ""
            folder_drive_id = r["folder_drive_id"] or ""

            raw_fallbacks: list[str] = []
            if thumb_drive_id:
                raw_fallbacks.append(f"https://drive.google.com/thumbnail?id={thumb_drive_id}&sz=w960")
                raw_fallbacks.append(f"https://lh3.googleusercontent.com/d/{thumb_drive_id}=w960")
            raw_fallbacks.append(f"https://drive.google.com/thumbnail?id={drive_id}&sz=w960")
            raw_fallbacks.append(f"https://lh3.googleusercontent.com/d/{drive_id}=w960")
            fallbacks = [f"/api/img?u={quote(u, safe='')}" for u in raw_fallbacks]

            vods_by_id[vid] = {
                "id": vid,
                "source": "gdrive",
                "drive_id": drive_id,
                "folder_drive_id": folder_drive_id,
                "title": r["title"],
                "game": r["game"] or "",
                "duration_s": int(r["duration_s"] or 0),
                "started_at": r["started_at"] or "",
                "size_label": r["size_label"] or "",
                "note": r["note"] or "",
                "stream_url": f"/api/vods/{q_vid}/stream",
                "download_url": f"/api/vods/{q_vid}/stream?download=1",
                "embed_url": f"https://drive.google.com/file/d/{drive_id}/preview",
                "view_url": (
                    f"https://drive.google.com/drive/folders/{folder_drive_id}"
                    if folder_drive_id
                    else f"https://drive.google.com/file/d/{drive_id}/view"
                ),
                "thumb_url": f"/api/vods/{q_vid}/thumb",
                "thumb_fallbacks": fallbacks,
                "has_chat": bool(chat_drive_id),
                "has_video": True,
                "sort_key": r["sort_key"],
            }

    overrides = read_vod_overrides()
    final_vods: list[dict[str, Any]] = []
    for vid, entry in vods_by_id.items():
        ov = overrides.get(vid) if isinstance(overrides.get(vid), dict) else None
        is_hidden = bool(ov and ov.get("hidden"))
        if is_hidden and not include_hidden:
            continue
        orig_title = str(entry.get("title") or "")
        orig_note = str(entry.get("note") or "")
        custom_title = text(ov.get("custom_title"), 260) if ov else ""
        custom_note = text(ov.get("custom_note"), 180) if ov else ""
        if custom_title:
            entry["title"] = custom_title
        if custom_note:
            entry["note"] = custom_note
        if include_hidden:
            entry["orig_title"] = orig_title
            entry["orig_note"] = orig_note
            entry["custom_title"] = custom_title
            entry["custom_note"] = custom_note
            entry["hidden"] = is_hidden
        final_vods.append(entry)

    if not force_sync:
        maybe_sync_drive_vods(force=False)
    return sorted(final_vods, key=lambda x: str(x.get("sort_key") or x.get("id") or ""), reverse=True)


def find_vod(vod_id: str) -> Path | None:
    if not safe_name(vod_id):
        return None
    folder = RECORD_DIR / vod_id
    return folder if folder.is_dir() else None


def _get_drive_vod_row(vod_id: str) -> dict[str, Any] | None:
    if not safe_name(vod_id):
        return None
    with _db_lock, db_connect() as conn:
        row = conn.execute("SELECT * FROM drive_vods WHERE id = ?", (vod_id,)).fetchone()
        return dict(row) if row else None


@app.get("/api/vods")
def api_vods(request: Request, refresh: str = "") -> JSONResponse:
    force = refresh in {"1", "true", "yes"}
    if force:
        rate_limit(request, "vods_refresh", 6, 60)
    folder_url = f"https://drive.google.com/drive/folders/{GDRIVE_FOLDER_ID}" if GDRIVE_FOLDER_ID else ""
    return j(
        {"vods": list_vods(force_sync=force), "folder_url": folder_url},
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/vods/{vod_id}/chat")
def vod_chat(vod_id: str, request: Request):
    rate_limit(request, "vod_chat", 60, 60)
    if not safe_name(vod_id):
        return j({"error": "not found"}, 404)
    chat_headers = {"X-Content-Type-Options": "nosniff"}
    folder = find_vod(vod_id)
    chat = folder / "chat.jsonl" if folder else None
    if chat and chat.is_file():
        return FileResponse(chat, media_type=MIME[".jsonl"], headers=chat_headers)

    row = _get_drive_vod_row(vod_id)
    chat_drive_id = str(row.get("chat_drive_id") or "").strip() if row else ""
    if not chat_drive_id:
        return j({"error": "no chat"}, 404)

    cached_chat = CHAT_CACHE_DIR / f"{_vod_cache_stem(vod_id)}.jsonl"
    pub_chat_headers = {"Cache-Control": "public, max-age=86400", "X-Content-Type-Options": "nosniff"}
    if cached_chat.is_file() and cached_chat.stat().st_size > 0:
        return FileResponse(cached_chat, media_type=MIME[".jsonl"], headers=pub_chat_headers)

    raw_chat = _download_gdrive_small_bytes(chat_drive_id, max_bytes=20 * 1024 * 1024)
    if not raw_chat:
        return j({"error": "chat_unavailable"}, 502)

    CHAT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp_chat = cached_chat.with_name(f".{cached_chat.stem}.{secrets.token_hex(4)}.jsonl")
    try:
        tmp_chat.write_bytes(raw_chat)
        tmp_chat.replace(cached_chat)
    except OSError:
        return Response(content=raw_chat, media_type=MIME[".jsonl"], headers=chat_headers)
    finally:
        if tmp_chat.exists():
            tmp_chat.unlink(missing_ok=True)

    return FileResponse(cached_chat, media_type=MIME[".jsonl"], headers=pub_chat_headers)


def video_file_for(folder: Path, filename: str) -> Path | None:
    if not safe_name(filename):
        return None
    direct = folder / filename
    if direct.is_file():
        return direct
    fallback = folder / "video.mp4"
    return fallback if fallback.is_file() else None


@app.api_route("/vods/{vod_id}/{filename}", methods=["GET", "HEAD"])
def vod_video(vod_id: str, filename: str, request: Request) -> Response:
    folder = find_vod(vod_id)
    if not folder:
        return j({"error": "not found"}, 404)
    video = video_file_for(folder, filename or "video.mp4")
    if not video:
        return j({"error": "not found"}, 404)
    size = video.stat().st_size
    media_type = MIME.get(video.suffix.lower(), "video/mp4")
    start, end = 0, max(0, size - 1)
    range_header = (request.headers.get("range") or "").strip()
    status_code = 200
    if range_header and range_header.lower().startswith("bytes="):
        if size <= 0:
            return j({"error": "range not satisfiable"}, 416, {"Content-Range": "bytes */0"})
        raw_range = range_header[6:].strip()
        if "," in raw_range or "-" not in raw_range:
            return j({"error": "bad range"}, 416, {"Content-Range": f"bytes */{size}"})
        start_s, _, end_s = raw_range.partition("-")
        start_s, end_s = start_s.strip(), end_s.strip()
        if not start_s and not end_s:
            return j({"error": "bad range"}, 416, {"Content-Range": f"bytes */{size}"})
        try:
            if not start_s:
                suffix_len = int(end_s)
                if suffix_len <= 0:
                    return j({"error": "range not satisfiable"}, 416, {"Content-Range": f"bytes */{size}"})
                start = max(0, size - suffix_len)
                end = size - 1
            else:
                start = int(start_s)
                end = int(end_s) if end_s else size - 1
                if start < 0 or start >= size or end < start:
                    return j({"error": "range not satisfiable"}, 416, {"Content-Range": f"bytes */{size}"})
                end = min(end, size - 1)
        except ValueError:
            return j({"error": "bad range"}, 416, {"Content-Range": f"bytes */{size}"})
        status_code = 206
    length = (end - start + 1) if size > 0 else 0

    headers = {"Accept-Ranges": "bytes", "Content-Length": str(length)}
    if status_code == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"

    if request.method == "HEAD":
        return Response(status_code=status_code, media_type=media_type, headers=headers)

    def iterator():
        with video.open("rb") as fh:
            fh.seek(start)
            remaining = length
            while remaining > 0:
                chunk = fh.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return StreamingResponse(iterator(), status_code=status_code, media_type=media_type, headers=headers)


_gdrive_uuid_cache: dict[str, tuple[float, str]] = {}
_gdrive_uuid_lock = threading.Lock()
_gdrive_unreachable_until: float = 0.0
_thumb_locks_guard = threading.Lock()
_thumb_locks: dict[str, threading.Lock] = {}
_thumb_sem = threading.BoundedSemaphore(2)


def _get_thumb_lock(vod_id: str) -> threading.Lock:
    with _thumb_locks_guard:
        if len(_thumb_locks) > 512:
            for k in list(_thumb_locks.keys())[:256]:
                if not _thumb_locks[k].locked():
                    _thumb_locks.pop(k, None)
        lk = _thumb_locks.get(vod_id)
        if lk is None:
            lk = threading.Lock()
            _thumb_locks[vod_id] = lk
        return lk


def _atomic_write_bytes(dest: Path, payload: bytes) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.stem}.{secrets.token_hex(4)}.jpg")
    try:
        tmp.write_bytes(payload)
        tmp.replace(dest)
        return True
    except OSError:
        return dest.is_file() and dest.stat().st_size > 1024
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def _find_ffmpeg_bin() -> str | None:
    for candidate in (ROOT / "ffmpeg.exe", ROOT / "ffmpeg", ROOT / "recorder" / "ffmpeg.exe"):
        if candidate.is_file():
            return str(candidate.resolve())
    return shutil.which("ffmpeg")


def _extract_ffmpeg_frame(input_target: str, dest: Path, seek_seconds: list[int]) -> bool:
    ffmpeg_exe = _find_ffmpeg_bin()
    if not ffmpeg_exe:
        return False
    is_remote = input_target.startswith("https://")
    proto_whitelist = "http,https,tcp,tls,crypto" if is_remote else "file,crypto,data"
    safe_target = input_target if is_remote else str(Path(input_target).resolve())
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.stem}.{secrets.token_hex(4)}.jpg")
    for ss in seek_seconds:
        try:
            subprocess.run(
                [
                    ffmpeg_exe, "-nostdin", "-y",
                    "-protocol_whitelist", proto_whitelist,
                    "-ss", str(int(ss)),
                    "-i", safe_target,
                    "-frames:v", "1",
                    "-vf", "scale=960:-2",
                    "-q:v", "3",
                    str(tmp),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=_WIN_NO_WINDOW,
                timeout=22,
                check=False,
            )
            if tmp.is_file() and tmp.stat().st_size > 1024:
                try:
                    tmp.replace(dest)
                except OSError:
                    pass
                return dest.is_file() and dest.stat().st_size > 1024
        except Exception:
            pass
        finally:
            if tmp.exists():
                tmp.unlink(missing_ok=True)
    return False


def _cache_gdrive_uuid(drive_id: str, found_uuid: str) -> None:
    now = time.time()
    with _gdrive_uuid_lock:
        if len(_gdrive_uuid_cache) > 512:
            expired = [k for k, v in _gdrive_uuid_cache.items() if v[0] <= now]
            for k in expired:
                _gdrive_uuid_cache.pop(k, None)
            if len(_gdrive_uuid_cache) > 512:
                _gdrive_uuid_cache.pop(next(iter(_gdrive_uuid_cache)))
        _gdrive_uuid_cache[drive_id] = (now + 1200, found_uuid)


def _resolve_gdrive_direct_url(drive_id: str) -> str | None:
    global _gdrive_unreachable_until
    if not _SAFE_GDRIVE_ID_RE.fullmatch(drive_id):
        return None
    now = time.time()
    if now < _gdrive_unreachable_until:
        return None
    with _gdrive_uuid_lock:
        entry = _gdrive_uuid_cache.get(drive_id)
        if entry and entry[0] > now and _SAFE_GDRIVE_UUID_RE.fullmatch(entry[1]):
            return f"https://drive.usercontent.google.com/download?id={drive_id}&export=download&confirm=t&uuid={entry[1]}"

    base_url = "https://drive.usercontent.google.com/download"
    params = {"id": drive_id, "export": "download", "confirm": "t"}
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
        "Range": "bytes=0-1023",
    }
    try:
        with requests.get(base_url, params=params, headers=headers, stream=True, timeout=(3, 10)) as r:
            ctype = (r.headers.get("content-type") or "").lower()
            if "text/html" in ctype:
                sample = _read_response_prefix(r)
                found_uuid = _extract_safe_gdrive_uuid(sample)
                if found_uuid:
                    _cache_gdrive_uuid(drive_id, found_uuid)
                    return f"{base_url}?id={drive_id}&export=download&confirm=t&uuid={found_uuid}"
                return None
            if r.status_code in (200, 206):
                return f"{base_url}?id={drive_id}&export=download&confirm=t"
    except (requests.RequestException, OSError):
        _gdrive_unreachable_until = time.time() + 25
    return None


@app.get("/api/vods/{vod_id}/thumb")
def vod_thumb(vod_id: str) -> Response:
    global _gdrive_unreachable_until
    if not safe_name(vod_id):
        return j({"error": "not found"}, 404)

    cache_headers = {"Cache-Control": "public, max-age=86400"}
    folder = find_vod(vod_id)
    if folder:
        thumb_path = folder / "thumb.jpg"
        if thumb_path.is_file() and thumb_path.stat().st_size > 512:
            return FileResponse(thumb_path, media_type="image/jpeg", headers=cache_headers)
        meta_file = folder / "meta.json"
        video_name = "video.mp4"
        duration_s = 0
        if meta_file.is_file():
            try:
                m = json.loads(meta_file.read_text(encoding="utf-8"))
                video_name = text(m.get("video_file"), 120) or "video.mp4"
                duration_s = int(m.get("duration_s") or 0)
            except Exception:
                pass
        video = video_file_for(folder, video_name)
        if video:
            with _get_thumb_lock(vod_id):
                if not (thumb_path.is_file() and thumb_path.stat().st_size > 512):
                    with _thumb_sem:
                        seeks = [min(180, max(5, duration_s // 5)) if duration_s > 10 else 5, 1]
                        _extract_ffmpeg_frame(str(video), thumb_path, seeks)
            if thumb_path.is_file() and thumb_path.stat().st_size > 512:
                return FileResponse(thumb_path, media_type="image/jpeg", headers=cache_headers)
        return j({"error": "no thumb"}, 404)

    row = _get_drive_vod_row(vod_id)
    drive_id = str(row.get("drive_id") or "").strip() if row else ""
    thumb_drive_id = str(row.get("thumb_drive_id") or "").strip() if row else ""
    if not drive_id:
        return j({"error": "not found"}, 404)

    cached_path = THUMB_CACHE_DIR / f"{_vod_cache_stem(vod_id)}.jpg"
    if cached_path.is_file() and cached_path.stat().st_size > 512:
        return FileResponse(cached_path, media_type="image/jpeg", headers=cache_headers)

    if time.time() < _gdrive_unreachable_until:
        return j({"error": "thumb_unavailable"}, 404)

    with _get_thumb_lock(vod_id):
        if cached_path.is_file() and cached_path.stat().st_size > 512:
            return FileResponse(cached_path, media_type="image/jpeg", headers=cache_headers)
        if time.time() < _gdrive_unreachable_until:
            return j({"error": "thumb_unavailable"}, 404)

        with _thumb_sem:
            # 1) Если в подпапке записи на Google Drive лежит готовый thumb.jpg — забираем его напрямую
            if thumb_drive_id:
                raw_thumb = _download_gdrive_small_bytes(thumb_drive_id, max_bytes=4 * 1024 * 1024)
                if raw_thumb and len(raw_thumb) > 512:
                    if _atomic_write_bytes(cached_path, raw_thumb):
                        return FileResponse(cached_path, media_type="image/jpeg", headers=cache_headers)

            # 2) Локальный ffmpeg по прямому потоку видео
            if _find_ffmpeg_bin():
                direct_url = _resolve_gdrive_direct_url(drive_id)
                if direct_url and _extract_ffmpeg_frame(direct_url, cached_path, [180, 30, 3]):
                    return FileResponse(cached_path, media_type="image/jpeg", headers=cache_headers)

            # 3) Превью от Google Drive (для thumb.jpg или для video.mp4)
            if time.time() >= _gdrive_unreachable_until:
                ua = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"}
                candidate_urls: list[str] = []
                if thumb_drive_id:
                    candidate_urls.append(f"https://drive.google.com/thumbnail?id={thumb_drive_id}&sz=w960")
                    candidate_urls.append(f"https://lh3.googleusercontent.com/d/{thumb_drive_id}=w960")
                candidate_urls.append(f"https://drive.google.com/thumbnail?id={drive_id}&sz=w960")
                candidate_urls.append(f"https://lh3.googleusercontent.com/d/{drive_id}=w960")
                for candidate_url in candidate_urls:
                    try:
                        r = requests.get(candidate_url, headers=ua, timeout=(3, 10))
                        ctype = (r.headers.get("content-type") or "").lower()
                        if r.status_code == 200 and ctype.startswith("image/") and len(r.content) > 512:
                            if _atomic_write_bytes(cached_path, r.content):
                                return FileResponse(cached_path, media_type="image/jpeg", headers=cache_headers)
                    except (requests.RequestException, OSError):
                        continue

    return j({"error": "thumb_unavailable"}, 404)


@app.post("/api/vods/{vod_id}/thumb")
async def cache_client_vod_thumb(vod_id: str, request: Request) -> JSONResponse:
    if not safe_name(vod_id):
        return j({"error": "not found"}, 404)
    require_same_origin(request)
    rate_limit(request, "vod_thumb_cache", 24, 60)

    folder = find_vod(vod_id)
    if folder:
        dest = folder / "thumb.jpg"
    elif _resolve_drive_id(vod_id):
        dest = THUMB_CACHE_DIR / f"{_vod_cache_stem(vod_id)}.jpg"
    else:
        return j({"error": "not found"}, 404)

    if dest.is_file() and dest.stat().st_size > 1024:
        return j({"cached": True, "already": True})

    raw = await request.body()
    if len(raw) < 1024 or len(raw) > 524288:
        return j({"error": "invalid_size"}, 400)
    if raw[:3] != b"\xff\xd8\xff" or raw[-2:] != b"\xff\xd9":
        return j({"error": "invalid_jpeg"}, 400)

    with _get_thumb_lock(vod_id):
        if not (dest.is_file() and dest.stat().st_size > 1024):
            _atomic_write_bytes(dest, raw)
    return j({"cached": True})


def _resolve_drive_id(vod_id: str) -> str | None:
    if not safe_name(vod_id):
        return None
    with _db_lock, db_connect() as conn:
        row = conn.execute("SELECT drive_id FROM drive_vods WHERE id = ?", (vod_id,)).fetchone()
        return str(row["drive_id"]) if row and row["drive_id"] else None


@app.api_route("/api/vods/{vod_id}/stream", methods=["GET", "HEAD"])
def vod_stream(vod_id: str, request: Request) -> Response:
    folder = find_vod(vod_id)
    if folder:
        meta_path = folder / "meta.json"
        video_name = "video.mp4"
        if meta_path.is_file():
            try:
                meta_obj = json.loads(meta_path.read_text(encoding="utf-8"))
                video_name = str(meta_obj.get("video_file") or "video.mp4")
            except Exception:
                pass
        return vod_video(vod_id, video_name, request)

    drive_id = _resolve_drive_id(vod_id)
    if not drive_id or not _SAFE_GDRIVE_ID_RE.fullmatch(drive_id):
        return j({"error": "not found"}, 404)

    now = time.time()
    cached_uuid = ""
    with _gdrive_uuid_lock:
        entry = _gdrive_uuid_cache.get(drive_id)
        if entry and entry[0] > now and _SAFE_GDRIVE_UUID_RE.fullmatch(entry[1]):
            cached_uuid = entry[1]

    req_headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    }
    range_header = (request.headers.get("range") or "").strip()
    if range_header:
        if not re.fullmatch(r"bytes=\d{0,16}-\d{0,16}", range_header, flags=re.IGNORECASE):
            return j({"error": "bad range"}, 416)
        req_headers["Range"] = range_header

    base_url = "https://drive.usercontent.google.com/download"
    params: dict[str, str] = {"id": drive_id, "export": "download", "confirm": "t"}
    if cached_uuid:
        params["uuid"] = cached_uuid

    session = requests.Session()
    try:
        upstream = session.request(
            "GET" if request.method == "HEAD" and not cached_uuid else request.method,
            base_url,
            params=params,
            headers=req_headers,
            stream=True,
            timeout=(4, 30),
        )
        ctype = (upstream.headers.get("content-type") or "").lower()
        if "text/html" in ctype:
            if cached_uuid:
                with _gdrive_uuid_lock:
                    _gdrive_uuid_cache.pop(drive_id, None)
            html_sample = _read_response_prefix(upstream)
            upstream.close()
            found_uuid = _extract_safe_gdrive_uuid(html_sample)
            if not found_uuid and request.method == "HEAD" and cached_uuid:
                probe_params = {"id": drive_id, "export": "download", "confirm": "t"}
                probe_headers = dict(req_headers)
                probe_headers.setdefault("Range", "bytes=0-1023")
                with session.get(base_url, params=probe_params, headers=probe_headers, stream=True, timeout=(4, 15)) as probe:
                    if "text/html" in (probe.headers.get("content-type") or "").lower():
                        found_uuid = _extract_safe_gdrive_uuid(_read_response_prefix(probe))
            if found_uuid:
                _cache_gdrive_uuid(drive_id, found_uuid)
                params["uuid"] = found_uuid
                upstream = session.request(
                    request.method,
                    base_url,
                    params=params,
                    headers=req_headers,
                    stream=True,
                    timeout=(4, 30),
                )
                ctype = (upstream.headers.get("content-type") or "").lower()

        if upstream.status_code >= 400 or "text/html" in ctype:
            upstream.close()
            session.close()
            return j({"error": "upstream_unavailable"}, 502)
    except (requests.RequestException, OSError):
        session.close()
        return j({"error": "upstream_unreachable"}, 502)

    want_download = (request.query_params.get("download") or "").strip() == "1"
    dl_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", vod_id)[:80].strip("_.") or "stream"
    out_headers: dict[str, str] = {
        "Accept-Ranges": "bytes",
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": f'attachment; filename="{dl_name}.mp4"' if want_download else "inline",
    }
    for h_name in ("Content-Length", "Content-Range"):
        val = upstream.headers.get(h_name)
        if val:
            out_headers[h_name] = val
    raw_mime = (upstream.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
    media_type = raw_mime if raw_mime in _SAFE_STREAM_MIMES else "video/mp4"
    status_code = upstream.status_code if upstream.status_code in (200, 206) else 200

    if request.method == "HEAD":
        upstream.close()
        session.close()
        return Response(status_code=status_code, media_type=media_type, headers=out_headers)

    def stream_chunks():
        sent = 0
        try:
            for chunk in upstream.iter_content(chunk_size=64 * 1024):
                if chunk:
                    sent += len(chunk)
                    yield chunk
        except (requests.RequestException, OSError):
            return
        finally:
            if sent > 0:
                with contextlib.suppress(Exception):
                    record_vod_stream_bytes(vod_id, sent)
            upstream.close()
            session.close()

    return StreamingResponse(stream_chunks(), status_code=status_code, media_type=media_type, headers=out_headers)


# ----------------------------- Статика -----------------------------

STATIC_ALLOWED = {
    ".html", ".css", ".js", ".xml", ".txt", ".png", ".jpg", ".jpeg",
    ".webp", ".svg", ".ico", ".mp4", ".webm", ".mkv", ".mov",
}
STATIC_EXACT_NAMES = {"CNAME"}
STATIC_BLOCKED_ROOTS = {"data", "recorder", "vods", ".git", ".github", ".venv", "venv", "__pycache__"}


def resolve_static(full_path: str) -> Path | None:
    rel = (full_path or "").replace("\\", "/").lstrip("/")
    if not rel:
        rel = "index.html"
    elif rel.endswith("/"):
        trimmed = rel.rstrip("/")
        if trimmed and "." not in trimmed.split("/")[-1]:
            candidate_html = (ROOT / (trimmed + ".html")).resolve()
            try:
                candidate_html.relative_to(ROOT)
                if candidate_html.is_file():
                    rel = trimmed + ".html"
                else:
                    rel += "index.html"
            except Exception:
                return None
        else:
            rel += "index.html"
    if not rel or rel.startswith("/") or ".." in rel.split("/"):
        return None
    parts = rel.split("/")
    if any(p.startswith(".") for p in parts):
        return None
    if parts[0] in STATIC_BLOCKED_ROOTS:
        return None
    path = (ROOT / rel).resolve()
    try:
        path.relative_to(ROOT)
    except Exception:
        return None
    if path.is_dir():
        path = path / "index.html"
    if not path.suffix and path.name not in STATIC_EXACT_NAMES and path.with_name(path.name + ".html").is_file():
        path = path.with_name(path.name + ".html")
    if path.name not in STATIC_EXACT_NAMES and path.suffix.lower() not in STATIC_ALLOWED:
        return None
    if not path.is_file():
        return None
    return path


@app.api_route("/{full_path:path}", methods=["GET", "HEAD"])
def static_file(full_path: str, request: Request) -> Response:
    path = resolve_static(full_path)
    if not path:
        return j({"error": "not found"}, 404)
    ext = path.suffix.lower()
    if ext == ".html":
        nonce = b64url(secrets.token_bytes(16))
        body = path.read_text(encoding="utf-8", errors="replace").replace("__CSP_NONCE__", nonce)
        encoded = body.encode("utf-8")
        headers = {"Cache-Control": "private, no-cache, no-transform", "Content-Length": str(len(encoded))}
        if path.name.lower() == "admin.html":
            headers["X-Robots-Tag"] = "noindex, nofollow"
        headers.update(
            security_headers(
                nonce,
                is_https=is_https_request(request),
                allow_frame=_is_e2b_preview_request(request),
            )
        )
        if request.method == "HEAD":
            return Response(status_code=200, media_type=MIME[".html"], headers=headers)
        return Response(content=encoded, status_code=200, media_type=MIME[".html"], headers=headers)

    media_type = MIME.get(ext, "text/plain; charset=utf-8")
    try:
        st = path.stat()
        etag = f'W/"{st.st_mtime_ns:x}-{st.st_size:x}"'
    except OSError:
        etag = ""

    cache_ctrl = "no-cache" if ext in {".css", ".js"} else "public, max-age=86400"
    if etag and (request.headers.get("if-none-match") or "").strip() == etag:
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": cache_ctrl})

    if ext in {".css", ".js", ".svg", ".xml", ".txt"} and st.st_size <= 2 * 1024 * 1024:
        raw_bytes = path.read_bytes()
        headers = {"Cache-Control": cache_ctrl, "Content-Length": str(len(raw_bytes))}
        if etag:
            headers["ETag"] = etag
        if request.method == "HEAD":
            return Response(status_code=200, media_type=media_type, headers=headers)
        return Response(content=raw_bytes, status_code=200, media_type=media_type, headers=headers)

    extra_headers: dict[str, str] = {"Cache-Control": cache_ctrl}
    if etag:
        extra_headers["ETag"] = etag
    return FileResponse(path, media_type=media_type, headers=extra_headers)


def _start_auto_update_watcher() -> None:
    interval = max(60, AUTO_UPDATE_INTERVAL)

    def _loop() -> None:
        while True:
            time.sleep(interval)
            res = perform_github_update(force=False, allow_zip_fallback_on_api_error=False)
            if res.get("ok") and res.get("server_changed"):
                _reexec_current_process()

    t = threading.Thread(target=_loop, name="krafen-auto-updater", daemon=True)
    t.start()
    print(f"автообновление: фоновая проверка каждые {interval} сек.", flush=True)


def start_recorder() -> subprocess.Popen | None:
    script = ROOT / "recorder" / "recorder.py"
    if not script.is_file():
        print("recorder/recorder.py не найден — автозапись выключена", flush=True)
        return None
    env = os.environ.copy()
    env.setdefault("RECORD_DIR", str(RECORD_DIR))
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUNBUFFERED", "1")
    print("автозапись: запускаю recorder/recorder.py", flush=True)
    kwargs: dict[str, Any] = {
        "env": env,
        "cwd": str(ROOT),
    }
    if _HEADLESS_LOG_FH is not None:
        exe = Path(sys.executable).resolve()
        py_console = exe.with_name("python.exe")
        runner = str(py_console if py_console.is_file() else exe)
        kwargs["stdin"] = subprocess.DEVNULL
        kwargs["stdout"] = _HEADLESS_LOG_FH
        kwargs["stderr"] = _HEADLESS_LOG_FH
        kwargs["creationflags"] = _WIN_NO_WINDOW
    else:
        runner = sys.executable
    return subprocess.Popen([runner, str(script)], **kwargs)


def main() -> None:
    global _ACTIVE_REC_PROC
    _configure_stdio_utf8(mirror_console_to_log=True)
    mode_label = "фоновый режим" if _HEADLESS_LOG_FH is not None else "консоль"
    print(f"\n=== [{time.strftime('%Y-%m-%d %H:%M:%S')}] запуск krafen ({mode_label}, PID {os.getpid()}) ===", flush=True)
    with contextlib.suppress(OSError):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        PID_FILE_PATH.write_text(f"{os.getpid()}\n", encoding="utf-8")
    if is_auto_update_enabled() and AUTO_UPDATE_INTERVAL > 0:
        _start_auto_update_watcher()
    if _ADMIN_PASSWORD_IS_PLACEHOLDER:
        print("ВНИМАНИЕ: ADMIN_PASSWORD оставлен как 'change-me' из примера — вход в админ-режим отключён до смены пароля", flush=True)
    elif not ADMIN_PASSWORD:
        print("ВНИМАНИЕ: ADMIN_PASSWORD не задан, вход в админ-режим отключён", flush=True)
    rec_proc = start_recorder() if os.environ.get("RECORDER") == "1" else None
    _ACTIVE_REC_PROC = rec_proc
    ssl_certfile = (os.environ.get("SSL_CERTFILE") or "").strip() or None
    ssl_keyfile = (os.environ.get("SSL_KEYFILE") or "").strip() or None
    scheme_lbl = "https" if (ssl_certfile and ssl_keyfile) else "http"
    print(f"krafen server → {scheme_lbl}://{HOST}:{PORT}", flush=True)
    print(f"статика: {ROOT}", flush=True)
    print(f"база данных: {DB_PATH}", flush=True)
    print(f"папка записей: {RECORD_DIR}", flush=True)
    try:
        uvicorn.run(
            app,
            host=HOST,
            port=PORT,
            log_level="info",
            ssl_certfile=ssl_certfile,
            ssl_keyfile=ssl_keyfile,
        )
    finally:
        _ACTIVE_REC_PROC = None
        if _read_pid_file() == os.getpid():
            with contextlib.suppress(OSError):
                PID_FILE_PATH.unlink(missing_ok=True)
        if rec_proc and rec_proc.poll() is None:
            rec_proc.terminate()
            try:
                rec_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                rec_proc.kill()
            print("рекордер остановлен", flush=True)


if __name__ == "__main__":
    main()

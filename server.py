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
import hashlib
import html
import hmac
import io
import json
import os
import re
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

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
SEED_SQL_PATH = DATA_DIR / "seed.sql"
THUMB_CACHE_DIR = DATA_DIR / "vod_thumbs"
CHAT_CACHE_DIR = DATA_DIR / "vod_chats"
RECORD_DIR = _resolve_root_path(os.environ.get("RECORD_DIR", ROOT / "vods"))
PORT = int(os.environ.get("PORT", "8080"))
HOST = os.environ.get("HOST", "0.0.0.0")
PUBLIC_ORIGIN = os.environ.get("PUBLIC_ORIGIN", "").strip().rstrip("/")

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
SECURE_COOKIES_MODE = os.environ.get("SECURE_COOKIES", "auto").strip().lower()

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
        if len(parts) >= 2 and parts[1] in {".update_sha", "vod_thumbs", "vod_chats"}:
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


def _download_github_zip() -> bytes:
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
    for url, req_headers in candidates:
        try:
            with requests.get(url, headers=req_headers, stream=True, timeout=25) as resp:
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

            with zipfile.ZipFile(io.BytesIO(raw_zip)) as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    if ((info.external_attr >> 16) & 0o170000) == 0o120000:
                        continue
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
            return {"ok": False, "error": str(exc), "updated": [], "server_changed": False}


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


if __name__ == "__main__":
    with contextlib.suppress(OSError):
        os.chdir(ROOT)
    _configure_stdio_utf8()
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
    _start_drive_vods_poller()
    yield


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
            INSERT INTO watchlist_items (pos, id, title, title_ru, year, type, status, poster, added_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                genres_json, platforms_json, category, added_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                pos, id, title, category, status, priority, price, currency, note, link, image, added_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
    if not url:
        return None
    return url if url.startswith("https://") else None


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


def sign(value: str) -> str:
    return b64url(hmac.new(SECRET_KEY.encode(), value.encode(), hashlib.sha256).digest())


def constant_time_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


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
    if not token or not ADMIN_PASSWORD:
        return False
    try:
        payload, signature = token.split(".", 1)
        if not constant_time_equal(signature, sign(payload)):
            return False
        data = json.loads(b64url_decode(payload))
        return int(data.get("exp", 0)) > int(time.time())
    except Exception:
        return False


def require_admin(request: Request) -> None:
    if not verify_session(request.cookies.get(SESSION_COOKIE)):
        raise HTTPException(status_code=401, detail={"error": "unauthorized"})


def is_https_request(request: Request) -> bool:
    proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    if proto:
        return proto == "https"
    return request.url.scheme == "https"


def cookie_header(request: Request, name: str, value: str, max_age: int, path: str = "/") -> str:
    https = is_https_request(request)
    if SECURE_COOKIES_MODE == "1":
        secure = True
    elif SECURE_COOKIES_MODE == "0":
        secure = False
    else:
        secure = https

    in_iframe = request.headers.get("sec-fetch-dest", "").lower() == "iframe"
    same_site = "None" if (in_iframe and secure) else "Lax"

    parts = [f"{name}={value}", f"Max-Age={max_age}", f"Path={path}", "HttpOnly", f"SameSite={same_site}"]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def client_ip(request: Request) -> str:
    forwarded = (
        request.headers.get("cf-connecting-ip")
        or request.headers.get("x-real-ip")
        or request.headers.get("x-forwarded-for")
        or ""
    )
    if forwarded:
        return forwarded.split(",")[0].strip()[:80]
    return request.client.host if request.client else "unknown"


def rate_limit(request: Request, name: str, limit: int = 60, period: int = 60) -> None:
    key = f"{name}:{client_ip(request)}"
    now = time.time()
    with _rate_lock:
        if len(_rate_buckets) > 2048:
            cutoff = now - max(period, 300)
            stale = [k for k, v in _rate_buckets.items() if not v or v[-1] <= cutoff]
            for k in stale:
                _rate_buckets.pop(k, None)
        bucket = [ts for ts in _rate_buckets.get(key, []) if ts > now - period]
        if len(bucket) >= limit:
            raise HTTPException(status_code=429, detail={"error": "rate_limited"})
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


def normalize_watchlist_item(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    item_id = text(item.get("id"), 120)
    title = text(item.get("title"), 180)
    item_type = text(item.get("type"), 20)
    status = text(item.get("status"), 20)
    if not ITEM_ID_PATTERN.fullmatch(item_id) or not title or item_type not in VALID_TYPES or status not in VALID_STATUSES:
        return None
    now = int(time.time() * 1000)
    return {
        "id": item_id,
        "title": title,
        "titleRu": text(item.get("titleRu"), 180) or None,
        "year": text(item.get("year"), 24) or "-",
        "type": item_type,
        "status": status,
        "poster": clean_url(item.get("poster")),
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


def init_db() -> None:
    now = int(time.time())
    with _db_lock, db_connect() as conn:
        conn.executescript(SCHEMA_SQL)

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


def security_headers(nonce: str | None = None) -> dict[str, str]:
    script_src = "script-src 'self'"
    if nonce:
        script_src += f" 'nonce-{nonce}'"
    style_src = "'self' 'unsafe-inline' https://fonts.googleapis.com"
    return {
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "strict-origin-when-cross-origin",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
        "Content-Security-Policy": "; ".join(
            [
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
                "connect-src 'self' https://discord.com",
            ]
        ),
    }


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    for key, value in security_headers().items():
        response.headers.setdefault(key, value)
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
        raw_items = body.get("items") if isinstance(body.get("items"), list) else []
        items = [normalizer(item) for item in raw_items]
        items = [item for item in items if item][: LIST_SPECS[name]["max_items"]]

        ok, next_state = update_list_atomic(name, revision, items)
        if not ok:
            return j({"error": "revision_conflict", "items": next_state["items"], "revision": next_state["revision"]}, 409)
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
    response = j({"ok": True})
    response.headers["Set-Cookie"] = cookie_header(request, SESSION_COOKIE, "", 0)
    return response


@app.post("/api/admin")
def admin_login(request: Request, body: dict[str, Any]) -> JSONResponse:
    rate_limit(request, "admin_login", 10, 60)
    if not ADMIN_PASSWORD:
        return j({"error": "admin_password_missing"}, 503)
    if not constant_time_equal(text(body.get("password"), 512), ADMIN_PASSWORD):
        return j({"error": "invalid_password"}, 401)
    response = j({"ok": True})
    response.headers["Set-Cookie"] = cookie_header(request, SESSION_COOKIE, create_session(), SESSION_TTL_SECONDS)
    return response


@app.post("/api/admin/update")
def admin_trigger_update(request: Request) -> JSONResponse:
    require_admin(request)
    rate_limit(request, "admin_update", 6, 60)
    result = perform_github_update(force=True, allow_zip_fallback_on_api_error=True)
    status = 200 if result.get("ok") else 502
    if result.get("ok") and result.get("server_changed"):
        threading.Timer(1.0, _reexec_current_process).start()
    return j(result, status)


# ----------------------------- Поиск (TMDB / Shikimori / IGDB) -----------------------------


def tmdb_poster(path: Any) -> str | None:
    value = text(path, 120)
    return f"{TMDB_IMAGE_BASE}{value}" if value.startswith("/") else None


def igdb_cover(image_id: Any) -> str | None:
    value = text(image_id, 80)
    return f"https://images.igdb.com/igdb/image/upload/t_cover_big/{value}.jpg" if value else None


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
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "http").split(",")[0].strip()
    host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc).split(",")[0].strip()
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
            "profileImage": user.get("profile_image_url", ""),
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
        icon_url = f"https://cdn.discordapp.com/icons/{guild_id}/{icon_hash}.png?size=128" if guild_id and icon_hash else None
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


def _probe_gdrive_file_size(drive_id: str) -> int:
    """Определяет точный размер файла на Google Drive по 1-байтовому Range-запросу."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{10,80}", drive_id):
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
            uuid_m = re.search(r'name="uuid"\s+value="([^"]+)"', sample)
            if not uuid_m:
                return 0
            params["uuid"] = uuid_m.group(1)
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
    if not re.fullmatch(r"[A-Za-z0-9_-]{10,80}", drive_id):
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
            uuid_m = re.search(r'name="uuid"\s+value="([^"]+)"', sample)
            if not uuid_m:
                return None
            params["uuid"] = uuid_m.group(1)
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


def list_vods(force_sync: bool = False) -> list[dict[str, Any]]:
    if force_sync:
        maybe_sync_drive_vods(force=True)

    vods_by_id: dict[str, dict[str, Any]] = {}

    if RECORD_DIR.exists():
        for folder in sorted(RECORD_DIR.iterdir(), reverse=True):
            if not folder.is_dir():
                continue
            meta_file = folder / "meta.json"
            if not meta_file.is_file():
                continue
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
            except Exception:
                continue
            video_file = text(meta.get("video_file"), 120) or "video.mp4"
            thumb_file = text(meta.get("thumb_file"), 120) or "thumb.jpg"
            q_id = quote(folder.name, safe="")
            meta["id"] = folder.name
            meta["source"] = "local"
            meta["has_chat"] = (folder / "chat.jsonl").is_file()
            meta["has_video"] = (folder / video_file).is_file() or (folder / "video.mp4").is_file()
            meta["has_thumb"] = (folder / thumb_file).is_file() or (folder / "thumb.jpg").is_file()
            meta["thumb_url"] = f"/api/vods/{q_id}/thumb"
            meta["thumb_fallbacks"] = []
            meta["sort_key"] = folder.name
            vods_by_id[folder.name] = meta

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

            fallbacks: list[str] = []
            if thumb_drive_id:
                fallbacks.append(f"https://drive.google.com/thumbnail?id={thumb_drive_id}&sz=w960")
                fallbacks.append(f"https://lh3.googleusercontent.com/d/{thumb_drive_id}=w960")
            fallbacks.append(f"https://drive.google.com/thumbnail?id={drive_id}&sz=w960")
            fallbacks.append(f"https://lh3.googleusercontent.com/d/{drive_id}=w960")

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
                "download_url": f"https://drive.usercontent.google.com/download?id={drive_id}&export=download&confirm=t",
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

    if not force_sync:
        maybe_sync_drive_vods(force=False)
    return sorted(vods_by_id.values(), key=lambda x: str(x.get("sort_key") or x.get("id") or ""), reverse=True)


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
def vod_chat(vod_id: str):
    if not safe_name(vod_id):
        return j({"error": "not found"}, 404)
    folder = find_vod(vod_id)
    chat = folder / "chat.jsonl" if folder else None
    if chat and chat.is_file():
        return FileResponse(chat, media_type=MIME[".jsonl"])

    row = _get_drive_vod_row(vod_id)
    chat_drive_id = str(row.get("chat_drive_id") or "").strip() if row else ""
    if not chat_drive_id:
        return j({"error": "no chat"}, 404)

    cached_chat = CHAT_CACHE_DIR / f"{_vod_cache_stem(vod_id)}.jsonl"
    if cached_chat.is_file() and cached_chat.stat().st_size > 0:
        return FileResponse(cached_chat, media_type=MIME[".jsonl"], headers={"Cache-Control": "public, max-age=86400"})

    raw_chat = _download_gdrive_small_bytes(chat_drive_id, max_bytes=20 * 1024 * 1024)
    if not raw_chat:
        return j({"error": "chat_unavailable"}, 502)

    CHAT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp_chat = cached_chat.with_name(f".{cached_chat.stem}.{secrets.token_hex(4)}.jsonl")
    try:
        tmp_chat.write_bytes(raw_chat)
        tmp_chat.replace(cached_chat)
    except OSError:
        return Response(content=raw_chat, media_type=MIME[".jsonl"])
    finally:
        if tmp_chat.exists():
            tmp_chat.unlink(missing_ok=True)

    return FileResponse(cached_chat, media_type=MIME[".jsonl"], headers={"Cache-Control": "public, max-age=86400"})


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
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.stem}.{secrets.token_hex(4)}.jpg")
    for ss in seek_seconds:
        try:
            subprocess.run(
                [
                    ffmpeg_exe, "-y",
                    "-ss", str(ss),
                    "-i", input_target,
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


def _resolve_gdrive_direct_url(drive_id: str) -> str | None:
    global _gdrive_unreachable_until
    now = time.time()
    if now < _gdrive_unreachable_until:
        return None
    with _gdrive_uuid_lock:
        entry = _gdrive_uuid_cache.get(drive_id)
        if entry and entry[0] > now:
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
                uuid_m = re.search(r'name="uuid"\s+value="([^"]+)"', sample)
                if uuid_m:
                    found_uuid = uuid_m.group(1)
                    with _gdrive_uuid_lock:
                        _gdrive_uuid_cache[drive_id] = (time.time() + 1200, found_uuid)
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
    sec_site = (request.headers.get("sec-fetch-site") or "").lower()
    if sec_site and sec_site not in ("same-origin", "none"):
        return j({"error": "forbidden"}, 403)
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
    if not drive_id:
        return j({"error": "not found"}, 404)

    now = time.time()
    cached_uuid = ""
    with _gdrive_uuid_lock:
        entry = _gdrive_uuid_cache.get(drive_id)
        if entry and entry[0] > now:
            cached_uuid = entry[1]

    req_headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    }
    range_header = request.headers.get("range")
    if range_header:
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
            uuid_m = re.search(r'name="uuid"\s+value="([^"]+)"', html_sample)
            if not uuid_m and request.method == "HEAD" and cached_uuid:
                probe_params = {"id": drive_id, "export": "download", "confirm": "t"}
                probe_headers = dict(req_headers)
                probe_headers.setdefault("Range", "bytes=0-1023")
                with session.get(base_url, params=probe_params, headers=probe_headers, stream=True, timeout=(4, 15)) as probe:
                    if "text/html" in (probe.headers.get("content-type") or "").lower():
                        uuid_m = re.search(r'name="uuid"\s+value="([^"]+)"', _read_response_prefix(probe))
            if uuid_m:
                found_uuid = uuid_m.group(1)
                with _gdrive_uuid_lock:
                    _gdrive_uuid_cache[drive_id] = (time.time() + 1200, found_uuid)
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

    out_headers: dict[str, str] = {"Accept-Ranges": "bytes"}
    for h_name in ("Content-Length", "Content-Range"):
        val = upstream.headers.get(h_name)
        if val:
            out_headers[h_name] = val
    media_type = upstream.headers.get("Content-Type") or "video/mp4"
    status_code = upstream.status_code if upstream.status_code in (200, 206) else 200

    if request.method == "HEAD":
        upstream.close()
        session.close()
        return Response(status_code=status_code, media_type=media_type, headers=out_headers)

    def stream_chunks():
        try:
            for chunk in upstream.iter_content(chunk_size=64 * 1024):
                if chunk:
                    yield chunk
        except (requests.RequestException, OSError):
            return
        finally:
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
    if path.suffix.lower() == ".html":
        nonce = b64url(secrets.token_bytes(16))
        try:
            body = path.read_text(encoding="utf-8").replace("__CSP_NONCE__", nonce)
        except UnicodeDecodeError:
            return FileResponse(path, media_type=MIME[".html"])
        encoded = body.encode("utf-8")
        headers = {"Cache-Control": "no-cache", "Content-Length": str(len(encoded))}
        headers.update(security_headers(nonce))
        if request.method == "HEAD":
            return Response(status_code=200, media_type=MIME[".html"], headers=headers)
        return Response(content=encoded, status_code=200, media_type=MIME[".html"], headers=headers)
    media_type = MIME.get(path.suffix.lower(), "text/plain; charset=utf-8")
    extra_headers = {"Cache-Control": "no-cache"} if path.suffix.lower() in {".css", ".js"} else None
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
    if not ADMIN_PASSWORD:
        print("ВНИМАНИЕ: ADMIN_PASSWORD не задан, вход в админ-режим отключён", flush=True)
    rec_proc = start_recorder() if os.environ.get("RECORDER") == "1" else None
    _ACTIVE_REC_PROC = rec_proc
    print(f"krafen server → http://{HOST}:{PORT}", flush=True)
    print(f"статика: {ROOT}", flush=True)
    print(f"база данных: {DB_PATH}", flush=True)
    print(f"папка записей: {RECORD_DIR}", flush=True)
    try:
        uvicorn.run(app, host=HOST, port=PORT, log_level="info")
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

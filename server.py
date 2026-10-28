#!/usr/bin/env python3
"""
Обычный сервер Krafen / danonsha-archive.

Заменяет Cloudflare Worker + KV:
  - отдаёт статику сайта;
  - хранит watchlist/gamelist/setup/wishlist в SQLite/JSON-файлах;
  - проксирует Twitch, Last.fm, Spotify, TMDB, Shikimori, IGDB;
  - отдаёт VOD-архив с поддержкой Range;
  - умеет запускать recorder/recorder.py через RECORDER=1.

Запуск:
  export ADMIN_PASSWORD=...
  export SECRET_KEY=...
  RECORDER=1 python3 server.py
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests
import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", ROOT / "data"))
RECORD_DIR = Path(os.environ.get("RECORD_DIR", ROOT / "vods"))
PORT = int(os.environ.get("PORT", "8080"))
HOST = os.environ.get("HOST", "0.0.0.0")

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
SECRET_KEY = os.environ.get("SECRET_KEY", ADMIN_PASSWORD or secrets.token_hex(32))
SECURE_COOKIES = os.environ.get("SECURE_COOKIES", "1") == "1"
TWITCH_CHANNEL = os.environ.get("TWITCH_CHANNEL", "danonsha")
TWITCH_CLIENT_ID = os.environ.get("TWITCH_CLIENT_ID", "")
TWITCH_CLIENT_SECRET = os.environ.get("TWITCH_CLIENT_SECRET", "")
LASTFM_API_KEY = os.environ.get("LASTFM_API_KEY", "")
LASTFM_USER = os.environ.get("LASTFM_USER", "krafen")
TMDB_ACCESS_TOKEN = os.environ.get("TMDB_ACCESS_TOKEN", "")
TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "")
SPOTIFY_CLIENT_ID = os.environ.get("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "")

SESSION_COOKIE = "kf_admin_session"
SPOTIFY_STATE_COOKIE = "kf_spotify_oauth"
SESSION_TTL_SECONDS = 8 * 60 * 60

TMDB_BASE = "https://api.themoviedb.org"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w342"
SHIKIMORI_BASE = "https://shikimori.one"
IGDB_BASE = "https://api.igdb.com/v4"
TWITCH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
LASTFM_API_BASE = "https://ws.audioscrobbler.com/2.0/"
SPOTIFY_API_BASE = "https://api.spotify.com/v1"
SPOTIFY_AUTH_URL = "https://accounts.spotify.com/authorize"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"

DATA_DIR.mkdir(parents=True, exist_ok=True)
RECORD_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

VALID_TYPES = {"anime", "movie", "series"}
VALID_STATUSES = {"watched", "planned", "dropped"}
VALID_GAME_STATUSES = {"completed", "playing", "dropped", "wishlist", "paused"}
VALID_SETUP_SECTIONS = {"pc", "gear"}
VALID_SETUP_ICONS = {
    "cpu", "gpu", "ram", "board", "ssd", "hdd", "cooler", "psu", "case",
    "audio", "monitor", "keyboard", "mouse", "mic", "other",
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
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".jsonl": "application/x-ndjson; charset=utf-8",
}

LIST_SPECS = {
    "watchlist": {
        "file": "watchlist.json",
        "normalizer": "watchlist",
        "max_items": 500,
        "cache_ttl": 300,
    },
    "gamelist": {
        "file": "gamelist.json",
        "normalizer": "gamelist",
        "max_items": 500,
        "cache_ttl": 300,
    },
    "setup": {
        "file": "setup.json",
        "normalizer": "setup",
        "max_items": 120,
        "cache_ttl": 300,
    },
    "wishlist": {
        "file": "wishlist.json",
        "normalizer": "wishlist",
        "max_items": 500,
        "cache_ttl": 300,
    },
}

_store_lock = threading.Lock()
_list_cache: dict[str, dict[str, Any]] = {}
_rate_lock = threading.Lock()
_rate_buckets: dict[str, list[float]] = {}
_twitch_token: dict[str, Any] = {}
_igdb_token: dict[str, Any] = {}


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
    payload = b64url(json.dumps({
        "exp": int(time.time()) + SESSION_TTL_SECONDS,
        "nonce": secrets.token_hex(16),
    }, separators=(",", ":")).encode())
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


def cookie_header(name: str, value: str, max_age: int, path: str = "/") -> str:
    parts = [f"{name}={value}", f"Max-Age={max_age}", f"Path={path}", "HttpOnly", "SameSite=Strict"]
    if SECURE_COOKIES:
        parts.append("Secure")
    return "; ".join(parts)


def rate_limit(request: Request, name: str, limit: int = 60, period: int = 60) -> None:
    ip = request.client.host if request.client else "unknown"
    key = f"{name}:{ip}"
    now = time.time()
    with _rate_lock:
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


def read_json_file(path: Path, default: Any) -> Any:
    try:
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json_file(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, path)


def list_path(name: str) -> Path:
    return DATA_DIR / LIST_SPECS[name]["file"]


def normalize_stored_list(value: Any) -> dict[str, Any]:
    if isinstance(value, list):
        return {"revision": 0, "items": value}
    if isinstance(value, dict) and isinstance(value.get("items"), list):
        revision = value.get("revision")
        if not isinstance(revision, int) or revision < 0:
            revision = 0
        return {"revision": revision, "items": value["items"]}
    return {"revision": 0, "items": []}


def read_list(name: str, use_cache: bool = True) -> dict[str, Any]:
    if use_cache:
        cached = _list_cache.get(name)
        if cached and cached["ts"] > time.time() - LIST_SPECS[name]["cache_ttl"]:
            return {"revision": cached["revision"], "items": list(cached["items"])}
    state = normalize_stored_list(read_json_file(list_path(name), None))
    with _store_lock:
        _list_cache[name] = {"ts": time.time(), "revision": state["revision"], "items": list(state["items"])}
    return state


def write_list(name: str, state: dict[str, Any]) -> None:
    with _store_lock:
        write_json_file(list_path(name), state)
        _list_cache[name] = {"ts": time.time(), "revision": state["revision"], "items": list(state["items"])}


def tokens_path() -> Path:
    return DATA_DIR / "tokens.json"


def read_tokens() -> dict[str, Any]:
    value = read_json_file(tokens_path(), {})
    return value if isinstance(value, dict) else {}


def write_tokens(value: dict[str, Any]) -> None:
    with _store_lock:
        write_json_file(tokens_path(), value)


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
        "addedAt": int(item.get("addedAt") or now),
        "updatedAt": int(item.get("updatedAt") or now),
    }


def normalize_gamelist_item(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    item_id = text(item.get("id"), 120)
    title = text(item.get("title"), 180)
    status = text(item.get("status"), 20)
    rating = max(0, min(10, float(item.get("rating") or 0)))
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
        "category": int(item.get("category") or 0),
        "addedAt": int(item.get("addedAt") or now),
        "updatedAt": int(item.get("updatedAt") or now),
    }


def normalize_setup_item(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    item_id = text(item.get("id"), 120)
    section = text(item.get("section"), 20)
    slot = text(item.get("slot"), 80)
    title = text(item.get("title"), 180)
    if not ITEM_ID_PATTERN.fullmatch(item_id) or section not in VALID_SETUP_SECTIONS or not slot or not title:
        return None
    icon = text(item.get("icon"), 20) or "other"
    order = item.get("order")
    try:
        order_value = max(0, min(999, round(float(order))))
    except Exception:
        order_value = 0
    return {
        "id": item_id,
        "section": section,
        "slot": slot,
        "title": title,
        "icon": icon if icon in VALID_SETUP_ICONS else "other",
        "note": text(item.get("note"), 300) or None,
        "link": clean_url(item.get("link")),
        "order": order_value,
        "updatedAt": int(item.get("updatedAt") or int(time.time() * 1000)),
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
        price = min(1_000_000_000, round(price * 100) / 100) if price > 0 else None
    except Exception:
        price = None
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
        "note": text(item.get("note"), 600) or None,
        "link": clean_url(item.get("link")),
        "image": clean_url(item.get("image")),
        "addedAt": int(item.get("addedAt") or now),
        "updatedAt": int(item.get("updatedAt") or now),
    }


NORMALIZERS = {
    "watchlist": normalize_watchlist_item,
    "gamelist": normalize_gamelist_item,
    "setup": normalize_setup_item,
    "wishlist": normalize_wishlist_item,
}


def security_headers(nonce: str | None = None) -> dict[str, str]:
    script_src = "script-src 'self' https://cdn.tailwindcss.com"
    style_src = "'self' 'unsafe-inline' https://fonts.googleapis.com"
    if nonce:
        script_src += f" 'nonce-{nonce}' 'unsafe-eval'"
        style_src += f" 'nonce-{nonce}'"
    return {
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "strict-origin-when-cross-origin",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
        "Cross-Origin-Opener-Policy": "same-origin",
        "Content-Security-Policy": "; ".join([
            "default-src 'self'",
            "base-uri 'self'",
            "object-src 'none'",
            "frame-ancestors 'none'",
            "form-action 'self'",
            script_src,
            f"style-src {style_src}",
            f"style-src-elem {style_src}",
            "style-src-attr 'unsafe-inline'",
            "font-src https://fonts.gstatic.com",
            "img-src 'self' data: https:",
            "connect-src 'self' https://discord.com https://shikimori.one https://api.themoviedb.org",
        ]),
    }


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    for key, value in security_headers().items():
        response.headers.setdefault(key, value)
    return response


def make_get_list(name: str):
    def endpoint() -> JSONResponse:
        try:
            state = read_list(name)
            return j({"items": state["items"], "revision": state["revision"]}, headers={"Cache-Control": f"public, max-age={LIST_SPECS[name]['cache_ttl']}"})
        except Exception:
            return j({"error": "storage_error", "items": [], "revision": 0}, 500)
    endpoint.__name__ = f"get_{name}"
    return endpoint


def make_put_list(name: str):
    def endpoint(request: Request, body: dict[str, Any]) -> JSONResponse:
        require_admin(request)
        rate_limit(request, f"write_{name}", 60, 60)
        state = read_list(name, use_cache=False)
        try:
            revision = int(body.get("revision"))
        except Exception:
            revision = -1
        if revision < 0:
            return j({"error": "revision_required", "items": state["items"], "revision": state["revision"]}, 409)
        if revision != state["revision"]:
            return j({"error": "revision_conflict", "items": state["items"], "revision": state["revision"]}, 409)
        normalizer = NORMALIZERS[name]
        raw_items = body.get("items") if isinstance(body.get("items"), list) else []
        items = [normalizer(item) for item in raw_items]
        items = [item for item in items if item][: LIST_SPECS[name]["max_items"]]
        next_state = {"revision": state["revision"] + 1, "items": items}
        write_list(name, next_state)
        return j({"ok": True, "items": items, "revision": next_state["revision"]})
    endpoint.__name__ = f"put_{name}"
    return endpoint


for _name in LIST_SPECS:
    app.add_api_route(f"/api/{_name}", make_get_list(_name), methods=["GET"])
    app.add_api_route(f"/api/{_name}", make_put_list(_name), methods=["PUT"])


@app.get("/api/admin")
def admin_check(request: Request) -> JSONResponse:
    return j({"ok": verify_session(request.cookies.get(SESSION_COOKIE))})


@app.delete("/api/admin")
def admin_logout() -> JSONResponse:
    response = j({"ok": True})
    response.headers["Set-Cookie"] = cookie_header(SESSION_COOKIE, "", 0)
    return response


@app.post("/api/admin")
def admin_login(request: Request, body: dict[str, Any]) -> JSONResponse:
    rate_limit(request, "admin_login", 10, 60)
    if not ADMIN_PASSWORD:
        return j({"error": "admin_password_missing"}, 500)
    if not constant_time_equal(text(body.get("password"), 512), ADMIN_PASSWORD):
        return j({"error": "invalid_password"}, 401)
    response = j({"ok": True})
    response.headers["Set-Cookie"] = cookie_header(SESSION_COOKIE, create_session(), SESSION_TTL_SECONDS)
    return response


IGDB_CLIENT_ID = os.environ.get("IGDB_CLIENT_ID", TWITCH_CLIENT_ID)
IGDB_CLIENT_SECRET = os.environ.get("IGDB_CLIENT_SECRET", TWITCH_CLIENT_SECRET)
IGDB_ACCESS_TOKEN = os.environ.get("IGDB_ACCESS_TOKEN", "")


def tmdb_poster(path: Any) -> str | None:
    value = text(path, 120)
    return f"{TMDB_IMAGE_BASE}{value}" if value.startswith("/") else None


def igdb_cover(image_id: Any) -> str | None:
    value = text(image_id, 80)
    return f"https://images.igdb.com/igdb/image/upload/t_cover_big/{value}.jpg" if value else None


def search_tmdb(query: str, item_type: str) -> JSONResponse:
    if not TMDB_ACCESS_TOKEN and not TMDB_API_KEY:
        return j({"error": "tmdb_key_missing"}, 500)
    endpoint = "/3/search/tv" if item_type == "series" else "/3/search/movie"
    params = {"query": query, "language": "ru-RU", "include_adult": "false", "page": 1}
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
            results.append({
                "id": f"tmdb_{'tv' if item_type == 'series' else 'movie'}_{text(item.get('id'), 40)}",
                "title": original,
                "titleRu": localized if localized and localized != original else None,
                "year": year_from(item.get("first_air_date") if item_type == "series" else item.get("release_date")),
                "type": item_type,
                "poster": clean_url(tmdb_poster(item.get("poster_path"))),
            })
        return j({"results": results})
    except Exception:
        return j({"error": "tmdb_unavailable"}, 502)


def search_shikimori(query: str) -> JSONResponse:
    try:
        res = requests.get(
            f"{SHIKIMORI_BASE}/api/animes",
            params={"search": query, "limit": 10, "order": "popularity"},
            headers={"Accept": "application/json", "User-Agent": "krafen-watchlist/1.0"},
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
            original = image.get("original")
            results.append({
                "id": f"sh_{item_id}",
                "title": title,
                "titleRu": text(item.get("russian")) or None,
                "year": year_from(item.get("aired_on")),
                "type": "anime",
                "poster": clean_url(f"{SHIKIMORI_BASE}{original}") if original else None,
            })
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


def get_igdb_token() -> str:
    global _igdb_token
    if IGDB_ACCESS_TOKEN:
        return IGDB_ACCESS_TOKEN.removeprefix("Bearer ")
    if _igdb_token.get("access_token") and _igdb_token.get("expires_at", 0) > time.time() + 60:
        return _igdb_token["access_token"]
    if not IGDB_CLIENT_ID or not IGDB_CLIENT_SECRET:
        return ""
    res = requests.post(TWITCH_TOKEN_URL, data={
        "client_id": IGDB_CLIENT_ID,
        "client_secret": IGDB_CLIENT_SECRET,
        "grant_type": "client_credentials",
    }, timeout=15)
    data = res.json() if res.content else {}
    if not res.ok or not data.get("access_token"):
        return ""
    expires_in = int(data.get("expires_in") or 3600)
    _igdb_token = {"access_token": data["access_token"], "expires_at": time.time() + expires_in - 300}
    return data["access_token"]


def search_igdb(query: str) -> JSONResponse:
    client_id = IGDB_CLIENT_ID
    token = get_igdb_token()
    if not client_id or not token:
        return j({"error": "igdb_key_missing"}, 500)
    safe_query = text(query, 80).replace("\\", "\\\\").replace('"', '\\"')
    body = (
        'fields name,cover.image_id,genres.name,platforms.abbreviation,platforms.name,first_release_date,category; '
        f'search "{safe_query}"; where version_parent = null; limit 10;'
    )
    try:
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
            platforms = [text((p or {}).get("abbreviation") or (p or {}).get("name"), 24) for p in (item.get("platforms") or []) if isinstance(p, dict)]
            genres = [text((g or {}).get("name"), 28) for g in (item.get("genres") or []) if isinstance(g, dict)]
            cover = item.get("cover") if isinstance(item.get("cover"), dict) else {}
            raw_id = text(item.get("id"), 40)
            title = text(item.get("name"), 180)
            if not raw_id or not title:
                continue
            results.append({
                "id": f"igdb_{raw_id}",
                "igdbId": int(raw_id) if raw_id.isdigit() else None,
                "title": title,
                "year": year_from_unix(item.get("first_release_date")),
                "type": "game",
                "cover": igdb_cover(cover.get("image_id")),
                "genres": [value for value in genres if value][:3],
                "platforms": [value for value in platforms if value][:4],
                "category": int(item.get("category") or 0),
            })
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


@app.get("/api/lastfm")
def lastfm(request: Request) -> JSONResponse:
    rate_limit(request, "lastfm", 60, 60)
    if not LASTFM_API_KEY or not LASTFM_USER:
        return j({"error": "lastfm_key_missing"}, 500)
    try:
        res = requests.get(LASTFM_API_BASE, params={
            "method": "user.getrecenttracks",
            "user": LASTFM_USER,
            "api_key": LASTFM_API_KEY,
            "format": "json",
            "limit": 1,
        }, timeout=15)
        data = res.json() if res.content else {}
        if not res.ok or data.get("error"):
            return j({"error": "lastfm_unavailable"}, 502)
        recent = data.get("recenttracks") if isinstance(data.get("recenttracks"), dict) else {}
        raw_track = (recent.get("track") or [None])[0]
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
        return j({"track": track}, headers={"Cache-Control": "public, max-age=5"})
    except Exception:
        return j({"error": "lastfm_unavailable"}, 502)


def spotify_basic_auth() -> str:
    raw = f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def spotify_callback_url(request: Request) -> str:
    return str(request.url.replace(query=None))


@app.get("/api/spotify/auth")
def spotify_auth(request: Request):
    require_admin(request)
    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        return j({"error": "spotify_credentials_missing"}, 503)
    state = b64url(secrets.token_bytes(18))
    callback = spotify_callback_url(request)
    query = urlencode({
        "response_type": "code",
        "client_id": SPOTIFY_CLIENT_ID,
        "redirect_uri": callback,
        "scope": "user-read-playback-state user-read-recently-played",
        "state": state,
    })
    response = RedirectResponse(f"{SPOTIFY_AUTH_URL}?{query}", status_code=302)
    response.headers["Set-Cookie"] = cookie_header(SPOTIFY_STATE_COOKIE, state, 600, "/api/spotify/callback")
    return response


@app.get("/api/spotify/callback")
def spotify_callback(request: Request, state: str = "", code: str = "", error: str = "") -> Response:
    cookie_state = request.cookies.get(SPOTIFY_STATE_COOKIE, "")
    clear_cookie = cookie_header(SPOTIFY_STATE_COOKIE, "", 0, "/api/spotify/callback")
    if not cookie_state or not state or not constant_time_equal(cookie_state, state):
        return PlainTextResponse("Spotify authorization state check failed. Return to the site and try again.", status_code=400, headers={"Set-Cookie": clear_cookie})
    if error:
        response = RedirectResponse("/?spotify=error", status_code=303)
        response.headers["Set-Cookie"] = clear_cookie
        return response
    if not code or not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        return PlainTextResponse("Spotify credentials missing.", status_code=503, headers={"Set-Cookie": clear_cookie})
    try:
        res = requests.post(SPOTIFY_TOKEN_URL, data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": spotify_callback_url(request),
        }, headers={"Authorization": spotify_basic_auth(), "Content-Type": "application/x-www-form-urlencoded"}, timeout=15)
        data = res.json() if res.content else {}
        if not res.ok or not data.get("refresh_token"):
            return PlainTextResponse("Spotify connection failed. Check the configuration and try again.", status_code=502, headers={"Set-Cookie": clear_cookie})
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
        return PlainTextResponse("Spotify connection failed. Check the configuration and try again.", status_code=502, headers={"Set-Cookie": clear_cookie})


def get_spotify_access_token() -> str:
    tokens = read_tokens()
    cached = tokens.get("spotify_access_token") if isinstance(tokens.get("spotify_access_token"), dict) else None
    if cached and cached.get("access_token") and int(cached.get("expires_at") or 0) > time.time() + 60:
        return cached["access_token"]
    refresh_token = text(tokens.get("spotify_refresh_token"), 4000)
    if not refresh_token or not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        return ""
    res = requests.post(SPOTIFY_TOKEN_URL, data={"grant_type": "refresh_token", "refresh_token": refresh_token}, headers={
        "Authorization": spotify_basic_auth(),
        "Content-Type": "application/x-www-form-urlencoded",
    }, timeout=15)
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
    token = get_spotify_access_token()
    if not token:
        return j({"error": "spotify_not_connected"}, 503)
    try:
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
            recent_res = requests.get(f"{SPOTIFY_API_BASE}/me/player/recently-played?limit=1", headers={"Authorization": f"Bearer {token}"}, timeout=15)
            recent = recent_res.json() if recent_res.content else {}
            if not recent_res.ok:
                return j({"error": "spotify_recently_played_http_error", "status": recent_res.status_code}, 502)
            items = recent.get("items") if isinstance(recent.get("items"), list) else []
            item = items[0].get("track") if items and isinstance(items[0], dict) and isinstance(items[0].get("track"), dict) else None
        else:
            error = "spotify_reauthorization_required" if res.status_code == 401 else "spotify_playback_http_error"
            return j({"error": error, "status": res.status_code}, 502)
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
        return j({"track": track}, headers={"Cache-Control": "public, max-age=3"})
    except Exception:
        return j({"error": "spotify_failed"}, 502)


def twitch_app_token() -> str:
    global _twitch_token
    if _twitch_token.get("token") and _twitch_token.get("expires_at", 0) > time.time() + 60:
        return _twitch_token["token"]
    res = requests.post(TWITCH_TOKEN_URL, data={
        "client_id": TWITCH_CLIENT_ID,
        "client_secret": TWITCH_CLIENT_SECRET,
        "grant_type": "client_credentials",
    }, timeout=15)
    data = res.json() if res.content else {}
    if not res.ok or not data.get("access_token"):
        raise RuntimeError("twitch_token_failed")
    _twitch_token = {"token": data["access_token"], "expires_at": time.time() + int(data.get("expires_in") or 3600)}
    return data["access_token"]


def helix(path: str, **params: Any) -> list[dict[str, Any]]:
    token = twitch_app_token()
    res = requests.get(f"https://api.twitch.tv/helix{path}", params=params, headers={
        "Client-ID": TWITCH_CLIENT_ID,
        "Authorization": f"Bearer {token}",
    }, timeout=15)
    if not res.ok:
        raise RuntimeError(f"twitch_http_{res.status_code}")
    data = res.json() if res.content else {}
    return data.get("data") if isinstance(data.get("data"), list) else []


def twitch_status(login: str) -> dict[str, Any]:
    if not TWITCH_CLIENT_ID or not TWITCH_CLIENT_SECRET:
        return {"error": "twitch_key_missing"}
    try:
        users = helix("/users", login=login)
        streams = helix("/streams", user_login=login)
        user = users[0] if users else {}
        stream = streams[0] if streams else None
        return {
            "live": bool(stream),
            "login": user.get("login", login),
            "displayName": user.get("display_name", login),
            "description": user.get("description", ""),
            "profileImage": user.get("profile_image_url", ""),
            "viewCount": user.get("view_count", 0),
            "url": f"https://www.twitch.tv/{login}",
            "stream": ({
                "title": stream.get("title"),
                "gameName": stream.get("game_name"),
                "viewerCount": stream.get("viewer_count", 0),
                "startedAt": stream.get("started_at"),
            } if stream else None),
        }
    except Exception as exc:
        return {"error": f"twitch_failed: {exc}"}


@app.get("/api/twitch")
def twitch(request: Request, login: str = TWITCH_CHANNEL) -> JSONResponse:
    rate_limit(request, "twitch", 60, 60)
    value = text(login, 80) or TWITCH_CHANNEL
    return j(twitch_status(value), headers={"Cache-Control": "public, max-age=300"})


def safe_name(value: str) -> bool:
    return bool(value) and ".." not in value and not value.startswith(("/", "\\")) and re.fullmatch(r"[A-Za-z0-9_.-]+", value) is not None


def list_vods() -> list[dict[str, Any]]:
    vods: list[dict[str, Any]] = []
    if not RECORD_DIR.exists():
        return vods
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
        meta["id"] = folder.name
        meta["has_chat"] = (folder / "chat.jsonl").is_file()
        meta["has_video"] = (folder / video_file).is_file() or (folder / "video.mp4").is_file()
        vods.append(meta)
    return vods


def find_vod(vod_id: str) -> Path | None:
    if not safe_name(vod_id):
        return None
    folder = RECORD_DIR / vod_id
    return folder if folder.is_dir() else None


@app.get("/api/vods")
def api_vods() -> JSONResponse:
    return j({"vods": list_vods()})


@app.get("/api/vods/{vod_id}/chat")
def vod_chat(vod_id: str):
    folder = find_vod(vod_id)
    chat = folder / "chat.jsonl" if folder else None
    if not chat or not chat.is_file():
        return j({"error": "no chat"}, 404)
    return FileResponse(chat, media_type=MIME[".jsonl"])


def video_file_for(folder: Path, filename: str) -> Path | None:
    if not safe_name(filename):
        return None
    direct = folder / filename
    if direct.is_file():
        return direct
    fallback = folder / "video.mp4"
    return fallback if fallback.is_file() else None


@app.get("/vods/{vod_id}/{filename}")
def vod_video(vod_id: str, filename: str, request: Request) -> Response:
    folder = find_vod(vod_id)
    if not folder:
        return j({"error": "not found"}, 404)
    video = video_file_for(folder, filename or "video.mp4")
    if not video:
        return j({"error": "not found"}, 404)
    size = video.stat().st_size
    start, end = 0, size - 1
    range_header = request.headers.get("range")
    status_code = 200
    if range_header:
        try:
            raw_range = range_header.replace("bytes=", "", 1)
            start_s, _, end_s = raw_range.partition("-")
            start = int(start_s) if start_s else 0
            end = int(end_s) if end_s else size - 1
        except Exception:
            return j({"error": "bad range"}, 416, {"Content-Range": f"bytes */{size}"})
        if start >= size or end < start:
            return j({"error": "range not satisfiable"}, 416, {"Content-Range": f"bytes */{size}"})
        end = min(end, size - 1)
        status_code = 206
    length = end - start + 1

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

    headers = {"Accept-Ranges": "bytes", "Content-Length": str(length)}
    if status_code == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    return StreamingResponse(iterator(), status_code=status_code, media_type="video/mp4", headers=headers)


STATIC_ALLOWED = {".html", ".css", ".js", ".png", ".jpg", ".jpeg", ".webp", ".svg", ".ico", ".mp4", ".txt"}
STATIC_BLOCKED_ROOTS = {"data", "recorder", "functions", "node_modules", ".git", ".github", ".wrangler"}


def resolve_static(full_path: str) -> Path | None:
    rel = (full_path or "").replace("\\", "/").lstrip("/")
    if not rel:
        rel = "index.html"
    if rel.endswith("/"):
        rel += "index.html"
    if not rel or rel.startswith("/") or ".." in rel.split("/"):
        return None
    parts = rel.split("/")
    if parts[0] in STATIC_BLOCKED_ROOTS:
        return None
    path = ROOT / rel
    if path.is_dir():
        path = path / "index.html"
    if not path.suffix and path.with_name(path.name + ".html").is_file():
        path = path.with_name(path.name + ".html")
    if path.suffix.lower() not in STATIC_ALLOWED:
        return None
    if not path.is_file():
        return None
    try:
        path.relative_to(ROOT)
    except Exception:
        return None
    return path


@app.get("/{full_path:path}")
def static_file(full_path: str) -> Response:
    path = resolve_static(full_path)
    if not path:
        return j({"error": "not found"}, 404)
    if path.suffix.lower() == ".html":
        nonce = b64url(secrets.token_bytes(16))
        try:
            body = path.read_text(encoding="utf-8").replace("__CSP_NONCE__", nonce)
        except UnicodeDecodeError:
            return FileResponse(path, media_type=MIME[".html"])
        response = HTMLResponse(body)
        for key, value in security_headers(nonce).items():
            response.headers[key] = value
        return response
    media_type = MIME.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(path, media_type=media_type)


def start_recorder() -> subprocess.Popen | None:
    script = ROOT / "recorder" / "recorder.py"
    if not script.is_file():
        print("recorder.py не найден — автозапись выключена", flush=True)
        return None
    print("автозапись: запускаю recorder.py", flush=True)
    return subprocess.Popen([sys.executable, str(script)])


def main() -> None:
    if not ADMIN_PASSWORD:
        print("ВНИМАНИЕ: ADMIN_PASSWORD не задан, admin panel отключена", flush=True)
    rec_proc = start_recorder() if os.environ.get("RECORDER") == "1" else None
    print(f"krafen server → http://localhost:{PORT}", flush=True)
    print(f"статика: {ROOT}", flush=True)
    print(f"данные: {DATA_DIR}", flush=True)
    print(f"папка записей: {RECORD_DIR}", flush=True)
    try:
        uvicorn.run(app, host=HOST, port=PORT, log_level="info")
    finally:
        if rec_proc:
            rec_proc.terminate()
            print("рекордер остановлен", flush=True)


if __name__ == "__main__":
    main()

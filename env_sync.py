"""Синхронизация .env с .env.example.

Когда в проекте появляются новые параметры, они попадают в `.env.example`
(этот файл обновляется вместе с кодом), но рабочий `.env` на сервере остаётся
прежним — и новый параметр просто не действует, пока его не добавят руками.

Этот модуль дописывает в `.env` только те ключи, которых там нет:

* существующие строки никогда не изменяются и не перезаписываются;
* порядок и оформление `.env` сохраняются, новый блок добавляется в конец;
* пояснительные комментарии из `.env.example` переносятся вместе с ключом;
* закомментированные в примере параметры (`# IPINFO_TOKEN=`) добавляются
  тоже закомментированными, чтобы ничего не включилось случайно;
* пароли и токены добавляются с пустым значением — плейсхолдеры вида
  `change-me` не должны попадать в рабочий конфиг;
* перед изменением создаётся резервная копия `.env`.

Запуск вручную:

    python env_sync.py [--dry-run] [--quiet]

В сервере вызывается автоматически при старте (см. SERVER). Управляется
переменной окружения `ENV_SYNC`: `auto` (по умолчанию), `1` — включено,
`0` — выключено.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

ENV_FILE = ".env"
EXAMPLE_FILE = ".env.example"
BACKUP_SUFFIX = ".bak"

# Ключи, значения которых в примере — лишь заготовка. В рабочий .env такие
# параметры попадают с пустым значением, чтобы никто случайно не начал
# работать с плейсхолдером вместо настоящего секрета.
SECRET_KEY_RE = re.compile(r"(PASSWORD|TOKEN|SECRET|_KEY$|API_KEY)", re.IGNORECASE)

_KEY_LINE_RE = re.compile(r"^\s*(?:export\s+)?(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=")


@dataclass
class EnvEntry:
    """Параметр из .env.example вместе с его комментариями."""

    key: str
    value: str
    commented: bool
    comments: list[str] = field(default_factory=list)

    def render(self) -> list[str]:
        line = f"{self.key}={self.value}"
        if self.commented:
            line = f"# {line}"
        return [*self.comments, line]


def _read_text(path: Path) -> tuple[str, str]:
    """Читает файл, возвращая (текст, перевод строки) с поддержкой BOM."""
    raw = path.read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        content = raw.decode("utf-16", errors="ignore")
    else:
        content = raw.decode("utf-8-sig", errors="ignore")
    if "\x00" in content:
        content = content.replace("\x00", "")
    newline = "\r\n" if "\r\n" in content else "\n"
    return content, newline


def parse_example(text: str) -> list[EnvEntry]:
    """Разбирает .env.example в список параметров с их комментариями."""
    entries: list[EnvEntry] = []
    pending_comments: list[str] = []

    for raw_line in text.splitlines():
        stripped = raw_line.strip()

        if not stripped:
            # Пустая строка разрывает смысловую связку «комментарий → параметр».
            pending_comments = []
            continue

        if stripped.startswith("#"):
            body = stripped[1:].strip()
            # Закомментированный параметр — это тоже объявление, только выключенное.
            m = _KEY_LINE_RE.match(body)
            if m:
                key = m.group("key")
                _, _, value = body.partition("=")
                entries.append(
                    EnvEntry(key=key, value=value.strip(), commented=True, comments=list(pending_comments))
                )
                pending_comments = []
            else:
                pending_comments.append(stripped)
            continue

        m = _KEY_LINE_RE.match(raw_line)
        if m:
            key = m.group("key")
            _, _, value = raw_line.partition("=")
            entries.append(
                EnvEntry(key=key, value=value.strip(), commented=False, comments=list(pending_comments))
            )
        pending_comments = []

    return entries


def known_keys(text: str) -> set[str]:
    """Все ключи, встречающиеся в .env — включая закомментированные.

    Закомментированный параметр не действует, но и дубль ему не нужен:
    пользователь мог отключить его осознанно.
    """
    keys: set[str] = set()
    for raw_line in text.splitlines():
        body = raw_line.strip()
        if body.startswith("#"):
            body = body[1:].strip()
        m = _KEY_LINE_RE.match(body)
        if m:
            keys.add(m.group("key").upper())
    return keys


def _value_for_env(entry: EnvEntry) -> str:
    if entry.commented:
        return entry.value
    if SECRET_KEY_RE.search(entry.key) and entry.value:
        return ""
    return entry.value


def build_append_block(entries: list[EnvEntry], now: str | None = None) -> str:
    """Формирует текстовый блок для добавления в конец .env."""
    stamp = now or time.strftime("%Y-%m-%d")
    lines = [f"# --- параметры, добавленные из .env.example ({stamp}) ---"]
    last_comments: tuple[str, ...] = ()
    for entry in entries:
        comments = tuple(entry.comments)
        # Комментарии из примера уже могли попасть в блок выше — не дублируем.
        if comments != last_comments:
            lines.extend(comments)
            last_comments = comments
        line = f"{entry.key}={_value_for_env(entry)}"
        lines.append(f"# {line}" if entry.commented else line)
    return "\n".join(lines)


@dataclass
class SyncResult:
    ok: bool
    added: list[str] = field(default_factory=list)
    skipped_reason: str = ""
    backup_path: Path | None = None
    dry_run: bool = False

    @property
    def changed(self) -> bool:
        return bool(self.added)


def sync_env(
    root: Path,
    *,
    env_name: str = ENV_FILE,
    example_name: str = EXAMPLE_FILE,
    dry_run: bool = False,
    now: str | None = None,
) -> SyncResult:
    """Дописывает в .env отсутствующие параметры из .env.example."""
    env_path = root / env_name
    example_path = root / example_name

    if not example_path.is_file():
        return SyncResult(ok=True, skipped_reason="example_missing")
    if not env_path.is_file():
        # Рабочий .env не создаём: на сервере он может жить вне репозитория.
        return SyncResult(ok=True, skipped_reason="env_missing")

    try:
        example_text, _ = _read_text(example_path)
        env_text, env_newline = _read_text(env_path)
    except OSError:
        return SyncResult(ok=False, skipped_reason="read_error")

    present = known_keys(env_text)
    missing: list[EnvEntry] = []
    seen: set[str] = set()
    for entry in parse_example(example_text):
        key = entry.key.upper()
        if key in present or key in seen:
            continue
        seen.add(key)
        missing.append(entry)

    if not missing:
        return SyncResult(ok=True, added=[], skipped_reason="up_to_date")

    block = build_append_block(missing, now=now)
    if env_newline != "\n":
        block = block.replace("\n", env_newline)

    result = SyncResult(ok=True, added=[e.key for e in missing], dry_run=dry_run)
    if dry_run:
        return result

    # Резервная копия: ровно одна на каждое изменение.
    backup_path = env_path.with_name(f"{env_name}{BACKUP_SUFFIX}")
    try:
        shutil.copy2(env_path, backup_path)
        result.backup_path = backup_path
    except OSError:
        result.backup_path = None

    # Ровно одна пустая строка перед блоком, без лишних переводов в конце.
    base = env_text.rstrip("\r\n")
    new_text = (base + env_newline + env_newline + block if base else block) + env_newline

    tmp_path = env_path.with_name(f".{env_name}.sync.{os.getpid()}")
    try:
        tmp_path.write_text(new_text, encoding="utf-8", newline="")
        os.replace(tmp_path, env_path)
    except OSError:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        return SyncResult(ok=False, added=[], skipped_reason="write_error", backup_path=result.backup_path)

    return result


def sync_enabled() -> bool:
    """Учитывает выключатель ENV_SYNC (auto / 1 / 0)."""
    mode = (os.environ.get("ENV_SYNC", "auto") or "auto").strip().lower()
    return mode not in {"0", "false", "no", "off", "disabled"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Добавляет в .env отсутствующие параметры из .env.example")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parent), help="каталог проекта")
    parser.add_argument("--dry-run", action="store_true", help="показать, что будет добавлено, но не менять файл")
    parser.add_argument("--quiet", action="store_true", help="не печатать отчёт")
    args = parser.parse_args(argv)

    result = sync_env(Path(args.root), dry_run=args.dry_run)

    if not args.quiet:
        if not result.ok:
            print(f"[env] не удалось обновить .env: {result.skipped_reason}")
        elif result.changed:
            action = "будет добавлено" if args.dry_run else "добавлено"
            keys = ", ".join(result.added)
            print(f"[env] {action} параметров: {len(result.added)} ({keys})")
            if result.backup_path:
                print(f"[env] резервная копия: {result.backup_path.name}")
        elif result.skipped_reason == "up_to_date":
            print("[env] .env актуален")
        elif result.skipped_reason == "env_missing":
            print("[env] .env не найден — синхронизация пропущена")
        elif result.skipped_reason == "example_missing":
            print("[env] .env.example не найден — синхронизация пропущена")

    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

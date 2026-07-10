"""
ByteFlow — зрозумілі логи користувачів (локально, не в git).
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any

LOGS_DIR = Path(__file__).resolve().parent / "logs"
USERS_DIR = LOGS_DIR / "users"
DAILY_DIR = LOGS_DIR / "daily"

_file_lock = asyncio.Lock()
MAX_LINE_LEN = 500


def _ensure_dirs() -> None:
    USERS_DIR.mkdir(parents=True, exist_ok=True)
    DAILY_DIR.mkdir(parents=True, exist_ok=True)


def _today_name() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _format_user_label(
    user_id: int,
    username: str | None,
    full_name: str | None,
) -> str:
    uname = f"@{username}" if username else "без username"
    name = full_name or "—"
    return f"ID {user_id} | {uname} | {name}"


def _truncate(text: str, limit: int = MAX_LINE_LEN) -> str:
    clean = " ".join(text.replace("\r", " ").split())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1] + "…"


def _write_line(path: Path, line: str) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


async def log_user_event(
    *,
    user_id: int,
    username: str | None,
    full_name: str | None,
    event: str,
    details: str = "",
    extra: dict[str, Any] | None = None,
) -> None:
    """Записує подію в денний лог і в персональний файл користувача."""
    _ensure_dirs()
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    user_label = _format_user_label(user_id, username, full_name)
    detail_part = f" | Деталі: {_truncate(details)}" if details else ""
    extra_part = ""
    if extra:
        pairs = ", ".join(f"{k}={v}" for k, v in extra.items())
        extra_part = f" | {pairs}"

    line = f"[{ts}] {user_label} | Подія: {event}{detail_part}{extra_part}"

    daily_path = DAILY_DIR / f"{_today_name()}.log"
    user_path = USERS_DIR / f"{user_id}.log"

    async with _file_lock:
        await asyncio.to_thread(_write_line, daily_path, line)
        await asyncio.to_thread(_write_line, user_path, line)


def read_daily_log(day: str | None = None, limit: int = 40) -> str:
    _ensure_dirs()
    day = day or _today_name()
    path = DAILY_DIR / f"{day}.log"
    if not path.is_file():
        return f"Логів за {day} немає."
    lines = path.read_text(encoding="utf-8").splitlines()
    tail = lines[-limit:] if limit > 0 else lines
    header = f"📋 Логи ByteFlow за {day} (останні {len(tail)} записів)\n\n"
    return header + "\n".join(tail)


def read_user_log(user_id: int, limit: int = 40) -> str:
    _ensure_dirs()
    path = USERS_DIR / f"{user_id}.log"
    if not path.is_file():
        return f"Логів користувача {user_id} немає."
    lines = path.read_text(encoding="utf-8").splitlines()
    tail = lines[-limit:] if limit > 0 else lines
    header = f"👤 Логи користувача {user_id} (останні {len(tail)} записів)\n\n"
    return header + "\n".join(tail)


def list_active_users_today() -> list[int]:
    _ensure_dirs()
    path = DAILY_DIR / f"{_today_name()}.log"
    if not path.is_file():
        return []
    users: set[int] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if "| ID " in line:
            try:
                part = line.split("| ID ", 1)[1].split(" |", 1)[0].strip()
                users.add(int(part))
            except (ValueError, IndexError):
                continue
    return sorted(users)

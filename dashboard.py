"""
ByteFlow — статус для M5Stick / GitHub dashboard.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp

logger = logging.getLogger("byteflow.dashboard")

BASE_DIR = Path(__file__).resolve().parent
STATUS_PATH = BASE_DIR / "dashboard" / "status.json"

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
GITHUB_REPO = os.getenv("GITHUB_REPO", "").strip()
GITHUB_STATS_PATH = os.getenv("GITHUB_STATS_PATH", "dashboard/status.json").strip()
DASHBOARD_UPDATE_INTERVAL = int(os.getenv("DASHBOARD_UPDATE_INTERVAL", "30") or "30")


class BotDashboard:
    def __init__(self) -> None:
        self.started_at = datetime.now(timezone.utc)
        self.users_today: set[int] = set()
        self.messages_today = 0
        self.orders_today = 0
        self.ai_sessions_today = 0
        self.ai_messages_today = 0
        self.errors_today = 0
        self.active_ai_chats = 0
        self.active_fsm_users = 0
        self.last_errors: deque[dict[str, str]] = deque(maxlen=8)
        self.events_today: dict[str, int] = {}
        self._last_write = 0.0
        self._github_sha: str | None = None

    def touch_user(self, user_id: int) -> None:
        self.users_today.add(user_id)

    def inc(self, key: str, user_id: int | None = None) -> None:
        if user_id is not None:
            self.touch_user(user_id)
        if key == "message":
            self.messages_today += 1
        elif key == "order":
            self.orders_today += 1
        elif key == "ai_session":
            self.ai_sessions_today += 1
        elif key == "ai_message":
            self.ai_messages_today += 1
        elif key == "error":
            self.errors_today += 1
        self.events_today[key] = self.events_today.get(key, 0) + 1

    def set_active(self, *, ai_chats: int, fsm_users: int) -> None:
        self.active_ai_chats = ai_chats
        self.active_fsm_users = fsm_users

    def add_error(self, source: str, message: str) -> None:
        self.errors_today += 1
        self.last_errors.appendleft(
            {
                "time": datetime.now().strftime("%H:%M:%S"),
                "source": source[:40],
                "message": message[:180],
            }
        )

    def _system_metrics(self) -> dict[str, Any]:
        try:
            import psutil

            proc = psutil.Process()
            return {
                "cpu_percent": round(psutil.cpu_percent(interval=None), 1),
                "ram_percent": round(psutil.virtual_memory().percent, 1),
                "process_mb": round(proc.memory_info().rss / (1024 * 1024), 1),
            }
        except Exception:
            return {"cpu_percent": 0.0, "ram_percent": 0.0, "process_mb": 0.0}

    def to_dict(self) -> dict[str, Any]:
        uptime_sec = int((datetime.now(timezone.utc) - self.started_at).total_seconds())
        metrics = self._system_metrics()
        return {
            "service": "ByteFlow Bot",
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "uptime_sec": uptime_sec,
            "today": {
                "date": datetime.now().strftime("%Y-%m-%d"),
                "users": len(self.users_today),
                "messages": self.messages_today,
                "orders": self.orders_today,
                "ai_sessions": self.ai_sessions_today,
                "ai_messages": self.ai_messages_today,
                "errors": self.errors_today,
            },
            "live": {
                "active_ai_chats": self.active_ai_chats,
                "active_fsm_users": self.active_fsm_users,
            },
            "system": metrics,
            "last_errors": list(self.last_errors),
            "status": "error" if self.last_errors else "ok",
        }

    async def write_local(self) -> None:
        STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.to_dict(), ensure_ascii=False, indent=2)
        await asyncio.to_thread(STATUS_PATH.write_text, payload, "utf-8")

    async def publish_github(self) -> None:
        if not GITHUB_TOKEN or not GITHUB_REPO:
            return
        owner, repo = GITHUB_REPO.split("/", 1)
        url = f"https://api.github.com/repos/{owner}/{repo}/contents/{GITHUB_STATS_PATH}"
        headers = {
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        content = json.dumps(self.to_dict(), ensure_ascii=False, indent=2)
        body: dict[str, Any] = {
            "message": f"dashboard update {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        }
        if self._github_sha:
            body["sha"] = self._github_sha

        timeout = aiohttp.ClientTimeout(total=20)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                if not self._github_sha:
                    async with session.get(url, headers=headers) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            self._github_sha = data.get("sha")
                            if self._github_sha:
                                body["sha"] = self._github_sha
                async with session.put(url, headers=headers, json=body) as resp:
                    if resp.status in (200, 201):
                        data = await resp.json()
                        self._github_sha = (data.get("content") or {}).get("sha") or self._github_sha
                        logger.info("Dashboard опубліковано на GitHub: %s", GITHUB_STATS_PATH)
                    else:
                        text = await resp.text()
                        logger.warning("GitHub dashboard publish failed %s: %s", resp.status, text[:200])
        except Exception:
            logger.exception("Помилка публікації dashboard на GitHub")

    async def flush(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_write < DASHBOARD_UPDATE_INTERVAL:
            return
        self._last_write = now
        await self.write_local()
        await self.publish_github()


dashboard = BotDashboard()

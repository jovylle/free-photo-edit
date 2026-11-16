"""Cookieless product analytics for free-photo-edit.

Table: events(id, name, path, preset, device, day, ts) in the same SQLite
file as the credits ledger. Rows are deliberately anonymous: `device` is a
salted sha256(ip + user-agent) truncated to 16 hex chars and `day` is the
UTC date, so only aggregate counts are recoverable. No prompts, images,
job ids, emails, IPs or raw user agents are ever written.

Reads are grouped in SQL; /api/stats never returns a device, IP or row.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
import time
from typing import TypedDict

ALLOWED_EVENTS = frozenset({
    "page_view", "upload", "submit", "success", "fail",
    "correction", "share", "donate_click", "pack_view",
})
# A stable default keeps device buckets comparable across restarts; operators
# can override the salt without touching code.
EVENT_SALT = os.environ.get("EVENT_SALT", "fpe-analytics-v1")
EVENT_PATH_MAX = 200
TOP_PRESETS = 5


class DayCounts(TypedDict):
    day: str
    counts: dict[str, int]


class PresetCount(TypedDict):
    preset: str
    count: int


class Stats(TypedDict):
    days: int
    since_day: str
    funnel: dict[str, int]
    success_rate: float
    daily: list[DayCounts]
    presets: list[PresetCount]


def event_device(ip: str, user_agent: str) -> str:
    """Salted, truncated device key for rate buckets — never reversible."""
    raw = f"{EVENT_SALT}|{ip}|{user_agent}".encode()
    return hashlib.sha256(raw).hexdigest()[:16]


def clean_label(value: object, limit: int) -> str:
    """Keep a client label to path/word characters so nothing odd is stored."""
    text = str(value or "").strip()
    return "".join(ch for ch in text if ch.isalnum() or ch in "/._-")[:limit]


def _utc_day(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


class EventsStore:
    def __init__(self, path: str) -> None:
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS events("
            "id INTEGER PRIMARY KEY, name TEXT NOT NULL, "
            "path TEXT NOT NULL DEFAULT '', preset TEXT NOT NULL DEFAULT '', "
            "device TEXT NOT NULL DEFAULT '', day TEXT NOT NULL, "
            "ts REAL NOT NULL)"
        )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS events_day ON events(day, name)")
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS events_device ON events(device, ts)")
        self._db.commit()

    def record(self, name: str, path: str = "", preset: str = "",
               device: str = "", ts: float | None = None) -> None:
        when = time.time() if ts is None else ts
        with self._lock:
            self._db.execute(
                "INSERT INTO events(name, path, preset, device, day, ts)"
                " VALUES(?,?,?,?,?,?)",
                (name, path, preset, device, _utc_day(when), when))
            self._db.commit()

    def count_since(self, device: str, since_ts: float) -> int:
        """Events from one device at/after since_ts (rolling-window guard)."""
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) FROM events WHERE device=? AND ts>=?",
                (device, since_ts)).fetchone()
        return int(row[0]) if row else 0

    def stats(self, days: int = 30) -> Stats:
        """Per-day counts, top presets and the derived funnel.

        Only aggregate numbers leave this method: no device, IP or row.
        """
        days = max(1, min(365, days))
        since_day = _utc_day(time.time() - (days - 1) * 86400)
        with self._lock:
            daily_rows = self._db.execute(
                "SELECT day, name, COUNT(*) FROM events WHERE day>=?"
                " GROUP BY day, name ORDER BY day", (since_day,)).fetchall()
            preset_rows = self._db.execute(
                "SELECT preset, COUNT(*) FROM events WHERE day>=?"
                " AND preset<>'' GROUP BY preset"
                " ORDER BY COUNT(*) DESC, preset LIMIT ?",
                (since_day, TOP_PRESETS)).fetchall()
        funnel: dict[str, int] = dict.fromkeys(sorted(ALLOWED_EVENTS), 0)
        by_day: dict[str, dict[str, int]] = {}
        for day, name, count in daily_rows:
            counts = by_day.setdefault(
                str(day), dict.fromkeys(sorted(ALLOWED_EVENTS), 0))
            if str(name) in counts:
                counts[str(name)] = int(count)
                funnel[str(name)] += int(count)
        finished = funnel["success"] + funnel["fail"]
        daily: list[DayCounts] = [
            {"day": day, "counts": by_day[day]} for day in sorted(by_day)
        ]
        presets: list[PresetCount] = [
            {"preset": str(p), "count": int(c)} for p, c in preset_rows
        ]
        return {
            "days": days,
            "since_day": since_day,
            "funnel": funnel,
            "success_rate": round(funnel["success"] / finished, 4)
            if finished else 0.0,
            "daily": daily,
            "presets": presets,
        }

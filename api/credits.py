"""SQLite-backed credits ledger for free-photo-edit.

Tables: users(id, credits, created_at), jobs(id, user, status, created_at, ...).
Rule: 1 credit = 1 generation (FREE_CREDITS=3). Check-then-debit happens in
ONE SQLite transaction (BEGIN IMMEDIATE) so double-submits cannot overspend.
0-credit requests raise OutOfCredits with zero ledger writes. Engine failures
call refund() to restore the credit and mark the job failed.

Adapted from photo-fusion/api/credits.py (1-photo v1: no admin-key layer,
no auth_fails table; IP quota enforced at server layer via ip_job_count).
"""

from __future__ import annotations

import json as _json
import os
import sqlite3
import threading
import time

FREE_CREDITS = int(os.environ.get("FREE_CREDITS", "3"))


class OutOfCredits(Exception):
    """Raised when a user with 0 credits attempts a generation."""


class DailyLimitReached(Exception):
    """Raised when the global rolling daily edit budget is exhausted."""


class CreditsStore:
    def __init__(self, path: str):
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS users("
            "id TEXT PRIMARY KEY, credits INTEGER NOT NULL, created_at REAL NOT NULL)"
        )
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS jobs("
            "id TEXT PRIMARY KEY, user TEXT NOT NULL, "
            "status TEXT NOT NULL, created_at REAL NOT NULL, "
            "preset TEXT NOT NULL DEFAULT 'custom', "
            "ip TEXT NOT NULL DEFAULT '', "
            "prompt TEXT NOT NULL DEFAULT '', "
            "error TEXT NOT NULL DEFAULT '')"
        )
        for _col, _ddl in (
            ("terminal_at", "ALTER TABLE jobs ADD COLUMN terminal_at REAL"),
            ("preset", "ALTER TABLE jobs ADD COLUMN preset TEXT NOT NULL DEFAULT 'custom'"),
            ("ip", "ALTER TABLE jobs ADD COLUMN ip TEXT NOT NULL DEFAULT ''"),
            ("ua", "ALTER TABLE jobs ADD COLUMN ua TEXT NOT NULL DEFAULT ''"),
            ("prompt", "ALTER TABLE jobs ADD COLUMN prompt TEXT NOT NULL DEFAULT ''"),
            ("error", "ALTER TABLE jobs ADD COLUMN error TEXT NOT NULL DEFAULT ''"),
        ):
            try:
                self._db.execute(_ddl)
            except sqlite3.OperationalError:
                pass  # column already exists (pre-migration DB)
        # Legacy completion times are unknown: grant a full retention interval.
        self._db.execute(
            "UPDATE jobs SET terminal_at=? WHERE status IN ('done','failed')"
            " AND terminal_at IS NULL", (time.time(),))
        self._db.commit()

    def balance(self, user: str) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT credits FROM users WHERE id=?", (user,)
            ).fetchone()
            if row is None:
                self._db.execute(
                    "INSERT INTO users(id, credits, created_at) VALUES(?,?,?)",
                    (user, FREE_CREDITS, time.time()),
                )
                self._db.commit()
                return FREE_CREDITS
            return int(row[0])

    def check_and_debit(self, user: str, job_id: str,
                        preset: str = "custom", ip: str = "", ua: str = "",
                        prompt: str = "", daily_limit: int | None = None,
                        since_ts: float | None = None) -> int:
        """Atomically check balance and debit 1 credit. Returns credits left.

        Raises OutOfCredits without touching the ledger when balance is 0.
        """
        if daily_limit is not None and since_ts is None:
            raise ValueError("since_ts is required with daily_limit")
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                if daily_limit is not None:
                    count = self._db.execute(
                        "SELECT COUNT(*) FROM jobs WHERE created_at>=?",
                        (since_ts,),
                    ).fetchone()
                    if count is not None and int(count[0]) >= daily_limit:
                        raise DailyLimitReached
                row = self._db.execute(
                    "SELECT credits FROM users WHERE id=?", (user,)
                ).fetchone()
                credits = FREE_CREDITS if row is None else int(row[0])
                if credits < 1:
                    self._db.execute("ROLLBACK")
                    raise OutOfCredits(f"user {user!r} has 0 credits")
                if row is None:
                    self._db.execute(
                        "INSERT INTO users(id, credits, created_at) VALUES(?,?,?)",
                        (user, credits - 1, time.time()),
                    )
                else:
                    self._db.execute(
                        "UPDATE users SET credits=? WHERE id=?", (credits - 1, user)
                    )
                self._db.execute(
                    "INSERT INTO jobs(id, user, status, created_at,"
                    " preset, ip, ua, prompt)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (job_id, user, "running", time.time(),
                     preset, ip, ua, prompt[:2000]),
                )
                self._db.execute("COMMIT")
                return credits - 1
            except Exception:
                try:
                    self._db.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

    def complete(self, job_id: str, status: str = "done") -> None:
        with self._lock:
            self._db.execute(
                "UPDATE jobs SET status=?, terminal_at=COALESCE(terminal_at, ?) WHERE id=?",
                (status, time.time() if status in ('done', 'failed') else None, job_id))
            self._db.commit()

    def refund(self, user: str, job_id: str, error: str = "") -> int:
        """Refund 1 credit after an engine failure. Returns new balance."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute(
                    "SELECT credits FROM users WHERE id=?", (user,)
                ).fetchone()
                credits = FREE_CREDITS if row is None else int(row[0])
                if row is None:
                    self._db.execute(
                        "INSERT INTO users(id, credits, created_at) VALUES(?,?,?)",
                        (user, credits + 1, time.time()),
                    )
                else:
                    self._db.execute(
                        "UPDATE users SET credits=? WHERE id=?", (credits + 1, user)
                    )
                self._db.execute(
                    "UPDATE jobs SET status='failed', error=?,"
                    " terminal_at=COALESCE(terminal_at, ?) WHERE id=?",
                    (error[:500], time.time(), job_id),
                )
                self._db.execute("COMMIT")
                return credits + 1
            except Exception:
                try:
                    self._db.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

    def fail(self, job_id: str, error: str = "") -> None:
        """Mark a job failed WITHOUT refund."""
        with self._lock:
            self._db.execute(
                "UPDATE jobs SET status='failed', error=?,"
                " terminal_at=COALESCE(terminal_at, ?) WHERE id=?",
                (error[:500], time.time(), job_id),
            )
            self._db.commit()

    def get_job(self, job_id: str) -> dict | None:
        with self._lock:
            row = self._db.execute(
                "SELECT id, user, status, created_at,"
                " preset, ip, prompt, error, terminal_at FROM jobs WHERE id=?",
                (job_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "id": row[0],
            "user": row[1],
            "status": row[2],
            "created_at": row[3],
            "preset": row[4] if len(row) > 4 else "custom",
            "ip": row[5] if len(row) > 5 else "",
            "prompt": row[6] if len(row) > 6 else "",
            "error": row[7] if len(row) > 7 else "",
            "terminal_at": row[8],
        }

    def user_jobs(self, user: str, limit: int = 30) -> list:
        """Newest-first job rows for one user (history collection)."""
        with self._lock:
            rows = self._db.execute(
                "SELECT id, user, status, created_at,"
                " preset, prompt, terminal_at FROM jobs"
                " WHERE user=? ORDER BY created_at DESC LIMIT ?",
                (user, limit),
            ).fetchall()
        return [
            {"id": r[0], "user": r[1], "status": r[2], "created_at": r[3],
             "preset": r[4], "prompt": r[5], "terminal_at": r[6]}
            for r in rows
        ]

    def expired_job_ids(self, cutoff: float) -> list[str]:
        with self._lock:
            return [r[0] for r in self._db.execute(
                "SELECT id FROM jobs WHERE status IN ('done','failed')"
                " AND terminal_at<=?", (cutoff,)).fetchall()]

    def ip_job_count(self, ip: str) -> int:
        """Total jobs ever submitted from one client IP (quota layer)."""
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) FROM jobs WHERE ip=?", (ip,)).fetchone()
        return int(row[0]) if row else 0

    def ip_job_count_since(self, ip: str, since_ts: float,
                           ua: str = "") -> int:
        """Jobs from one IP(+UA device) at/after since_ts (rate windows).

        ua="" matches ip-only (legacy rows/tests); a UA hash scopes the
        bucket per device behind shared NAT.
        """
        with self._lock:
            if ua:
                row = self._db.execute(
                    "SELECT COUNT(*) FROM jobs WHERE ip=? AND ua=? AND created_at>=?",
                    (ip, ua, since_ts)).fetchone()
            else:
                row = self._db.execute(
                    "SELECT COUNT(*) FROM jobs WHERE ip=? AND created_at>=?",
                    (ip, since_ts)).fetchone()
        return int(row[0]) if row else 0

    def count_jobs_since(self, since_ts: float) -> int:
        """All jobs at/after since_ts (global daily spend cap)."""
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) FROM jobs WHERE created_at>=?",
                (since_ts,)).fetchone()
        return int(row[0]) if row else 0

    def set_credits(self, user: str, n: int) -> None:
        """Test/admin helper: force a user's balance."""
        with self._lock:
            if self._db.execute(
                "SELECT 1 FROM users WHERE id=?", (user,)
            ).fetchone() is None:
                self._db.execute(
                    "INSERT INTO users(id, credits, created_at) VALUES(?,?,?)",
                    (user, n, time.time()),
                )
            else:
                self._db.execute("UPDATE users SET credits=? WHERE id=?", (n, user))
            self._db.commit()

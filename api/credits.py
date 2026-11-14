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

FREE_CREDITS = int(os.environ.get("FREE_CREDITS", "3"))  # lifetime paid-equivalent seed (legacy)
FREE_DAILY = int(os.environ.get("FREE_DAILY", "5"))  # free edits per unique user per day


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
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS payments("
            "session_id TEXT PRIMARY KEY, user TEXT NOT NULL, "
            "pack TEXT NOT NULL, credits INTEGER NOT NULL, "
            "amount INTEGER NOT NULL, status TEXT NOT NULL, "
            "created_at REAL NOT NULL, paid_at REAL, "
            "email TEXT NOT NULL DEFAULT '')"
        )
        try:
            self._db.execute(
                "ALTER TABLE payments ADD COLUMN"
                " email TEXT NOT NULL DEFAULT ''")
        except sqlite3.OperationalError:
            pass  # column already exists
        try:
            self._db.execute(
                "ALTER TABLE users ADD COLUMN"
                " paid_total INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # column already exists (pre-migration DB)
        for _col, _ddl in (
            ("terminal_at", "ALTER TABLE jobs ADD COLUMN terminal_at REAL"),
            ("preset", "ALTER TABLE jobs ADD COLUMN preset TEXT NOT NULL DEFAULT 'custom'"),
            ("ip", "ALTER TABLE jobs ADD COLUMN ip TEXT NOT NULL DEFAULT ''"),
            ("ua", "ALTER TABLE jobs ADD COLUMN ua TEXT NOT NULL DEFAULT ''"),
            ("prompt", "ALTER TABLE jobs ADD COLUMN prompt TEXT NOT NULL DEFAULT ''"),
            ("error", "ALTER TABLE jobs ADD COLUMN error TEXT NOT NULL DEFAULT ''"),
            ("free_debit", "ALTER TABLE jobs ADD COLUMN free_debit INTEGER NOT NULL DEFAULT 0"),
        ):
            try:
                self._db.execute(_ddl)
            except sqlite3.OperationalError:
                pass  # column already exists (pre-migration DB)
        for _col, _ddl in (
            ("paid_credits", "ALTER TABLE users ADD COLUMN paid_credits INTEGER NOT NULL DEFAULT 0"),
            ("free_day", "ALTER TABLE users ADD COLUMN free_day TEXT"),
            ("free_used", "ALTER TABLE users ADD COLUMN free_used INTEGER NOT NULL DEFAULT 0"),
        ):
            try:
                self._db.execute(_ddl)
            except sqlite3.OperationalError:
                pass
        # Migrate legacy lifetime `credits` into paid_credits (one-time).
        try:
            self._db.execute("UPDATE users SET paid_credits = credits WHERE paid_credits = 0")
        except sqlite3.OperationalError:
            pass
        self._db.execute(
            "UPDATE jobs SET terminal_at=? WHERE status IN ('done','failed')"
            " AND terminal_at IS NULL", (time.time(),))
        self._db.commit()

    def _today(self) -> str:
        return time.strftime("%Y-%m-%d", time.gmtime())

    def balance(self, user: str) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT paid_credits, free_day, free_used FROM users WHERE id=?", (user,)
            ).fetchone()
            if row is None:
                self._db.execute(
                    "INSERT INTO users(id, credits, paid_credits, free_day, free_used, created_at)"
                    " VALUES(?,?,?,?,?,?)",
                    (user, FREE_DAILY, 0, self._today(), 0, time.time()),
                )
                self._db.commit()
                return FREE_DAILY
            paid, free_day, free_used = row
            free_remaining = FREE_DAILY if free_day != self._today() else max(0, FREE_DAILY - int(free_used or 0))
            return int(free_remaining) + int(paid or 0)

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
                today = self._today()
                row = self._db.execute(
                    "SELECT paid_credits, free_day, free_used FROM users WHERE id=?", (user,)
                ).fetchone()
                if row is None:
                    paid, free_day, free_used = 0, None, 0
                    self._db.execute(
                        "INSERT INTO users(id, credits, paid_credits, free_day, free_used, created_at)"
                        " VALUES(?,?,?,?,?,?)",
                        (user, 0, 0, today, 0, time.time()),
                    )
                else:
                    paid, free_day, free_used = row
                    paid = int(paid or 0)
                    free_used = int(free_used or 0)
                free_remaining = FREE_DAILY if free_day != today else max(0, FREE_DAILY - free_used)
                if free_remaining > 0:
                    new_free_used = free_used + 1 if free_day == today else 1
                    self._db.execute(
                        "UPDATE users SET free_day=?, free_used=? WHERE id=?",
                        (today, new_free_used, user),
                    )
                    free_debit = 1
                    new_paid = paid
                elif paid > 0:
                    self._db.execute(
                        "UPDATE users SET paid_credits=? WHERE id=?", (paid - 1, user)
                    )
                    free_debit = 0
                    new_paid = paid - 1
                else:
                    self._db.execute("ROLLBACK")
                    raise OutOfCredits(
                        "You've used today's free edits — they reset at midnight. "
                        "Buy more or come back tomorrow.")
                self._db.execute(
                    "INSERT INTO jobs(id, user, status, created_at,"
                    " preset, ip, ua, prompt, free_debit)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (job_id, user, "running", time.time(),
                     preset, ip, ua, prompt[:2000], free_debit),
                )
                self._db.execute("COMMIT")
                if free_debit:
                    free_after = max(0, free_remaining - 1)
                else:
                    free_after = free_remaining if free_day != today else max(0, FREE_DAILY - free_used)
                paid_after = new_paid
                return free_after + paid_after
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
                job = self._db.execute(
                    "SELECT free_debit FROM jobs WHERE id=?", (job_id,)
                ).fetchone()
                free_debit = int(job[0]) if job else 0
                row = self._db.execute(
                    "SELECT paid_credits, free_day, free_used FROM users WHERE id=?", (user,)
                ).fetchone()
                if row is None:
                    self._db.execute(
                        "INSERT INTO users(id, credits, paid_credits, free_day, free_used, created_at)"
                        " VALUES(?,?,?,?,?,?)",
                        (user, 0, 0, self._today(), 0, time.time()),
                    )
                    paid, free_day, free_used = 0, self._today(), 0
                else:
                    paid, free_day, free_used = row
                    paid = int(paid or 0)
                    free_used = int(free_used or 0)
                if free_debit:
                    new_free_used = max(0, free_used - 1)
                    self._db.execute(
                        "UPDATE users SET free_used=? WHERE id=?", (new_free_used, user)
                    )
                else:
                    self._db.execute(
                        "UPDATE users SET paid_credits=? WHERE id=?", (paid + 1, user)
                    )
                self._db.execute(
                    "UPDATE jobs SET status='failed', error=?,"
                    " terminal_at=COALESCE(terminal_at, ?) WHERE id=?",
                    (error[:500], time.time(), job_id),
                )
                self._db.execute("COMMIT")
                if free_debit:
                    return max(0, FREE_DAILY - new_free_used) + paid
                return (FREE_DAILY - (0 if free_day != self._today() else free_used)) + paid + 1
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

    def paid_total(self, user: str) -> int:
        """Edits this user has ever purchased (0 never paid)."""
        with self._lock:
            row = self._db.execute(
                "SELECT paid_total FROM users WHERE id=?", (user,)
            ).fetchone()
        return int(row[0]) if row else 0

    def create_payment(self, session_id: str, user: str, pack: str,
                       credits: int, amount: int, email: str = "") -> None:
        """Record a pending Xendit checkout session (server-side price).

        email is the buyer-supplied recovery address; it is never used to
        grant credits, only to find a stranded balance at support time.
        """
        with self._lock:
            self._db.execute(
                "INSERT INTO payments(session_id, user, pack, credits, amount,"
                " status, created_at, email) VALUES(?,?,?,?,?,'pending',?,?)",
                (session_id, user, pack, credits, amount, time.time(), email),
            )
            self._db.commit()

    def payment(self, session_id: str) -> dict[str, object] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT session_id, user, pack, credits, amount, status,"
                " created_at, paid_at, email FROM payments WHERE session_id=?",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        return {"session_id": row[0], "user": row[1], "pack": row[2],
                "credits": row[3], "amount": row[4], "status": row[5],
                "created_at": row[6], "paid_at": row[7], "email": row[8]}

    def mark_paid(self, session_id: str, paid_amount: int) -> int:
        """Atomically mark a checkout paid and add its edits to the user.

        Returns the credited amount, or 0 when already paid (duplicate
        webhook) or when paid_amount is below the recorded price.
        Raises KeyError for an unknown session.
        """
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute(
                    "SELECT user, credits, amount, status FROM payments"
                    " WHERE session_id=?", (session_id,),
                ).fetchone()
                if row is None:
                    self._db.execute("ROLLBACK")
                    raise KeyError(session_id)
                user, credits, amount, status = (
                    row[0], int(row[1]), int(row[2]), row[3])
                cur = None
                if status == "pending" and paid_amount >= amount:
                    cur = self._db.execute(
                        "UPDATE payments SET status='paid', paid_at=?"
                        " WHERE session_id=? AND status='pending'",
                        (time.time(), session_id),
                    )
                if cur is None or cur.rowcount != 1:
                    self._db.execute("ROLLBACK")
                    return 0
                urow = self._db.execute(
                    "SELECT 1 FROM users WHERE id=?", (user,)
                ).fetchone()
                if urow is None:
                    self._db.execute(
                        "INSERT INTO users(id, credits, paid_credits, created_at, paid_total, free_day, free_used)"
                        " VALUES(?,?,?,?,?,?,?)",
                        (user, 0, credits, time.time(), credits, None, 0),
                    )
                else:
                    self._db.execute(
                        "UPDATE users SET paid_credits=paid_credits+?,"
                        " paid_total=paid_total+? WHERE id=?",
                        (credits, credits, user),
                    )
                self._db.execute("COMMIT")
                return credits
            except KeyError:
                raise
            except Exception:
                try:
                    self._db.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

    def stats(self) -> dict[str, int]:
        """Aggregate counts for /metrics; one lock, no per-user data."""
        with self._lock:
            rows = self._db.execute(
                "SELECT status, COUNT(*) FROM jobs GROUP BY status"
            ).fetchall()
            users = self._db.execute(
                "SELECT COUNT(*), COALESCE(SUM(paid_credits), 0) FROM users"
            ).fetchone()
        out: dict[str, int] = {
            "users": int(users[0]),
            "credits": int(users[1]),
        }
        for status, count in rows:
            out[f"jobs_{status}"] = int(count)
        return out

    def set_credits(self, user: str, n: int) -> None:
        """Test/admin helper: force a user's balance."""
        with self._lock:
            if self._db.execute(
                "SELECT 1 FROM users WHERE id=?", (user,)
            ).fetchone() is None:
                self._db.execute(
                    "INSERT INTO users(id, credits, paid_credits, created_at, free_day, free_used)"
                    " VALUES(?,?,?,?,?,?)",
                    (user, 0, n, time.time(),
                     self._today() if n > 0 else self._today(),
                     0 if n > 0 else FREE_DAILY),
                )
            else:
                if n == 0:
                    self._db.execute(
                        "UPDATE users SET paid_credits=0, free_day=?, free_used=? WHERE id=?",
                        (self._today(), FREE_DAILY, user),
                    )
                else:
                    self._db.execute("UPDATE users SET paid_credits=? WHERE id=?", (n, user))
            self._db.commit()

"""SQLite persistence: users, bans, download history."""

import asyncio
import logging
import sqlite3
import threading
import time

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id    INTEGER PRIMARY KEY,
    username   TEXT,
    first_name TEXT,
    first_seen INTEGER NOT NULL,
    banned     INTEGER NOT NULL DEFAULT 0,
    credit     INTEGER NOT NULL DEFAULT 0,
    free_credit   INTEGER NOT NULL DEFAULT 0,
    free_reset_at INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS downloads (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(user_id),
    platform   TEXT NOT NULL,
    url        TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_downloads_user_time
    ON downloads(user_id, created_at);
CREATE TABLE IF NOT EXISTS transactions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    credits    INTEGER NOT NULL,
    stars      INTEGER NOT NULL,
    charge_id  TEXT NOT NULL UNIQUE,
    refunded   INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
"""


class Database:
    """Thread-safe SQLite wrapper; public methods are async (run in a thread)."""

    def __init__(self, path: str, free_credits: int = 5, free_period: int = 7 * 86400) -> None:
        self.free_credits = free_credits
        self.free_period = free_period
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
            # Migration for databases created before the credit column existed
            cols = [r["name"] for r in self._conn.execute("PRAGMA table_info(users)")]
            for col, ddl in [
                ("credit", "ALTER TABLE users ADD COLUMN credit INTEGER NOT NULL DEFAULT 0"),
                ("free_credit", "ALTER TABLE users ADD COLUMN free_credit INTEGER NOT NULL DEFAULT 0"),
                ("free_reset_at", "ALTER TABLE users ADD COLUMN free_reset_at INTEGER NOT NULL DEFAULT 0"),
            ]:
                if col not in cols:
                    self._conn.execute(ddl)
            self._conn.commit()
        log.info("Database ready at %s", path)

    def close(self) -> None:
        self._conn.close()

    # --- internal sync helpers -------------------------------------------

    def _write(self, sql: str, params: tuple = ()) -> None:
        with self._lock:
            self._conn.execute(sql, params)
            self._conn.commit()

    def _fetchone(self, sql: str, params: tuple = ()):
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def _fetchall(self, sql: str, params: tuple = ()):
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # --- users -----------------------------------------------------------

    def _upsert_user_sync(
        self, user_id: int, username: str | None, first_name: str | None
    ) -> bool:
        now = int(time.time())
        with self._lock:
            exists = self._conn.execute(
                "SELECT 1 FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
            self._conn.execute(
                """INSERT INTO users
                   (user_id, username, first_name, first_seen, credit,
                    free_credit, free_reset_at)
                   VALUES (?, ?, ?, ?, 0, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET
                       username = excluded.username,
                       first_name = excluded.first_name""",
                (user_id, username, first_name, now, self.free_credits, now),
            )
            self._conn.commit()
            return exists is None

    async def upsert_user(
        self, user_id: int, username: str | None, first_name: str | None
    ) -> bool:
        """Insert or refresh a user. New users get the free allowance.
        Returns True if this is a NEW user."""
        return await asyncio.to_thread(
            self._upsert_user_sync, user_id, username, first_name
        )

    async def is_banned(self, user_id: int) -> bool:
        row = await asyncio.to_thread(
            self._fetchone, "SELECT banned FROM users WHERE user_id = ?", (user_id,)
        )
        return bool(row and row["banned"])

    async def set_banned(self, user_id: int, banned: bool) -> bool:
        """Returns False if the user is unknown."""
        row = await asyncio.to_thread(
            self._fetchone, "SELECT 1 FROM users WHERE user_id = ?", (user_id,)
        )
        if not row:
            return False
        await asyncio.to_thread(
            self._write, "UPDATE users SET banned = ? WHERE user_id = ?",
            (int(banned), user_id),
        )
        return True

    async def all_user_ids(self, include_banned: bool = False) -> list[int]:
        sql = "SELECT user_id FROM users"
        if not include_banned:
            sql += " WHERE banned = 0"
        rows = await asyncio.to_thread(self._fetchall, sql)
        return [r["user_id"] for r in rows]

    def _refresh_free_locked(self, user_id: int, now: int) -> None:
        """Re-grant the free allowance if the period elapsed. Caller holds the lock."""
        row = self._conn.execute(
            "SELECT free_reset_at FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        if row and now >= row["free_reset_at"] + self.free_period:
            self._conn.execute(
                "UPDATE users SET free_credit = ?, free_reset_at = ? WHERE user_id = ?",
                (self.free_credits, now, user_id),
            )
            self._conn.commit()

    def _balances_sync(self, user_id: int) -> dict:
        now = int(time.time())
        with self._lock:
            self._refresh_free_locked(user_id, now)
            row = self._conn.execute(
                "SELECT credit, free_credit, free_reset_at FROM users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if not row:
                return {"free": 0, "paid": 0, "total": 0, "reset_in": 0}
            reset_in = max(0, row["free_reset_at"] + self.free_period - now)
            return {
                "free": row["free_credit"],
                "paid": row["credit"],
                "total": row["free_credit"] + row["credit"],
                "reset_in": reset_in,
            }

    async def get_balances(self, user_id: int) -> dict:
        """Free/paid/total balances, refreshing the weekly free allowance first."""
        return await asyncio.to_thread(self._balances_sync, user_id)

    def _add_credit_sync(self, user_id: int, amount: int) -> int | None:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE users SET credit = MAX(0, credit + ?) WHERE user_id = ?",
                (amount, user_id),
            )
            if cur.rowcount == 0:
                return None
            self._conn.commit()
            return self._conn.execute(
                "SELECT credit FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()["credit"]

    async def add_credit(self, user_id: int, amount: int) -> int | None:
        """Add (or with negative amount, remove) credit. Floors at 0.
        Returns the new balance, or None if the user is unknown."""
        return await asyncio.to_thread(self._add_credit_sync, user_id, amount)

    def _consume_credit_sync(self, user_id: int, amount: int) -> dict | None:
        """Spend `amount`, free credits first, then paid. Atomic under the lock.
        Returns remaining balances, or None if insufficient."""
        now = int(time.time())
        with self._lock:
            self._refresh_free_locked(user_id, now)
            row = self._conn.execute(
                "SELECT credit, free_credit FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
            if not row or row["free_credit"] + row["credit"] < amount:
                return None
            from_free = min(row["free_credit"], amount)
            from_paid = amount - from_free
            self._conn.execute(
                "UPDATE users SET free_credit = free_credit - ?, credit = credit - ? "
                "WHERE user_id = ?",
                (from_free, from_paid, user_id),
            )
            self._conn.commit()
            return {
                "free": row["free_credit"] - from_free,
                "paid": row["credit"] - from_paid,
                "total": row["free_credit"] + row["credit"] - amount,
            }

    async def consume_credit(self, user_id: int, amount: int = 1) -> dict | None:
        return await asyncio.to_thread(self._consume_credit_sync, user_id, amount)

    async def all_users(self) -> list[sqlite3.Row]:
        return await asyncio.to_thread(
            self._fetchall,
            "SELECT user_id, username, first_name, banned, credit, free_credit FROM users ORDER BY first_seen",
        )

    # --- downloads -------------------------------------------------------

    async def record_download(self, user_id: int, platform: str, url: str) -> None:
        await asyncio.to_thread(
            self._write,
            "INSERT INTO downloads (user_id, platform, url, created_at) VALUES (?, ?, ?, ?)",
            (user_id, platform, url, int(time.time())),
        )

    async def download_count_since(self, user_id: int, since_ts: int) -> int:
        row = await asyncio.to_thread(
            self._fetchone,
            "SELECT COUNT(*) AS n FROM downloads WHERE user_id = ? AND created_at >= ?",
            (user_id, since_ts),
        )
        return row["n"]

    async def stats(self) -> dict:
        day_ago = int(time.time()) - 86400
        users = (await asyncio.to_thread(
            self._fetchone, "SELECT COUNT(*) AS n FROM users"))["n"]
        banned = (await asyncio.to_thread(
            self._fetchone, "SELECT COUNT(*) AS n FROM users WHERE banned = 1"))["n"]
        downloads = (await asyncio.to_thread(
            self._fetchone, "SELECT COUNT(*) AS n FROM downloads"))["n"]
        downloads_24h = (await asyncio.to_thread(
            self._fetchone,
            "SELECT COUNT(*) AS n FROM downloads WHERE created_at >= ?", (day_ago,)))["n"]
        return {
            "users": users,
            "banned": banned,
            "downloads_total": downloads,
            "downloads_24h": downloads_24h,
        }

    # --- transactions ----------------------------------------------------

    async def record_transaction(
        self, user_id: int, credits: int, stars: int, charge_id: str
    ) -> None:
        await asyncio.to_thread(
            self._write,
            """INSERT OR IGNORE INTO transactions
               (user_id, credits, stars, charge_id, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (user_id, credits, stars, charge_id, int(time.time())),
        )

    async def get_transaction(self, charge_id: str):
        return await asyncio.to_thread(
            self._fetchone,
            "SELECT * FROM transactions WHERE charge_id = ?",
            (charge_id,),
        )

    async def mark_refunded(self, charge_id: str) -> None:
        await asyncio.to_thread(
            self._write,
            "UPDATE transactions SET refunded = 1 WHERE charge_id = ?",
            (charge_id,),
        )

    async def revenue_stats(self) -> dict:
        row = await asyncio.to_thread(
            self._fetchone,
            """SELECT COUNT(*) AS n, COALESCE(SUM(stars), 0) AS stars
               FROM transactions WHERE refunded = 0""",
        )
        return {"payments": row["n"], "stars": row["stars"]}

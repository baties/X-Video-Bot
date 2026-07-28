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
    credit     INTEGER NOT NULL DEFAULT 0
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
"""


class Database:
    """Thread-safe SQLite wrapper; public methods are async (run in a thread)."""

    def __init__(self, path: str) -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
            # Migration for databases created before the credit column existed
            cols = [r["name"] for r in self._conn.execute("PRAGMA table_info(users)")]
            if "credit" not in cols:
                self._conn.execute(
                    "ALTER TABLE users ADD COLUMN credit INTEGER NOT NULL DEFAULT 0"
                )
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
        self, user_id: int, username: str | None, first_name: str | None, initial_credit: int
    ) -> bool:
        with self._lock:
            exists = self._conn.execute(
                "SELECT 1 FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
            self._conn.execute(
                """INSERT INTO users (user_id, username, first_name, first_seen, credit)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET
                       username = excluded.username,
                       first_name = excluded.first_name""",
                (user_id, username, first_name, int(time.time()), initial_credit),
            )
            self._conn.commit()
            return exists is None

    async def upsert_user(
        self,
        user_id: int,
        username: str | None,
        first_name: str | None,
        initial_credit: int = 0,
    ) -> bool:
        """Insert or refresh a user. New users start with initial_credit.
        Returns True if this is a NEW user."""
        return await asyncio.to_thread(
            self._upsert_user_sync, user_id, username, first_name, initial_credit
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

    async def get_credit(self, user_id: int) -> int:
        row = await asyncio.to_thread(
            self._fetchone, "SELECT credit FROM users WHERE user_id = ?", (user_id,)
        )
        return row["credit"] if row else 0

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

    def _consume_credit_sync(self, user_id: int) -> int | None:
        """Atomically spend 1 credit. Returns new balance, None if none left."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE users SET credit = credit - 1 WHERE user_id = ? AND credit > 0",
                (user_id,),
            )
            if cur.rowcount == 0:
                return None
            self._conn.commit()
            return self._conn.execute(
                "SELECT credit FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()["credit"]

    async def consume_credit(self, user_id: int) -> int | None:
        return await asyncio.to_thread(self._consume_credit_sync, user_id)

    async def all_users(self) -> list[sqlite3.Row]:
        return await asyncio.to_thread(
            self._fetchall,
            "SELECT user_id, username, first_name, banned, credit FROM users ORDER BY first_seen",
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

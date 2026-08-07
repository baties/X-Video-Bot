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
    free_reset_at INTEGER NOT NULL DEFAULT 0,
    vip           INTEGER NOT NULL DEFAULT 0,
    vip_until     INTEGER NOT NULL DEFAULT 0,
    broadcast_on  INTEGER NOT NULL DEFAULT 1
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
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tickets (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    status     TEXT NOT NULL DEFAULT 'open',
    created_at INTEGER NOT NULL,
    closed_at  INTEGER
);
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
                ("vip", "ALTER TABLE users ADD COLUMN vip INTEGER NOT NULL DEFAULT 0"),
                ("vip_until", "ALTER TABLE users ADD COLUMN vip_until INTEGER NOT NULL DEFAULT 0"),
                ("broadcast_on", "ALTER TABLE users ADD COLUMN broadcast_on INTEGER NOT NULL DEFAULT 1"),
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
        """All users, with free_credit shown as its EFFECTIVE value (i.e. what
        the lazy refill would grant on the user's next interaction)."""
        now = int(time.time())
        return await asyncio.to_thread(
            self._fetchall,
            """SELECT user_id, username, first_name, banned, credit,
                      CASE WHEN ? >= free_reset_at + ?
                           THEN ? ELSE free_credit END AS free_credit,
                      vip, vip_until
               FROM users ORDER BY first_seen""",
            (now, self.free_period, self.free_credits),
        )

    async def vip_count(self) -> int:
        now = int(time.time())
        row = await asyncio.to_thread(
            self._fetchone,
            "SELECT COUNT(*) AS n FROM users WHERE vip = 1 AND (vip_until = 0 OR vip_until > ?)",
            (now,),
        )
        return row["n"]

    # --- VIP -------------------------------------------------------------

    def _is_vip_sync(self, user_id: int) -> bool:
        now = int(time.time())
        with self._lock:
            row = self._conn.execute(
                "SELECT vip, vip_until FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
            if not row or not row["vip"]:
                return False
            if row["vip_until"] and now > row["vip_until"]:  # expired -> lazily clear
                self._conn.execute(
                    "UPDATE users SET vip = 0, vip_until = 0 WHERE user_id = ?",
                    (user_id,),
                )
                self._conn.commit()
                return False
            return True

    async def is_vip(self, user_id: int) -> bool:
        """VIP status with lazy expiry (vip_until = 0 means permanent)."""
        return await asyncio.to_thread(self._is_vip_sync, user_id)

    async def vip_until(self, user_id: int) -> int:
        row = await asyncio.to_thread(
            self._fetchone, "SELECT vip_until FROM users WHERE user_id = ?", (user_id,)
        )
        return row["vip_until"] if row else 0

    async def set_vip(self, user_id: int, vip: bool, until: int = 0) -> bool:
        """Grant/revoke VIP. until=0 means permanent. False if user unknown."""
        row = await asyncio.to_thread(
            self._fetchone, "SELECT 1 FROM users WHERE user_id = ?", (user_id,)
        )
        if not row:
            return False
        await asyncio.to_thread(
            self._write,
            "UPDATE users SET vip = ?, vip_until = ? WHERE user_id = ?",
            (int(vip), until if vip else 0, user_id),
        )
        return True

    def _extend_vip_sync(self, user_id: int, seconds: int) -> int | None:
        now = int(time.time())
        with self._lock:
            row = self._conn.execute(
                "SELECT vip, vip_until FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
            if not row:
                return None
            if row["vip"] and row["vip_until"] == 0:
                return 0  # already permanent — nothing to extend
            base = max(now, row["vip_until"]) if row["vip"] else now
            until = base + seconds
            self._conn.execute(
                "UPDATE users SET vip = 1, vip_until = ? WHERE user_id = ?",
                (until, user_id),
            )
            self._conn.commit()
            return until

    async def extend_vip(self, user_id: int, seconds: int) -> int | None:
        """Add paid VIP time (stacks on remaining time). Returns new expiry ts,
        0 if the user is permanent VIP, None if unknown."""
        return await asyncio.to_thread(self._extend_vip_sync, user_id, seconds)

    async def find_user_by_username(self, username: str):
        return await asyncio.to_thread(
            self._fetchone,
            "SELECT * FROM users WHERE LOWER(username) = LOWER(?)",
            (username.lstrip("@"),),
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

    # --- settings (key/value) --------------------------------------------

    async def get_setting(self, key: str, default: str = "") -> str:
        row = await asyncio.to_thread(
            self._fetchone, "SELECT value FROM settings WHERE key = ?", (key,)
        )
        return row["value"] if row else default

    async def set_setting(self, key: str, value: str) -> None:
        await asyncio.to_thread(
            self._write,
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    # --- broadcast opt-in/out --------------------------------------------

    async def get_broadcast_on(self, user_id: int) -> bool:
        row = await asyncio.to_thread(
            self._fetchone,
            "SELECT broadcast_on FROM users WHERE user_id = ?", (user_id,),
        )
        return bool(row and row["broadcast_on"])

    async def set_broadcast_on(self, user_id: int, on: bool) -> None:
        await asyncio.to_thread(
            self._write,
            "UPDATE users SET broadcast_on = ? WHERE user_id = ?",
            (int(on), user_id),
        )

    async def broadcast_user_ids(self) -> list[int]:
        rows = await asyncio.to_thread(
            self._fetchall,
            "SELECT user_id FROM users WHERE banned = 0 AND broadcast_on = 1",
        )
        return [r["user_id"] for r in rows]

    # --- support tickets --------------------------------------------------

    async def get_open_ticket(self, user_id: int):
        return await asyncio.to_thread(
            self._fetchone,
            "SELECT * FROM tickets WHERE user_id = ? AND status = 'open'",
            (user_id,),
        )

    async def open_ticket(self, user_id: int) -> int:
        """Return the id of the user's open ticket, creating one if needed."""
        existing = await self.get_open_ticket(user_id)
        if existing:
            return existing["id"]

        def _create() -> int:
            with self._lock:
                cur = self._conn.execute(
                    "INSERT INTO tickets (user_id, created_at) VALUES (?, ?)",
                    (user_id, int(time.time())),
                )
                self._conn.commit()
                return cur.lastrowid

        return await asyncio.to_thread(_create)

    async def close_ticket(self, user_id: int) -> bool:
        ticket = await self.get_open_ticket(user_id)
        if not ticket:
            return False
        await asyncio.to_thread(
            self._write,
            "UPDATE tickets SET status = 'closed', closed_at = ? WHERE id = ?",
            (int(time.time()), ticket["id"]),
        )
        return True

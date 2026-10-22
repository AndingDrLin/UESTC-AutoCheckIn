"""SQLite persistence layer.

Every public method opens its own short-lived connection.  SQLite is configured
with WAL + a busy timeout, which is plenty for a handful of users and a single
scheduler thread.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .uestc.dorms import SEED_BUILDINGS, sort_key

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL,
    student_id      TEXT    NOT NULL UNIQUE,
    password_enc    TEXT    NOT NULL,
    fingerprint     TEXT    NOT NULL DEFAULT '',
    dorm_building   TEXT    NOT NULL DEFAULT '',
    dorm_room       TEXT    NOT NULL DEFAULT '',
    address         TEXT    NOT NULL DEFAULT '',
    latitude        REAL    NOT NULL,
    longitude       REAL    NOT NULL,
    window_start    TEXT    NOT NULL,
    window_end      TEXT    NOT NULL,
    notify_url      TEXT    NOT NULL DEFAULT '',
    notes           TEXT    NOT NULL DEFAULT '',
    enabled         INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS plans (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id           INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    plan_date         TEXT    NOT NULL,
    planned_epoch     INTEGER NOT NULL,
    planned_time      TEXT    NOT NULL,
    status            TEXT    NOT NULL DEFAULT 'pending',
    attempts          INTEGER NOT NULL DEFAULT 0,
    next_attempt_epoch INTEGER,
    last_error        TEXT    NOT NULL DEFAULT '',
    result            TEXT    NOT NULL DEFAULT '',
    created_at        TEXT    NOT NULL,
    updated_at        TEXT    NOT NULL,
    UNIQUE(user_id, plan_date)
);
CREATE INDEX IF NOT EXISTS idx_plans_date   ON plans(plan_date);
CREATE INDEX IF NOT EXISTS idx_plans_status ON plans(status);
CREATE INDEX IF NOT EXISTS idx_plans_epoch  ON plans(planned_epoch);

CREATE TABLE IF NOT EXISTS logs (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id   INTEGER,
    user_name TEXT    NOT NULL DEFAULT '',
    ts        TEXT    NOT NULL,
    level     TEXT    NOT NULL DEFAULT 'INFO',
    event     TEXT    NOT NULL DEFAULT '',
    message   TEXT    NOT NULL,
    detail    TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs(ts);

CREATE TABLE IF NOT EXISTS sessions (
    user_id          INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    cookies_enc      TEXT    NOT NULL,
    saved_at         TEXT    NOT NULL,
    last_login_epoch INTEGER,
    last_warmup_epoch INTEGER,
    invalid          INTEGER NOT NULL DEFAULT 0,
    logins_date      TEXT    NOT NULL DEFAULT '',
    logins_today     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS buildings (
    name       TEXT PRIMARY KEY,
    latitude   REAL NOT NULL,
    longitude  REAL NOT NULL,
    source     TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
"""


class DuplicateStudentId(Exception):
    pass


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _row(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.RLock()
        self.init()

    # -- connection helpers -------------------------------------------------
    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 15000")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init(self) -> None:
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(SCHEMA)
            self._seed_buildings(conn)

    @staticmethod
    def _seed_buildings(conn: sqlite3.Connection) -> None:
        """只在表为空时写入默认楼栋，管理员删掉的默认项不会被重新塞回来。"""
        if conn.execute("SELECT COUNT(*) FROM buildings").fetchone()[0]:
            return
        now = utc_now_iso()
        conn.executemany(
            "INSERT OR IGNORE INTO buildings"
            " (name, latitude, longitude, source, updated_at) VALUES (?, ?, ?, ?, ?)",
            [(*row, now) for row in SEED_BUILDINGS],
        )

    # -- users --------------------------------------------------------------
    _USER_FIELDS = {
        "name",
        "student_id",
        "password_enc",
        "fingerprint",
        "dorm_building",
        "dorm_room",
        "address",
        "latitude",
        "longitude",
        "window_start",
        "window_end",
        "notify_url",
        "notes",
        "enabled",
    }

    def create_user(self, **fields: Any) -> int:
        now = utc_now_iso()
        values = {
            "name": "",
            "student_id": "",
            "password_enc": "",
            "fingerprint": "",
            "dorm_building": "",
            "dorm_room": "",
            "address": "",
            "latitude": 0.0,
            "longitude": 0.0,
            "window_start": "21:00",
            "window_end": "23:00",
            "notify_url": "",
            "notes": "",
            "enabled": 1,
        }
        unknown = set(fields) - self._USER_FIELDS
        if unknown:
            raise ValueError(f"unknown user fields: {sorted(unknown)}")
        values.update(fields)
        columns = ", ".join(values.keys()) + ", created_at, updated_at"
        placeholders = ", ".join(["?"] * len(values)) + ", ?, ?"
        try:
            with self._write_lock, self.connect() as conn:
                cur = conn.execute(
                    f"INSERT INTO users ({columns}) VALUES ({placeholders})",
                    [*values.values(), now, now],
                )
                return int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            if "student_id" in str(exc):
                raise DuplicateStudentId(
                    f"学号 {values['student_id']} 已存在"
                ) from exc
            raise

    def update_user(self, user_id: int, **fields: Any) -> None:
        unknown = set(fields) - self._USER_FIELDS
        if unknown:
            raise ValueError(f"unknown user fields: {sorted(unknown)}")
        if not fields:
            return
        fields["updated_at"] = utc_now_iso()
        assignments = ", ".join(f"{key} = ?" for key in fields)
        with self._write_lock, self.connect() as conn:
            conn.execute(
                f"UPDATE users SET {assignments} WHERE id = ?",
                [*fields.values(), user_id],
            )

    def delete_user(self, user_id: int) -> None:
        with self._write_lock, self.connect() as conn:
            conn.execute("DELETE FROM users WHERE id = ?", (user_id,))

    def get_user(self, user_id: int) -> dict | None:
        with self.connect() as conn:
            return _row(
                conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            )

    def get_user_by_student_id(self, student_id: str) -> dict | None:
        with self.connect() as conn:
            return _row(
                conn.execute(
                    "SELECT * FROM users WHERE student_id = ?", (student_id,)
                ).fetchone()
            )

    def list_users(self, enabled_only: bool = False) -> list[dict]:
        sql = "SELECT * FROM users"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY id"
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(sql).fetchall()]

    def count_users(self, enabled_only: bool = False) -> int:
        sql = "SELECT COUNT(*) AS n FROM users"
        if enabled_only:
            sql += " WHERE enabled = 1"
        with self.connect() as conn:
            return int(conn.execute(sql).fetchone()["n"])

    # -- buildings ----------------------------------------------------------
    _BUILDING_COLUMNS = "name, latitude, longitude, source, updated_at"

    def list_buildings(self) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT {self._BUILDING_COLUMNS} FROM buildings ORDER BY name"
            ).fetchall()
        return sorted((dict(r) for r in rows), key=lambda r: sort_key(r["name"]))

    def get_building(self, name: str) -> dict | None:
        with self.connect() as conn:
            return _row(
                conn.execute(
                    f"SELECT {self._BUILDING_COLUMNS} FROM buildings WHERE name = ?",
                    (name,),
                ).fetchone()
            )

    def upsert_building(
        self,
        name: str,
        latitude: float,
        longitude: float,
        source: str = "",
    ) -> None:
        with self._write_lock, self.connect() as conn:
            conn.execute(
                "INSERT INTO buildings (name, latitude, longitude, source, updated_at)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(name) DO UPDATE SET"
                " latitude = excluded.latitude, longitude = excluded.longitude,"
                " source = excluded.source, updated_at = excluded.updated_at",
                (name, float(latitude), float(longitude), source[:100], utc_now_iso()),
            )

    def delete_building(self, name: str) -> None:
        with self._write_lock, self.connect() as conn:
            conn.execute("DELETE FROM buildings WHERE name = ?", (name,))

    # -- plans --------------------------------------------------------------
    def insert_plan(
        self,
        user_id: int,
        plan_date: str,
        planned_epoch: int,
        planned_time: str,
        status: str = "pending",
        last_error: str = "",
    ) -> bool:
        now = utc_now_iso()
        with self._write_lock, self.connect() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO plans
                    (user_id, plan_date, planned_epoch, planned_time, status,
                     last_error, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (user_id, plan_date, planned_epoch, planned_time, status,
                 last_error, now, now),
            )
            return cur.rowcount > 0

    def get_plan(self, user_id: int, plan_date: str) -> dict | None:
        with self.connect() as conn:
            return _row(
                conn.execute(
                    "SELECT * FROM plans WHERE user_id = ? AND plan_date = ?",
                    (user_id, plan_date),
                ).fetchone()
            )

    def get_plan_by_id(self, plan_id: int) -> dict | None:
        with self.connect() as conn:
            return _row(
                conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
            )

    def list_plans_for_date(self, plan_date: str) -> list[dict]:
        with self.connect() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM plans WHERE plan_date = ? ORDER BY planned_epoch",
                    (plan_date,),
                ).fetchall()
            ]

    def list_due_plans(self, now_epoch: int) -> list[dict]:
        with self.connect() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT * FROM plans
                    WHERE status = 'pending'
                      AND COALESCE(next_attempt_epoch, planned_epoch) <= ?
                    ORDER BY planned_epoch
                    """,
                    (now_epoch,),
                ).fetchall()
            ]

    def claim_plan(self, plan_id: int, now_iso: str | None = None) -> bool:
        now = now_iso or utc_now_iso()
        with self._write_lock, self.connect() as conn:
            cur = conn.execute(
                """
                UPDATE plans
                   SET status = 'running', attempts = attempts + 1, updated_at = ?
                 WHERE id = ? AND status = 'pending'
                """,
                (now, plan_id),
            )
            return cur.rowcount > 0

    _PLAN_FIELDS = {
        "status",
        "attempts",
        "next_attempt_epoch",
        "last_error",
        "result",
        "planned_epoch",
        "planned_time",
    }

    def update_plan(self, plan_id: int, **fields: Any) -> None:
        unknown = set(fields) - self._PLAN_FIELDS
        if unknown:
            raise ValueError(f"unknown plan fields: {sorted(unknown)}")
        if not fields:
            return
        fields["updated_at"] = utc_now_iso()
        assignments = ", ".join(f"{key} = ?" for key in fields)
        with self._write_lock, self.connect() as conn:
            conn.execute(
                f"UPDATE plans SET {assignments} WHERE id = ?",
                [*fields.values(), plan_id],
            )

    def delete_pending_plan(self, user_id: int, plan_date: str) -> None:
        with self._write_lock, self.connect() as conn:
            conn.execute(
                "DELETE FROM plans WHERE user_id = ? AND plan_date = ? "
                "AND status = 'pending'",
                (user_id, plan_date),
            )

    def delete_pending_plans_for_date(self, plan_date: str) -> int:
        with self._write_lock, self.connect() as conn:
            cur = conn.execute(
                "DELETE FROM plans WHERE plan_date = ? AND status = 'pending'",
                (plan_date,),
            )
            return cur.rowcount

    def recover_running_plans(self, now_iso: str | None = None) -> int:
        """A crash can leave a plan stuck in ``running``; make it retryable."""
        now = now_iso or utc_now_iso()
        with self._write_lock, self.connect() as conn:
            cur = conn.execute(
                "UPDATE plans SET status = 'pending', updated_at = ? "
                "WHERE status = 'running'",
                (now,),
            )
            return cur.rowcount

    def expire_pending_before(self, plan_date: str, message: str) -> int:
        now = utc_now_iso()
        with self._write_lock, self.connect() as conn:
            cur = conn.execute(
                """
                UPDATE plans
                   SET status = 'failed', last_error = ?, updated_at = ?
                 WHERE status IN ('pending', 'running') AND plan_date < ?
                """,
                (message, now, plan_date),
            )
            return cur.rowcount

    def prune_old(self, days: int = 90) -> None:
        cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
        cutoff_iso = datetime.fromtimestamp(cutoff, timezone.utc).isoformat(timespec="seconds")
        with self._write_lock, self.connect() as conn:
            conn.execute("DELETE FROM plans WHERE created_at < ?", (cutoff_iso,))
            conn.execute("DELETE FROM logs WHERE ts < ?", (cutoff_iso,))

    def recent_stats(self, days: int = 7, today: date | None = None) -> list[dict]:
        """过去 days 天（含今天）的计划执行情况，用于看板上的迷你趋势图。"""
        end = today or datetime.now().date()
        start = end - timedelta(days=days - 1)
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT plan_date, status, COUNT(*) AS n FROM plans"
                " WHERE plan_date >= ? AND plan_date <= ?"
                " GROUP BY plan_date, status",
                (start.isoformat(), end.isoformat()),
            ).fetchall()
        buckets: dict[str, dict[str, int]] = {}
        for row in rows:
            bucket = buckets.setdefault(row["plan_date"], {"total": 0, "success": 0})
            bucket["total"] += int(row["n"])
            if row["status"] == "success":
                bucket["success"] += int(row["n"])
        out: list[dict] = []
        for offset in range(days):
            day = start + timedelta(days=offset)
            bucket = buckets.get(day.isoformat(), {"total": 0, "success": 0})
            total = bucket["total"]
            out.append(
                {
                    "date": day.isoformat(),
                    "weekday": "一二三四五六日"[day.weekday()],
                    "total": total,
                    "success": bucket["success"],
                    "rate": round(bucket["success"] * 100 / total) if total else None,
                }
            )
        return out

    # -- logs ---------------------------------------------------------------
    def add_log(
        self,
        message: str,
        *,
        user_id: int | None = None,
        user_name: str = "",
        level: str = "INFO",
        event: str = "",
        detail: dict | None = None,
    ) -> None:
        detail_text = ""
        if detail:
            try:
                detail_text = json.dumps(detail, ensure_ascii=False)[:4000]
            except (TypeError, ValueError):
                detail_text = ""
        with self._write_lock, self.connect() as conn:
            conn.execute(
                """
                INSERT INTO logs (user_id, user_name, ts, level, event, message, detail)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (user_id, user_name, utc_now_iso(), level.upper(), event,
                 message[:2000], detail_text),
            )

    def list_logs(
        self, user_id: int | None = None, limit: int = 50, offset: int = 0
    ) -> list[dict]:
        where, params = "", []
        if user_id is not None:
            where = "WHERE user_id = ?"
            params.append(user_id)
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM logs {where} ORDER BY id DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
            return [dict(r) for r in rows]

    def count_logs(self, user_id: int | None = None) -> int:
        where, params = "", []
        if user_id is not None:
            where = "WHERE user_id = ?"
            params.append(user_id)
        with self.connect() as conn:
            return int(
                conn.execute(f"SELECT COUNT(*) AS n FROM logs {where}", params)
                .fetchone()["n"]
            )

    def clear_logs(self) -> None:
        with self._write_lock, self.connect() as conn:
            conn.execute("DELETE FROM logs")

    # -- sessions -----------------------------------------------------------
    def get_session(self, user_id: int) -> dict | None:
        with self.connect() as conn:
            return _row(
                conn.execute(
                    "SELECT * FROM sessions WHERE user_id = ?", (user_id,)
                ).fetchone()
            )

    def save_session(
        self,
        user_id: int,
        cookies_enc: str,
        saved_at: str | None = None,
        warmup_epoch: int | None = None,
    ) -> None:
        now = saved_at or utc_now_iso()
        with self._write_lock, self.connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions
                    (user_id, cookies_enc, saved_at, last_warmup_epoch, invalid,
                     logins_date, logins_today)
                VALUES (?, ?, ?, ?, 0, '', 0)
                ON CONFLICT(user_id) DO UPDATE SET
                    cookies_enc = excluded.cookies_enc,
                    saved_at = excluded.saved_at,
                    last_warmup_epoch = COALESCE(excluded.last_warmup_epoch,
                                                 sessions.last_warmup_epoch),
                    invalid = 0
                """,
                (user_id, cookies_enc, now, warmup_epoch),
            )

    def clear_session(self, user_id: int) -> None:
        with self._write_lock, self.connect() as conn:
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))

    def mark_session_invalid(self, user_id: int) -> None:
        with self._write_lock, self.connect() as conn:
            conn.execute(
                "UPDATE sessions SET invalid = 1 WHERE user_id = ?", (user_id,)
            )

    def mark_warmup(self, user_id: int, epoch: int) -> None:
        with self._write_lock, self.connect() as conn:
            conn.execute(
                "UPDATE sessions SET last_warmup_epoch = ? WHERE user_id = ?",
                (epoch, user_id),
            )

    def record_login(self, user_id: int, date_str: str, now_epoch: int) -> None:
        with self._write_lock, self.connect() as conn:
            row = conn.execute(
                "SELECT logins_date, logins_today FROM sessions WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if row is None:
                conn.execute(
                    """
                    INSERT INTO sessions
                        (user_id, cookies_enc, saved_at, last_login_epoch,
                         invalid, logins_date, logins_today)
                    VALUES (?, '', ?, ?, 0, ?, 1)
                    """,
                    (user_id, utc_now_iso(), now_epoch, date_str),
                )
            else:
                today_count = (
                    int(row["logins_today"]) + 1
                    if row["logins_date"] == date_str
                    else 1
                )
                conn.execute(
                    "UPDATE sessions SET last_login_epoch = ?, logins_date = ?, "
                    "logins_today = ?, invalid = 0 WHERE user_id = ?",
                    (now_epoch, date_str, today_count, user_id),
                )

    def can_login(
        self,
        user_id: int,
        date_str: str,
        now_epoch: int,
        cooldown_sec: int,
        max_per_day: int,
    ) -> tuple[bool, str]:
        row = self.get_session(user_id)
        if row is None:
            return True, ""
        last = row.get("last_login_epoch")
        if last and now_epoch - int(last) < cooldown_sec:
            remaining = cooldown_sec - (now_epoch - int(last))
            return False, f"登录冷却中，还需约 {remaining} 秒"
        if row.get("logins_date") == date_str and int(row.get("logins_today") or 0) >= max_per_day:
            return False, f"今日登录次数已达上限（{max_per_day} 次），为避免风控暂停"
        return True, ""

    # -- key/value ----------------------------------------------------------
    def get_kv(self, key: str, default: str = "") -> str:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else default

    def set_kv(self, key: str, value: str) -> None:
        with self._write_lock, self.connect() as conn:
            conn.execute(
                "INSERT INTO kv (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def db_size(self) -> int:
        try:
            return Path(self.path).stat().st_size
        except OSError:
            return 0

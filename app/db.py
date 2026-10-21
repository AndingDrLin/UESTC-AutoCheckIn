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
CREATE TABLE IF NOT EXISTS report_previews (
    id TEXT PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    business_date TEXT NOT NULL,
    entries TEXT NOT NULL,
    expires_epoch INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    message TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS report_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    student_id TEXT NOT NULL,
    location TEXT NOT NULL,
    reason TEXT NOT NULL,
    remark TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    weekdays TEXT NOT NULL,
    run_time TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS report_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id INTEGER NOT NULL REFERENCES report_rules(id) ON DELETE CASCADE,
    account_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    business_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    message TEXT NOT NULL DEFAULT '',
    preview_id TEXT,
    UNIQUE(rule_id, business_date)
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
        self._account_locks: dict[int, threading.RLock] = {}
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

    def account_lock(self, account_id: int):
        """Serialize authentication and writes for the same university account."""
        with self._write_lock:
            return self._account_locks.setdefault(account_id, threading.RLock())

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
            "window_start": "21:30",
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

    # -- administrator reporting --------------------------------------------
    def save_report_preview(self, preview: dict, expires_epoch: int) -> None:
        with self._write_lock, self.connect() as conn:
            conn.execute(
                "INSERT INTO report_previews (id, account_id, business_date, entries, expires_epoch) VALUES (?, ?, ?, ?, ?)",
                (preview['id'], preview['account_id'], preview['business_date'],
                 json.dumps(preview['entries'], ensure_ascii=False), expires_epoch),
            )

    def claim_report_preview(self, preview_id: str, account_id: int, now_epoch: int) -> dict:
        with self._write_lock, self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM report_previews WHERE id = ?', (preview_id,)).fetchone()
            if row is None or row['account_id'] != account_id:
                raise ValueError('预览不存在或不属于当前账号')
            if row['status'] != 'pending':
                raise ValueError('预览已使用，禁止重复提交')
            if row['expires_epoch'] <= now_epoch:
                raise ValueError('预览已过期，请重新预览')
            conn.execute("UPDATE report_previews SET status = 'claimed' WHERE id = ?", (preview_id,))
            result = dict(row)
            result['entries'] = json.loads(result['entries'])
            return result

    def finish_report_preview(self, preview_id: str, status: str, message: str) -> None:
        with self._write_lock, self.connect() as conn:
            conn.execute('UPDATE report_previews SET status = ?, message = ? WHERE id = ?',
                         (status, message, preview_id))

    @staticmethod
    def validate_report_rule(student_id, location, reason, start_date, end_date, weekdays, run_time):
        if not isinstance(student_id, str) or not student_id.strip() or not location or not reason or not str(location).strip() or not str(reason).strip():
            raise ValueError('学号、情况类型及关键词不能为空')
        start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
        if start.isoformat() != start_date or end.isoformat() != end_date or start > end:
            raise ValueError('日期范围无效')
        days = {int(day.strip()) for day in weekdays.split(',')}
        if not days or not days <= set(range(1, 8)):
            raise ValueError('星期必须为 1 到 7')
        parsed = datetime.strptime(run_time, '%H:%M')
        if parsed.strftime('%H:%M') != run_time:
            raise ValueError('运行时间必须为 HH:MM')
        return ','.join(str(day) for day in sorted(days))

    @staticmethod
    def _reject_report_overlap(conn, candidate: dict, exclude_id: int = 0) -> None:
        if not candidate['enabled']:
            return
        for row in conn.execute('SELECT * FROM report_rules WHERE enabled = 1 AND student_id = ? AND id != ?',
                                (candidate['student_id'], exclude_id)):
            start = max(date.fromisoformat(row['start_date']), date.fromisoformat(candidate['start_date']))
            end = min(date.fromisoformat(row['end_date']), date.fromisoformat(candidate['end_date']))
            days = set(row['weekdays'].split(',')) & set(candidate['weekdays'].split(','))
            for offset in range(min(7, max(0, (end - start).days + 1))):
                if str((start + timedelta(days=offset)).isoweekday()) in days:
                    raise ValueError(f"与规则 #{row['id']} 重叠：同一学生不可重复安排（包括其他账号）")

    def create_report_rule(self, account_id: int, student_id: str, location: str, reason: str,
                           remark: str, start_date: str, end_date: str, weekdays: str,
                           run_time: str, enabled: bool = True) -> int:
        weekdays = self.validate_report_rule(student_id, location, reason, start_date, end_date, weekdays, run_time)
        student_id = student_id.strip()
        values = dict(account_id=account_id, student_id=student_id, location=location, reason=reason,
                      remark=remark, start_date=start_date, end_date=end_date, weekdays=weekdays,
                      run_time=run_time, enabled=int(enabled))
        with self._write_lock, self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            self._reject_report_overlap(conn, values)
            cur = conn.execute(f"INSERT INTO report_rules ({', '.join(values)}) VALUES ({', '.join('?' for _ in values)})",
                               tuple(values.values()))
            return int(cur.lastrowid)

    def update_report_rule(self, rule_id: int, account_id: int, student_id: str, location: str,
                           reason: str, remark: str, start_date: str, end_date: str,
                           weekdays: str, run_time: str, enabled: bool = True) -> None:
        weekdays = self.validate_report_rule(student_id, location, reason, start_date, end_date, weekdays, run_time)
        student_id = student_id.strip()
        values = dict(student_id=student_id, location=location, reason=reason, remark=remark,
                      start_date=start_date, end_date=end_date, weekdays=weekdays,
                      run_time=run_time, enabled=int(enabled))
        with self._write_lock, self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if conn.execute('SELECT id FROM report_rules WHERE id = ? AND account_id = ?',
                            (rule_id, account_id)).fetchone() is None:
                raise ValueError('规则不存在或不属于当前账号')
            self._reject_report_overlap(conn, values, rule_id)
            conn.execute(f"UPDATE report_rules SET {', '.join(key + ' = ?' for key in values)} WHERE id = ?",
                         (*values.values(), rule_id))

    def list_report_rules(self, account_id: int | None = None) -> list[dict]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(
                'SELECT * FROM report_rules' + (' WHERE account_id = ?' if account_id is not None else '') + ' ORDER BY id',
                (account_id,) if account_id is not None else ())]

    def set_report_rule_enabled(self, rule_id: int, enabled: bool) -> None:
        with self._write_lock, self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM report_rules WHERE id = ?', (rule_id,)).fetchone()
            if row is None:
                raise ValueError('规则不存在')
            candidate = dict(row, enabled=int(enabled))
            self._reject_report_overlap(conn, candidate, rule_id)
            conn.execute('UPDATE report_rules SET enabled = ? WHERE id = ?', (int(enabled), rule_id))

    def delete_report_rule(self, rule_id: int) -> None:
        with self._write_lock, self.connect() as conn:
            conn.execute('DELETE FROM report_rules WHERE id = ?', (rule_id,))

    def list_report_jobs(self, account_id: int | None = None) -> list[dict]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(
                'SELECT * FROM report_jobs' + (' WHERE account_id = ?' if account_id is not None else '') + ' ORDER BY id DESC',
                (account_id,) if account_id is not None else ())]

    def claim_report_job(self, rule_id: int, account_id: int, business_date: str) -> int | None:
        with self._write_lock, self.connect() as conn:
            cur = conn.execute("INSERT OR IGNORE INTO report_jobs (rule_id, account_id, business_date, status) VALUES (?, ?, ?, 'running')",
                               (rule_id, account_id, business_date))
            return int(cur.lastrowid) if cur.rowcount else None

    def finish_report_job(self, job_id: int, status: str, message: str, preview_id: str | None = None) -> None:
        with self._write_lock, self.connect() as conn:
            conn.execute('UPDATE report_jobs SET status = ?, message = ?, preview_id = ? WHERE id = ?',
                         (status, message, preview_id, job_id))

    def recover_report_jobs(self) -> int:
        with self._write_lock, self.connect() as conn:
            cur = conn.execute("UPDATE report_jobs SET status = 'uncertain', message = '进程中断，结果不确定；请人工核对，禁止自动重试' WHERE status IN ('running', 'writing')")
            conn.execute("UPDATE report_previews SET status = 'uncertain', message = '进程中断，请人工核对' WHERE status IN ('claimed', 'writing')")
            return cur.rowcount

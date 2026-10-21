"""Database layer: users, plans, sessions, login limits, logs."""
from __future__ import annotations

import pytest

from app.db import Database, DuplicateStudentId


def make_db(tmp_path) -> Database:
    return Database(tmp_path / "test.db")


def make_user(db: Database, student_id: str = "2020001") -> int:
    return db.create_user(
        name="张三",
        student_id=student_id,
        password_enc="encrypted",
        latitude=30.75498,
        longitude=103.933883,
    )


def test_user_crud_and_unique_student_id(tmp_path):
    db = make_db(tmp_path)
    uid = make_user(db)

    user = db.get_user(uid)
    assert user is not None
    assert user["name"] == "张三"
    assert user["student_id"] == "2020001"
    assert user["enabled"] == 1
    assert user["window_start"] == "21:30"
    assert user["window_end"] == "23:00"
    assert db.get_user_by_student_id("2020001")["id"] == uid
    assert db.count_users() == 1

    db.update_user(uid, dorm_building="学知苑 5 栋", dorm_room="512", enabled=0)
    user = db.get_user(uid)
    assert user["dorm_building"] == "学知苑 5 栋"
    assert user["enabled"] == 0
    assert db.count_users(enabled_only=True) == 0

    with pytest.raises(DuplicateStudentId):
        make_user(db)

    db.delete_user(uid)
    assert db.get_user(uid) is None


def test_plan_lifecycle_and_due_selection(tmp_path):
    db = make_db(tmp_path)
    uid = make_user(db)
    day = "2026-10-21"

    assert db.insert_plan(uid, day, 1_000, "21:40:00") is True
    assert db.insert_plan(uid, day, 1_000, "21:40:00") is False
    plan = db.get_plan(uid, day)
    assert plan["status"] == "pending"
    assert plan["attempts"] == 0

    assert db.list_due_plans(999) == []
    assert [p["id"] for p in db.list_due_plans(1_000)] == [plan["id"]]

    assert db.claim_plan(plan["id"]) is True
    assert db.claim_plan(plan["id"]) is False
    plan = db.get_plan(uid, day)
    assert plan["status"] == "running"
    assert plan["attempts"] == 1

    db.update_plan(plan["id"], status="pending", next_attempt_epoch=2_000)
    assert db.list_due_plans(1_999) == []
    assert len(db.list_due_plans(2_000)) == 1

    db.update_plan(plan["id"], status="success", result='{"ok": true}')
    plan = db.get_plan(uid, day)
    assert plan["status"] == "success"
    assert plan["result"] == '{"ok": true}'


def test_recover_expire_and_delete_pending(tmp_path):
    db = make_db(tmp_path)
    uid = make_user(db)

    db.insert_plan(uid, "2026-10-20", 1_000, "21:40:00")
    db.insert_plan(uid, "2026-10-21", 2_000, "21:50:00")
    old = db.get_plan(uid, "2026-10-20")
    db.update_plan(old["id"], status="running")

    assert db.recover_running_plans() == 1
    assert db.get_plan(uid, "2026-10-20")["status"] == "pending"

    assert db.expire_pending_before("2026-10-21", "窗口已过") == 1
    assert db.get_plan(uid, "2026-10-20")["status"] == "failed"
    assert db.get_plan(uid, "2026-10-21")["status"] == "pending"

    assert db.delete_pending_plans_for_date("2026-10-21") == 1
    assert db.get_plan(uid, "2026-10-21") is None


def test_sessions_and_login_limits(tmp_path):
    db = make_db(tmp_path)
    uid = make_user(db)

    db.save_session(uid, "cipher-1", warmup_epoch=111)
    session = db.get_session(uid)
    assert session["cookies_enc"] == "cipher-1"
    assert session["invalid"] == 0
    assert session["last_warmup_epoch"] == 111

    db.mark_warmup(uid, 222)
    assert db.get_session(uid)["last_warmup_epoch"] == 222

    db.mark_session_invalid(uid)
    assert db.get_session(uid)["invalid"] == 1
    db.save_session(uid, "cipher-2")
    assert db.get_session(uid)["invalid"] == 0

    db.record_login(uid, "2026-10-21", 1_000)
    db.record_login(uid, "2026-10-21", 2_000)
    assert db.get_session(uid)["logins_today"] == 2

    ok, reason = db.can_login(uid, "2026-10-21", 2_100, 600, 3)
    assert ok is False and "冷却" in reason
    ok, _ = db.can_login(uid, "2026-10-21", 2_600, 600, 3)
    assert ok is True

    db.record_login(uid, "2026-10-21", 3_000)
    ok, reason = db.can_login(uid, "2026-10-21", 4_000, 0, 3)
    assert ok is False and "上限" in reason

    db.record_login(uid, "2026-10-22", 5_000)
    assert db.get_session(uid)["logins_today"] == 1
    ok, _ = db.can_login(uid, "2026-10-22", 6_000, 0, 3)
    assert ok is True

    db.clear_session(uid)
    assert db.get_session(uid) is None


def test_logs_are_scoped_and_clearable(tmp_path):
    db = make_db(tmp_path)
    uid = make_user(db)
    other = db.create_user(name="李四", student_id="2020002")

    db.add_log("hello", user_id=uid, user_name="张三", event="test", detail={"a": 1})
    db.add_log("other", user_id=other, user_name="李四")

    assert db.count_logs() == 2
    assert db.count_logs(uid) == 1
    row = db.list_logs(uid)[0]
    assert row["message"] == "hello"
    assert row["event"] == "test"
    assert '"a": 1' in row["detail"]

    db.clear_logs()
    assert db.count_logs() == 0

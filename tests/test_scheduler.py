"""Scheduler tests: plan generation, idempotency, execution and expiry."""
from __future__ import annotations

import json
import random
from datetime import datetime, timedelta

import pytest

from app.config import Settings
from app.db import Database
from app.scheduler import CheckinScheduler
from app.uestc.service import Outcome


class FakeService:
    def __init__(self, outcome: Outcome | None = None) -> None:
        self.outcome = outcome or Outcome(True, message="打卡成功")
        self.calls: list[dict] = []

    def run(self, user: dict) -> Outcome:
        self.calls.append(user)
        return self.outcome


def make_env(tmp_path, outcome: Outcome | None = None):
    settings = Settings(
        data_dir=tmp_path / "data",
        db_path=tmp_path / "data" / "test.db",
        admin_password="test-password",
        app_secret="test-secret",
        scheduler_enabled=False,
        min_user_gap_sec=30,
    )
    db = Database(settings.db_path)
    service = FakeService(outcome)
    scheduler = CheckinScheduler(settings, db, service, rng=random.Random(7))
    return settings, db, service, scheduler


def add_user(db: Database, **overrides) -> int:
    fields = {
        "name": "测试用户",
        "student_id": "20200001",
        "password_enc": "encrypted",
        "fingerprint": "",
        "dorm_building": "1 栋",
        "dorm_room": "101",
        "address": "四川省成都市",
        "latitude": 30.75,
        "longitude": 103.93,
        "window_start": "21:30",
        "window_end": "23:00",
        "notify_url": "",
        "notes": "",
        "enabled": 1,
    }
    fields.update(overrides)
    return db.create_user(**fields)


def test_ensure_plans_is_random_and_idempotent(tmp_path):
    settings, db, _, scheduler = make_env(tmp_path)
    try:
        user_id = add_user(db)
        now = datetime(2026, 10, 21, 21, 35, tzinfo=settings.tz)
        scheduler.ensure_plans(now)

        plan = db.get_plan(user_id, "2026-10-21")
        assert plan is not None
        assert plan["status"] == "pending"
        assert int(plan["planned_epoch"]) >= int(now.timestamp()) + 20
        assert plan["planned_time"] >= "21:35"

        scheduler.ensure_plans(now + timedelta(minutes=1))
        again = db.get_plan(user_id, "2026-10-21")
        assert again["planned_epoch"] == plan["planned_epoch"]
        assert again["planned_time"] == plan["planned_time"]
    finally:
        scheduler._executor.shutdown(wait=True)


def test_plan_outside_window_is_marked_missed(tmp_path):
    settings, db, _, scheduler = make_env(tmp_path)
    try:
        user_id = add_user(db)
        now = datetime(2026, 10, 21, 23, 10, tzinfo=settings.tz)
        scheduler.ensure_plans(now)
        plan = db.get_plan(user_id, "2026-10-21")
        assert plan["status"] == "missed"
        assert "窗口已过" in plan["last_error"]
    finally:
        scheduler._executor.shutdown(wait=True)


def test_run_plan_marks_success_and_stores_result(tmp_path):
    settings, db, service, scheduler = make_env(
        tmp_path, Outcome(True, message="打卡成功", verified=True)
    )
    try:
        user_id = add_user(db)
        epoch = int(datetime(2026, 10, 21, 21, 45, tzinfo=settings.tz).timestamp())
        db.insert_plan(user_id, "2026-10-21", epoch, "21:45:00")
        plan = db.get_plan(user_id, "2026-10-21")
        assert db.claim_plan(int(plan["id"]))

        scheduler._run_plan(int(plan["id"]), user_id)

        updated = db.get_plan_by_id(int(plan["id"]))
        assert updated["status"] == "success"
        assert updated["next_attempt_epoch"] is None
        assert json.loads(updated["result"])["ok"] is True
        assert len(service.calls) == 1
    finally:
        scheduler._executor.shutdown(wait=True)


def test_run_plan_schedules_retry_for_transient_failure(tmp_path):
    settings, db, _, scheduler = make_env(
        tmp_path, Outcome(False, False, "网络错误：连接超时")
    )
    try:
        user_id = add_user(db, window_end="23:59")
        epoch = int(datetime(2026, 10, 21, 21, 45, tzinfo=settings.tz).timestamp())
        db.insert_plan(user_id, "2026-10-21", epoch, "21:45:00")
        plan = db.get_plan(user_id, "2026-10-21")
        assert db.claim_plan(int(plan["id"]))

        scheduler._run_plan(int(plan["id"]), user_id)

        updated = db.get_plan_by_id(int(plan["id"]))
        assert updated["status"] == "pending"
        assert updated["next_attempt_epoch"] is not None
        assert updated["attempts"] == 1
        assert "网络错误" in updated["last_error"]
    finally:
        scheduler._executor.shutdown(wait=True)


def test_expire_marks_yesterdays_and_todays_plans_failed(tmp_path):
    settings, db, _, scheduler = make_env(tmp_path)
    try:
        user_id = add_user(db, window_end="22:00")
        old = int(datetime(2026, 10, 20, 21, 45, tzinfo=settings.tz).timestamp())
        db.insert_plan(user_id, "2026-10-20", old, "21:45:00")
        today = int(datetime(2026, 10, 21, 21, 45, tzinfo=settings.tz).timestamp())
        db.insert_plan(user_id, "2026-10-21", today, "21:45:00")

        scheduler._expire(datetime(2026, 10, 21, 22, 5, tzinfo=settings.tz))

        assert db.get_plan(user_id, "2026-10-20")["status"] == "failed"
        current = db.get_plan(user_id, "2026-10-21")
        assert current["status"] == "failed"
        assert "窗口已结束" in current["last_error"]
    finally:
        scheduler._executor.shutdown(wait=True)

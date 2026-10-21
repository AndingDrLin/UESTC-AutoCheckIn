"""Regression tests for the administrative CLI."""
from __future__ import annotations

from app import cli
from app.config import Settings
from app.db import Database
from app.scheduler import CheckinScheduler
from app.security import SecretBox
from app.uestc.service import CheckinService


def _context(tmp_path):
    cfg = Settings(
        data_dir=tmp_path / "data",
        db_path=tmp_path / "data" / "checkin.db",
        admin_password="test-admin",
        app_secret="test-secret",
        scheduler_enabled=False,
    )
    db = Database(cfg.db_path)
    box = SecretBox(cfg.app_secret)
    service = CheckinService(cfg, db, box)
    scheduler = CheckinScheduler(cfg, db, service)
    return cfg, db, box, service, scheduler


def test_user_update_disable_and_enable(tmp_path, monkeypatch):
    cfg, db, box, service, scheduler = _context(tmp_path)
    user_id = db.create_user(
        name="测试用户",
        student_id="2023000001",
        password_enc=box.encrypt("secret"),
        latitude=30.753736,
        longitude=103.934585,
    )
    monkeypatch.setattr(cli, "_context", lambda: (cfg, db, box, service, scheduler))

    parser = cli.build_parser()
    disable = parser.parse_args(["user", "update", "2023000001", "--disable"])
    assert disable.disable is True
    assert cli.cmd_user_update(disable) == 0
    assert db.get_user(user_id)["enabled"] == 0

    enable = parser.parse_args(["user", "update", "2023000001", "--enable"])
    assert enable.enable is True
    assert cli.cmd_user_update(enable) == 0
    assert db.get_user(user_id)["enabled"] == 1

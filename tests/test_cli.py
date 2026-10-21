"""Behavioral coverage for explicit acting accounts and safe CLI submissions."""
from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from app import cli
from app.config import Settings
from app.db import Database
from app.security import SecretBox
from app.uestc.errors import ServiceError


@pytest.fixture
def context(tmp_path, monkeypatch):
    cfg = Settings(data_dir=tmp_path / "data", db_path=tmp_path / "data" / "checkin.db",
                   admin_password="test-admin", app_secret="test-secret", scheduler_enabled=False)
    db = Database(cfg.db_path)
    box = SecretBox(cfg.app_secret)
    calls = []
    personal = SimpleNamespace(
        test=lambda account: calls.append(("test", account["id"])) or SimpleNamespace(
            ok=True, to_dict=lambda: {"ok": True, "message": "preview"}),
        run=lambda account, manual: calls.append(("run", account["id"], manual)) or SimpleNamespace(
            ok=True, fatal=False, message="submitted", detail={},
            to_dict=lambda: {"ok": True, "message": "submitted"}),
    )
    scheduler = SimpleNamespace(ensure_plans=lambda *args: calls.append(("plans",)))
    admin = SimpleNamespace()
    monkeypatch.setattr(cli, "_context", lambda: (cfg, db, box, personal, scheduler, admin))
    return cfg, db, box, calls, admin


def _account(context, student_id="2023000001", **fields):
    _, db, box, _, _ = context
    return db.create_user(name="管理账号", student_id=student_id, password_enc=box.encrypt("secret"),
                          enabled=0, **fields)


def test_account_add_defaults_to_admin_only(context, capsys):
    assert cli.main(["account", "add", "--name", "辅导员", "--student-id", "2023000001",
                     "--password", "secret", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["enabled"] == 0
    assert "password_enc" not in payload
    assert context[3] == []
    assert context[2].decrypt(context[1].get_user(payload["id"])["password_enc"]) == "secret"


def test_enable_requires_coordinates_but_account_remains_available(context):
    account_id = _account(context)
    assert cli.main(["self", "enable", "--account", "2023000001"]) == 1
    assert context[1].get_user(account_id)["enabled"] == 0
    assert cli.main(["self", "config", "--account", "2023000001", "--lat", "30.753736", "--lng", "103.934585"]) == 0
    assert cli.main(["self", "enable", "--account", "2023000001"]) == 0
    assert context[1].get_user(account_id)["enabled"] == 1
    assert cli.main(["self", "disable", "--account", "2023000001"]) == 0
    assert context[1].get_user_by_student_id("2023000001") is not None


@pytest.mark.parametrize("options", [
    ["--lat", "30"], ["--lat", "nan", "--lng", "103"],
    ["--window-start", "20:00"], ["--window-end", "21:00"],
])
def test_invalid_personal_config_does_not_mutate_account(context, options):
    account_id = _account(context)
    before = context[1].get_user(account_id)
    assert cli.main(["self", "config", "--account", "2023000001", *options]) == 1
    assert context[1].get_user(account_id) == before


def test_partial_window_update_preserves_existing_other_boundary(context):
    account_id = _account(context, window_start="21:15", window_end="22:45")
    assert cli.main(["self", "config", "--account", "2023000001", "--window-start", "21:45"]) == 0
    account = context[1].get_user(account_id)
    assert (account["window_start"], account["window_end"]) == ("21:45", "22:45")


def test_personal_run_defaults_to_non_submitting_test(context):
    account_id = _account(context)
    assert cli.main(["self", "run", "--account", "2023000001"]) == 0
    assert context[3] == [("test", account_id)]


def test_personal_manual_submit_does_not_require_automation_enabled(context, monkeypatch):
    account_id = _account(context, latitude=30.753736, longitude=103.934585)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 21, 22, 0, tzinfo=tz)
    monkeypatch.setattr(cli, "datetime", Clock)
    assert cli.main(["self", "run", "--account", "2023000001", "--submit"]) == 0
    assert context[3] == [("run", account_id, True)]
    assert context[1].get_user(account_id)["enabled"] == 0


def test_personal_submit_outside_window_never_calls_service(context, monkeypatch):
    _account(context, latitude=30.753736, longitude=103.934585)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 21, 12, 0, tzinfo=tz)
    monkeypatch.setattr(cli, "datetime", Clock)
    assert cli.main(["self", "run", "--account", "2023000001", "--submit"]) == 1
    assert context[3] == []


def test_report_preview_never_submits_and_exposes_resolved_identity(context, capsys):
    account_id = _account(context)
    admin = context[4]
    def prepare(account, day, targets, location, reason, remark):
        assert account["id"] == account_id
        assert targets == [{"student_id": "target-001", "class_key": "class-1"},
                           {"student_name": "张三", "class_key": "class-1"}]
        return {"id": "preview-1", "account_id": account_id, "business_date": day,
                "entries": [{"student_id": "target-001", "student_name": "李四", "location": location,
                             "reason": reason, "remark": remark}]}
    admin.prepare = prepare
    admin.submit = lambda *args: pytest.fail("Preview must not submit")
    assert cli.main(["report", "--account", "2023000001", "--student-id", "target-001",
                     "--student-name", "张三", "--class-key", "class-1", "--date", "2026-10-21",
                     "--location", "校内", "--reason", "学习", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["account"] == "2023000001"
    assert payload["status"] == "preview"
    assert payload["preview"]["business_date"] == "2026-10-21"
    assert payload["preview"]["entries"][0]["student_name"] == "李四"


@pytest.mark.parametrize("status,code", [("acknowledged", 0), ("uncertain", 1), ("failed", 1)])
def test_report_submit_preserves_upstream_acknowledgement_status(context, capsys, status, code):
    account_id = _account(context)
    admin = context[4]
    admin.prepare = lambda *args: {"id": "single-use", "entries": []}
    submissions = []
    def submit(account, preview_id):
        submissions.append((account["id"], preview_id))
        return {"status": status, "message": "upstream result", "entries": []}
    admin.submit = submit
    assert cli.main(["report", "--account", "2023000001", "--student-id", "target", "--location", "校内",
                     "--reason", "学习", "--submit", "--json"]) == code
    assert submissions == [(account_id, "single-use")]
    assert json.loads(capsys.readouterr().out)["status"] == status


def test_report_resolution_failure_never_submits(context):
    _account(context)
    def prepare(*args):
        raise ServiceError("姓名匹配多个学生")
    context[4].prepare = prepare
    context[4].submit = lambda *args: pytest.fail("Resolution failure must not submit")
    assert cli.main(["report", "--account", "2023000001", "--student-name", "张三", "--location", "校内",
                     "--reason", "学习", "--submit"]) == 1


def test_rule_cannot_mutate_another_accounts_rule(context):
    _account(context)
    context[1].list_report_rules = lambda account_id: [{"id": 7, "account_id": account_id}]
    context[1].delete_report_rule = lambda rule_id: pytest.fail("Wrong account rule must not be deleted")
    assert cli.main(["rule", "remove", "--account", "2023000001", "--rule-id", "8"]) == 1


def test_rule_jobs_are_scoped_to_acting_account(context, capsys):
    account_id = _account(context)
    context[1].list_report_jobs = lambda selected: [{"rule_id": 4, "business_date": "2026-10-21",
                                                   "status": "uncertain", "account_id": selected}]
    assert cli.main(["rule", "jobs", "--account", "2023000001", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["data"][0]["account_id"] == account_id
    assert payload["data"][0]["status"] == "uncertain"


def test_password_change_clears_cached_session(context):
    account_id = _account(context)
    cleared = []
    context[1].clear_session = cleared.append
    assert cli.main(["account", "password", "--account", "2023000001", "--password", "new-secret"]) == 0
    assert cleared == [account_id]
    assert context[2].decrypt(context[1].get_user(account_id)["password_enc"]) == "new-secret"


def test_fingerprint_update_invalidates_session_but_coordinates_do_not(context):
    account_id = _account(context)
    cleared = []
    context[1].clear_session = cleared.append
    assert cli.main(["self", "config", "--account", "2023000001", "--lat", "30.75", "--lng", "103.93"]) == 0
    assert cleared == []
    assert cli.main(["account", "update", "--account", "2023000001", "--fingerprint", "A" * 32]) == 0
    assert cleared == [account_id]


def test_account_audit_never_contains_credentials(context):
    assert cli.main(["account", "add", "--name", "辅导员", "--student-id", "2023000001", "--password", "private-password"]) == 0
    assert cli.main(["account", "password", "--account", "2023000001", "--password", "replacement-password"]) == 0
    logs = context[1].list_logs()
    assert {log["event"] for log in logs} >= {"cli.account.add", "cli.account.password"}
    assert "private-password" not in json.dumps(logs)
    assert "replacement-password" not in json.dumps(logs)

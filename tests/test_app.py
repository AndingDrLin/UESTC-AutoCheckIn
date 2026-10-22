"""End-to-end admin UI smoke tests with FastAPI's TestClient (no network)."""
from __future__ import annotations

import re
import time
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.uestc.service import Outcome


@pytest.fixture()
def app(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data",
        db_path=tmp_path / "data" / "test.db",
        admin_password="s3cret",
        app_secret="test-secret",
        scheduler_enabled=False,
    )
    return create_app(settings)


def csrf_from(html: str) -> str:
    match = re.search(r'name="_csrf" value="([^"]+)"', html)
    assert match, "page did not contain a CSRF token"
    return match.group(1)


def login(client: TestClient) -> None:
    response = client.post(
        "/login", data={"password": "s3cret"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/"


def add_user(client: TestClient, student_id: str = "2020001") -> int:
    page = client.get("/users/new")
    assert page.status_code == 200
    csrf = csrf_from(page.text)
    response = client.post(
        "/users/new",
        data={
            "_csrf": csrf,
            "name": "张三",
            "student_id": student_id,
            "password": "cas-password",
            "fingerprint": "A" * 32,
            "dorm_building": "本科13栋",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    return response


def test_admin_requires_login_and_rejects_bad_password(app):
    with TestClient(app) as client:
        response = client.get("/", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"

        bad = client.post("/login", data={"password": "wrong"}, follow_redirects=False)
        assert bad.status_code == 401
        assert "管理密码错误" in bad.text

        login(client)
        page = client.get("/")
        assert page.status_code == 200
        assert "今晚的打卡" in page.text
        assert "还没有用户" in page.text


def test_add_user_dashboard_edit_logs_settings_and_health(app):
    with TestClient(app) as client:
        login(client)
        add_user(client)

        page = client.get("/")
        assert page.status_code == 200
        assert "张三" in page.text
        assert "2020001" in page.text
        assert "本科13栋" in page.text

        db = app.state.db
        user = db.list_users()[0]
        user_id = user["id"]
        today = datetime.now(app.state.settings.tz).strftime("%Y-%m-%d")
        assert db.insert_plan(user_id, today, int(time.time()), "21:45:00")
        db.save_session(user_id, "encrypted-cookie", warmup_epoch=1)

        page = client.get("/")
        assert "21:45:00" in page.text

        edit = client.get(f"/users/{user_id}/edit")
        assert edit.status_code == 200
        assert "编辑 张三" in edit.text
        assert "重新匹配坐标" in edit.text
        assert "本科13栋" in edit.text

        logs = client.get("/logs")
        assert logs.status_code == 200
        assert "运行日志" in logs.text
        assert "user.create" in logs.text

        settings_page = client.get("/settings")
        assert settings_page.status_code == 200
        assert "设置与运行状态" in settings_page.text
        assert "自动打卡窗口" in settings_page.text
        assert "宿舍楼坐标" in settings_page.text
        assert "本科13栋" in settings_page.text

        health = client.get("/healthz")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"
        assert health.json()["version"]


def test_test_result_template_renders_full_detail(app, monkeypatch):
    with TestClient(app) as client:
        login(client)
        add_user(client)
        user_id = app.state.db.list_users()[0]["id"]

        outcome = Outcome(
            ok=True,
            message="链路正常，未提交打卡。",
            detail={
                "session_valid": True,
                "login_used": False,
                "today": {
                    "business_date": "2026-10-21",
                    "record_status_code": "CHECKED_IN",
                    "checkin_time": "2026-10-21 22:03:11",
                    "distance_meter": 12.5,
                    "abnormal_flag": False,
                    "location_name": "学知苑 5 栋",
                },
                "schedule": {
                    "name": "晚安成电",
                    "window": "21:00–23:30",
                    "effective": True,
                },
                "locations": [
                    {
                        "id": 7,
                        "location_name": "学知苑 5 栋",
                        "address": "四川省成都市郫都区",
                        "latitude": 30.75498,
                        "longitude": 103.933883,
                        "radius_meter": 500,
                        "group_name": "宿舍区",
                    }
                ],
                "location_count": 1,
            },
        )
        monkeypatch.setattr(app.state.service, "test", lambda user: outcome)

        page = client.get("/")
        response = client.post(
            f"/users/{user_id}/test",
            data={"_csrf": csrf_from(page.text)},
        )
        assert response.status_code == 200
        assert "链路测试" in response.text
        assert "CHECKED_IN" in response.text
        assert "校准" in response.text
        assert "学知苑 5 栋" in response.text


def test_building_crud_and_user_matching(app):
    with TestClient(app) as client:
        login(client)
        page = client.get("/settings")
        csrf = csrf_from(page.text)

        response = client.post(
            "/buildings",
            data={
                "_csrf": csrf,
                "name": "本科 7 栋",
                "latitude": "30.754000",
                "longitude": "103.932000",
                "source": "手动录入",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        building = app.state.db.get_building("本科7栋")
        assert building is not None
        assert round(building["latitude"], 3) == 30.754

        page = client.get("/settings")
        assert "本科7栋" in page.text

        # 用户坐标以楼栋基准为中心抖动，必须落在基准附近。
        add = client.get("/users/new")
        response = client.post(
            "/users/new",
            data={
                "_csrf": csrf_from(add.text),
                "name": "李四",
                "student_id": "2020002",
                "password": "cas-password",
                "fingerprint": "",
                "dorm_building": "本科7栋",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        user = app.state.db.get_user_by_student_id("2020002")
        assert user is not None
        assert user["dorm_building"] == "本科7栋"
        assert abs(user["latitude"] - 30.754) < 0.001
        assert abs(user["longitude"] - 103.932) < 0.001

        # 未登记的楼栋会被拒绝，并把错误回显在表单上。
        add = client.get("/users/new")
        response = client.post(
            "/users/new",
            data={
                "_csrf": csrf_from(add.text),
                "name": "王五",
                "student_id": "2020003",
                "password": "cas-password",
                "fingerprint": "",
                "dorm_building": "本科99栋",
            },
            follow_redirects=False,
        )
        assert response.status_code == 400
        assert "本科99栋" in response.text

        page = client.get("/settings")
        response = client.post(
            "/buildings/delete",
            data={"_csrf": csrf_from(page.text), "name": "本科7栋"},
            follow_redirects=False,
        )
        assert response.status_code == 303


def test_dashboard_can_add_building(app):
    with TestClient(app) as client:
        login(client)
        page = client.get("/")
        assert "宿舍楼坐标" in page.text
        assert 'action="/buildings"' in page.text

        # 看板上的楼栋表单提交后回到看板，而不是设置页。
        response = client.post(
            "/buildings",
            data={
                "_csrf": csrf_from(page.text),
                "name": "本科 9 栋",
                "latitude": "30.755000",
                "longitude": "103.934000",
                "source": "看板新增",
                "next": "/",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"].startswith("/?")
        assert app.state.db.get_building("本科9栋") is not None

        follow = client.get(response.headers["location"])
        assert "本科9栋" in follow.text
        assert app.state.db.get_building("本科7栋") is None


def test_csrf_is_enforced_for_mutations(app):
    with TestClient(app) as client:
        login(client)
        add_user(client)
        user_id = app.state.db.list_users()[0]["id"]

        response = client.post(
            f"/users/{user_id}/toggle",
            data={"_csrf": "definitely-wrong"},
            follow_redirects=False,
        )
        assert response.status_code == 400
        assert "表单已过期" in response.text


def test_logout_clears_cookie(app):
    with TestClient(app) as client:
        login(client)
        page = client.get("/")
        response = client.post(
            "/logout",
            data={"_csrf": csrf_from(page.text)},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/login"
        assert client.get("/", follow_redirects=False).status_code == 303

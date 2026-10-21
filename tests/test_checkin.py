"""H5 payload construction, status parsing and API error classification."""
from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
import requests

from app.uestc.checkin import (
    LocationInfo,
    ScheduleInfo,
    TodayStatus,
    _parse_json,
    build_payload,
    is_session_message,
)
from app.uestc.errors import ApiError, SessionInvalid


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.url = "https://jzsz.uestc.edu.cn/api/test"

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def test_build_payload_matches_captured_shape():
    when = datetime(2026, 10, 21, 22, 15, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    payload = build_payload(30.7549801, 103.9338831, "", "UA/1.0", when)

    assert payload["latitude"] == 30.75498
    assert payload["longitude"] == 103.933883
    assert payload["reverseGeocode"] == "四川省成都市"
    assert payload["clientTime"] == "2026-10-21 22:15:30"
    assert payload["userAgent"] == "UA/1.0"
    assert payload["coordinateSystemCode"] == "gcj02"
    assert payload["networkTypeCode"] == "4g"
    assert payload["locationEnabled"] is False
    assert payload["locationAuthorized"] == ""
    assert payload["locationAccuracy"] == ""
    assert payload["errorMessage"] == ""
    assert set(payload) == {
        "latitude", "longitude", "reverseGeocode", "clientTime", "userAgent",
        "coordinateSystemCode", "accuracy", "locationDurationMs",
        "networkTypeCode", "locationEnabled", "locationAuthorized",
        "locationAccuracy", "errorMessage",
    }


def test_today_status_is_checked_in_and_summary_is_redacted():
    raw = {
        "businessDate": "2026-10-21",
        "recordStatusCode": "CHECKED_IN",
        "checkinTime": "2026-10-21 22:03:11",
        "distanceMeter": 12.5,
        "abnormalFlag": False,
        "checkinLocationName": "学知苑 5 栋",
        "checkinLocationLatitude": 30.7549,
        "checkinLocationLongitude": 103.9338,
        "checkinRadiusMeter": 500,
        "name": "张三",
        "studentId": "2020001",
        "phone": "13800000000",
    }
    status = TodayStatus.from_data(raw)
    assert status.checked_in is True
    assert status.distance_meter == 12.5
    summary = status.safe_summary()
    assert "name" not in summary and "studentId" not in summary and "phone" not in summary
    assert summary["record_status_code"] == "CHECKED_IN"
    assert summary["business_date"] == "2026-10-21"


def test_schedule_and_location_parsing():
    schedule = ScheduleInfo.from_data(
        {
            "scheduleName": "晚安成电",
            "studentCheckinStartOffsetMinute": 1260,
            "studentCheckinEndOffsetMinute": 1410,
            "effective": True,
        }
    )
    assert schedule.start_offset_minute == 1260
    assert schedule.end_offset_minute == 1410
    assert schedule.effective is True

    location = LocationInfo.from_data(
        {
            "id": 7,
            "locationName": "学知苑 5 栋",
            "address": "四川省成都市郫都区",
            "longitude": 103.933883,
            "latitude": 30.75498,
            "radiusMeter": 500,
            "groupName": "宿舍区",
        }
    )
    assert location.location_id == 7
    assert location.location_name == "学知苑 5 栋"
    assert location.as_dict()["radius_meter"] == 500


def test_session_messages_are_detected():
    assert is_session_message("登录无效或已过期") is True
    assert is_session_message("请重新登录") is True
    assert is_session_message("操作成功") is False


def test_parse_json_success_and_session_expiry():
    assert _parse_json(FakeResponse(payload={"code": 0, "data": {"x": 1}}), "u") == {"x": 1}

    with pytest.raises(SessionInvalid):
        _parse_json(FakeResponse(payload={"code": 4001, "msg": "登录无效"}), "u")

    with pytest.raises(SessionInvalid):
        _parse_json(FakeResponse(status_code=302), "u")

    with pytest.raises(ApiError):
        _parse_json(FakeResponse(payload={"code": 500, "msg": "业务错误"}), "u")

    with pytest.raises(ApiError):
        _parse_json(FakeResponse(text="<html>login</html>"), "u")


def test_classify_login_verdicts():
    from app.uestc.cas import classify_login

    def response(status=200, location="", body=""):
        item = requests.Response()
        item.status_code = status
        item.url = "https://idas.uestc.edu.cn/authserver/login"
        item.headers["Location"] = location
        item._content = body.encode("utf-8")
        item.encoding = "utf-8"
        return item

    verdict, detail = classify_login(
        response(302, "https://jzsz.uestc.edu.cn/?ticket=ST-123")
    )
    assert verdict == "OK" and "ticket=ST-" in detail

    verdict, _ = classify_login(
        response(302, "https://idas.uestc.edu.cn/authserver/reAuthLoginView")
    )
    assert verdict == "MFA"

    verdict, _ = classify_login(response(200, body="<div>isMultifactor</div>"))
    assert verdict == "MFA"

    verdict, detail = classify_login(response(200, body="<div>用户名或密码错误</div>"))
    assert verdict == "FAIL" and "密码错误" in detail


def test_payload_is_json_serialisable():
    payload = build_payload(
        30.7, 103.9, "四川省成都市", "UA",
        datetime(2026, 10, 21, 21, 45, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    assert json.loads(json.dumps(payload))["latitude"] == 30.7

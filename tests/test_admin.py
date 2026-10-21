"""Offline class-cadre pagination and failure semantics."""
from __future__ import annotations

import json

import pytest
import requests

from app.uestc.admin import fetch_checkin_list, report_situations
from app.uestc.errors import ApiError, SessionInvalid
from app.uestc.http import UestcHttp


def response(payload):
    result = requests.Response()
    result.status_code = 200
    result._content = json.dumps(payload).encode("utf-8")
    return result


def test_checkin_list_preserves_page_metadata_and_opaque_ids(monkeypatch):
    pages = iter([
        {"code": 0, "data": [{"id": "2108446145967206444"}],
         "page": {"pageIndex": 1, "pageSize": 1, "totalCount": "2", "totalPage": 2}},
        {"code": 0, "data": [{"id": "2108446145967206445"}],
         "page": {"pageIndex": 2, "pageSize": 1, "totalCount": "2", "totalPage": 2}},
        {"code": 0, "data": [],
         "page": {"pageIndex": 3, "pageSize": 1, "totalCount": "2", "totalPage": 2}},
    ])
    with UestcHttp("offline-test") as client:
        monkeypatch.setattr(client.session, "request", lambda *a, **kw: response(next(pages)))
        first = fetch_checkin_list(client, "2026-10-21", "class-key", page_size=1)
        second = fetch_checkin_list(client, "2026-10-21", "class-key", page_index=2, page_size=1)
        empty = fetch_checkin_list(client, "2026-10-21", "class-key", page_index=3, page_size=1)
    assert first["data"] == [{"id": "2108446145967206444"}]
    assert second["data"] == [{"id": "2108446145967206445"}]
    assert first["page"]["totalCount"] == "2"
    assert second["page"]["pageIndex"] == 2
    assert second["page"]["totalPage"] == 2
    assert empty["data"] == []
    assert empty["page"]["totalCount"] == "2"


@pytest.mark.parametrize("payload, error", [
    ({"code": 4001, "msg": "登录无效或已过期"}, SessionInvalid),
    ({"code": 500, "msg": "无权上报该班级"}, ApiError),
])
def test_report_failure_is_not_a_success_acknowledgement(monkeypatch, payload, error):
    with UestcHttp("offline-test") as client:
        monkeypatch.setattr(client.session, "request", lambda *a, **kw: response(payload))
        with pytest.raises(error):
            report_situations(client, [{
                "classKey": "class-key", "recordId": "2108446145967206444",
                "locationTypeCode": "OUTSIDE", "caseKeywordId": "keyword-id",
                "caseRemark": "测试说明",
            }])


@pytest.fixture
def named_api(monkeypatch):
    from urllib.parse import parse_qs, urlsplit
    from app.uestc import admin

    state = {
        "classes": [{"classKey": "first"}, {"classKey": "second"}],
        "records": {
            ("first", 1): [{"id": "other", "studentSn": "20231905050330", "studentName": "同学甲"}],
            ("second", 1): [{"id": "prefix", "studentSn": "202319050503", "studentName": "同学乙"}],
            ("second", 2): [{"id": "dated-record", "studentSn": "2023190505033", "studentName": "同学丙"}],
        },
        "types": [{"label": "校内", "value": "CAMPUS"}],
        "keywords": [{"id": "study", "keyword": "学习", "remarkRequired": True}],
        "reports": [],
    }

    def request(method, url, **kwargs):
        parsed = urlsplit(url)
        query = parse_qs(parsed.query)
        if parsed.path.endswith("/my-classes"):
            data = state["classes"]
        elif parsed.path.endswith("/checkin-list"):
            key, page = query["classKey"][0], int(query["pageIndex"][0])
            data = state["records"][(key, page)]
            total = max(p for k, p in state["records"] if k == key)
            return response({"code": 0, "data": data, "page": {"totalPage": total}})
        elif parsed.path.endswith("/attachMore"):
            data = {"situationTypeOptions": state["types"]}
        elif parsed.path.endswith("/active"):
            assert query["locationTypeCode"] == ["CAMPUS"]
            data = state["keywords"]
        elif parsed.path.endswith("/report"):
            state["reports"].extend(json.loads(kwargs["data"]))
            data = True
        else:
            pytest.fail(f"Unexpected endpoint: {parsed.path}")
        return response({"code": 0, "data": data})

    with UestcHttp("offline-test") as client:
        monkeypatch.setattr(client.session, "request", request)
        yield admin, client, state


def test_named_report_resolves_exact_id_across_classes_and_pages(named_api):
    admin, client, state = named_api
    remark = "正在努力复习母猪的产后护理。"
    prepared = admin.build_situation_report(
        client, "2026-10-21", "校内", "学习", remark, student_id="2023190505033",
    )
    assert state["reports"] == []
    assert prepared == {
        "classKey": "second", "recordId": "dated-record",
        "locationTypeCode": "CAMPUS", "caseKeywordId": "study", "caseRemark": remark,
    }
    state["records"][("second", 2)][0]["id"] = "next-day-record"
    assert admin.report_student_situation(
        client, "2026-10-22", "校内", "学习", remark, student_id="2023190505033",
    ) is True
    assert state["reports"] == [{**prepared, "recordId": "next-day-record"}]


def test_duplicate_names_require_id_or_class(named_api):
    admin, client, state = named_api
    state["records"][("first", 1)][0]["studentName"] = "同学丙"
    with pytest.raises(ApiError, match="不唯一"):
        admin.report_student_situation(
            client, "2026-10-21", "校内", "学习", "说明", student_name="同学丙",
        )
    assert state["reports"] == []
    assert admin.resolve_student(
        client, "2026-10-21", student_name="同学丙", class_key="second",
    )["record"]["studentSn"] == "2023190505033"


@pytest.mark.parametrize("case", ["missing_student", "conflicting_identity", "missing_location",
                                    "duplicate_location", "missing_reason", "duplicate_reason", "blank_remark"])
def test_resolution_errors_never_submit(named_api, case):
    admin, client, state = named_api
    identity = {"student_id": "2023190505033"}
    remark = "说明"
    if case == "missing_student":
        identity = {"student_id": "2023190505"}
    elif case == "conflicting_identity":
        identity["student_name"] = "不同姓名"
    elif case == "missing_location":
        state["types"] = []
    elif case == "duplicate_location":
        state["types"] *= 2
    elif case == "missing_reason":
        state["keywords"] = []
    elif case == "duplicate_reason":
        state["keywords"] *= 2
    else:
        remark = " \t"
    with pytest.raises(ApiError):
        admin.report_student_situation(client, "2026-10-21", "校内", "学习", remark, **identity)
    assert state["reports"] == []

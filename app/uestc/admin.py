"""Class-cadre APIs captured from the daily-checkin H5 application.

Callers supply an authenticated, warmed-up ``UestcHttp`` session with the
university's class-cadre permissions. No login, retries or scheduling occur here.
Identifiers and record fields remain in their upstream form.
"""
from __future__ import annotations

from urllib.parse import urlencode

from .checkin import BASE, H5_HEADERS, _parse_envelope, _parse_json
from .errors import ApiError
from .http import UestcHttp

API_BASE = f"{BASE}/daily-checkin/api"
CHECKIN_COUNT_URL = f"{API_BASE}/class-cadre/checkin-count"
MY_CLASSES_URL = f"{API_BASE}/class-cadre/my-classes"
CHECKIN_LIST_URL = f"{API_BASE}/class-cadre/checkin-list"
ATTACH_MORE_URL = f"{API_BASE}/common/attachMore"
CASE_KEYWORDS_URL = f"{API_BASE}/case-keyword/active"
SITUATION_REPORT_URL = f"{API_BASE}/class-cadre/situation/report"


def fetch_checkin_count(client: UestcHttp, business_date: str) -> int:
    """Return the upstream count for a YYYY-MM-DD business date."""
    url = f"{CHECKIN_COUNT_URL}?{urlencode({'businessDate': business_date})}"
    response = client.request("GET", url, headers=H5_HEADERS)
    return int(_parse_json(response, url))


def fetch_my_classes(client: UestcHttp) -> list[dict]:
    """Return manageable classes, including each opaque ``classKey``."""
    response = client.request("GET", MY_CLASSES_URL, headers=H5_HEADERS)
    return _parse_json(response, MY_CLASSES_URL)


def fetch_checkin_list(
    client: UestcHttp,
    business_date: str,
    class_key: str,
    *,
    keyword: str = "",
    page_index: int = 1,
    page_size: int = 10,
) -> dict:
    """Return one full response envelope, preserving sibling ``data`` and ``page``.

    Pages are one-based. ``page`` includes ``pageIndex``, ``pageSize``,
    ``totalCount`` (a string in the capture), ``orderBy`` and ``totalPage``.
    This does not automatically fetch subsequent pages.
    """
    url = f"{CHECKIN_LIST_URL}?{urlencode({
        'businessDate': business_date,
        'classKey': class_key,
        'keyword': keyword,
        'pageIndex': page_index,
        'pageSize': page_size,
    })}"
    response = client.request("GET", url, headers=H5_HEADERS)
    return _parse_envelope(response, url)


def fetch_situation_types(client: UestcHttp) -> list[dict]:
    """Return the ``situationTypeOptions`` supplied by ``common/attachMore``."""
    response = client.request(
        "POST",
        ATTACH_MORE_URL,
        headers=H5_HEADERS,
        json_body=[{"target": "SITUATION_TYPE"}],
    )
    return _parse_json(response, ATTACH_MORE_URL)["situationTypeOptions"]


def fetch_case_keywords(client: UestcHttp, location_type_code: str) -> list[dict]:
    """Return active keywords and ``remarkRequired`` for CAMPUS or OUTSIDE."""
    url = f"{CASE_KEYWORDS_URL}?{urlencode({'locationTypeCode': location_type_code})}"
    response = client.request("GET", url, headers=H5_HEADERS)
    return _parse_json(response, url)


def report_situations(client: UestcHttp, reports: list[dict]) -> bool:
    """Submit a batch; this changes university records and is never automatic.

    Each item contains ``classKey``, ``recordId``, ``locationTypeCode``,
    ``caseKeywordId`` and ``caseRemark``. A true result is the server's
    acknowledgement, not independently verified check-in success.
    """
    response = client.request(
        "POST", SITUATION_REPORT_URL, headers=H5_HEADERS, json_body=reports,
    )
    return _parse_json(response, SITUATION_REPORT_URL)


def _unique_match(matches: list[dict], description: str) -> dict:
    if not matches:
        raise ApiError(-1, f"未找到{description}")
    if len(matches) != 1:
        raise ApiError(-1, f"{description}匹配不唯一，请提供学号或班级键以消除歧义")
    return matches[0]


def resolve_student(
    client: UestcHttp,
    business_date: str,
    *,
    student_id: str | None = None,
    student_name: str | None = None,
    class_key: str | None = None,
) -> dict:
    """Find exactly one dated record across all pages of manageable classes.

    Supply an exact student ID, name, or both (both must match). An optional
    class key restricts the search. Returns ``classKey`` and the raw ``record``.
    No fuzzy matching or persistent record-ID cache is used.
    """
    if not student_id and not student_name:
        raise ApiError(-1, "请提供学生学号或姓名")
    classes = fetch_my_classes(client)
    if class_key is not None:
        classes = [item for item in classes if item["classKey"] == class_key]
    matches = []
    seen_classes = set()
    for item in classes:
        key = item["classKey"]
        if key in seen_classes:
            continue
        seen_classes.add(key)
        page_index = 1
        while True:
            result = fetch_checkin_list(client, business_date, key, page_index=page_index)
            for record in result["data"]:
                if student_id and record.get("studentSn") != student_id:
                    continue
                if student_name and record.get("studentName") != student_name:
                    continue
                matches.append({"classKey": key, "record": record})
            if page_index >= int(result["page"]["totalPage"]):
                break
            page_index += 1
    return _unique_match(matches, "学生记录")


def build_situation_report(
    client: UestcHttp,
    business_date: str,
    location: str,
    reason: str,
    remark: str,
    *,
    student_id: str | None = None,
    student_name: str | None = None,
    class_key: str | None = None,
) -> dict:
    """Resolve exact Chinese labels and a student into a report without submitting.

    ``location`` is the situation label (e.g. 校内); ``reason`` is its keyword
    (e.g. 学习). Options are fetched from the server, never hardcoded.
    """
    student = resolve_student(
        client, business_date, student_id=student_id,
        student_name=student_name, class_key=class_key,
    )
    situation = _unique_match(
        [item for item in fetch_situation_types(client) if item["label"] == location],
        "情况类型",
    )
    keyword = _unique_match(
        [item for item in fetch_case_keywords(client, situation["value"])
         if item["keyword"] == reason],
        "情况关键词",
    )
    if keyword.get("remarkRequired") and not remark.strip():
        raise ApiError(-1, "该情况关键词要求填写说明")
    return {
        "classKey": student["classKey"],
        "recordId": student["record"]["id"],
        "locationTypeCode": situation["value"],
        "caseKeywordId": keyword["id"],
        "caseRemark": remark,
    }


def report_student_situation(
    client: UestcHttp,
    business_date: str,
    location: str,
    reason: str,
    remark: str,
    *,
    student_id: str | None = None,
    student_name: str | None = None,
    class_key: str | None = None,
) -> bool:
    """Resolve a student and labels, then submit once; changes university records.

    Resolution failures raise ``ApiError`` before any report is submitted.
    The return value is only the server's acknowledgement.
    """
    report = build_situation_report(
        client, business_date, location, reason, remark,
        student_id=student_id, student_name=student_name, class_key=class_key,
    )
    return report_situations(client, [report])

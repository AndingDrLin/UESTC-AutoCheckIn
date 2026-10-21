"""The ``daily-checkin`` micro-app API.

The portal requires a one-time warm-up request after login; without it every
business endpoint answers ``code=4001`` ("登录无效或已过期").  That warm-up is
the single most important non-obvious detail in this integration.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlencode

from .cas import now_ms
from .errors import ApiError, HttpStatusError, SessionInvalid
from .http import UestcHttp

logger = logging.getLogger(__name__)

BASE = "https://jzsz.uestc.edu.cn"
APP_ID = 294
H5_URL = f"{BASE}/daily-checkin-h5/index.html?appId={APP_ID}"
CHECK_LOGIN_URL = f"{BASE}/daily-checkin/checkLogin?appId={APP_ID}"
WARMUP_URL = f"{BASE}/api/isLoginFailure"
TODAY_URL = f"{BASE}/daily-checkin/api/student-checkin-record/today"
CHECKIN_URL = f"{BASE}/daily-checkin/api/student-checkin-record/checkin"
SCHEDULE_URL = f"{BASE}/daily-checkin/api/checkin-schedule/effective"
LOCATIONS_URL = f"{BASE}/daily-checkin/api/checkin-location/available-locations"
REVERSE_URL = f"{BASE}/daily-checkin/api/map/reverse"

H5_HEADERS = {
    "Platform": "h5",
    "Accept": "*/*",
    "Origin": BASE,
    "Referer": H5_URL,
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Dest": "empty",
}

WARMUP_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json;charset=UTF-8",
    "Origin": BASE,
    "Referer": f"{BASE}/",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Dest": "empty",
}

_SESSION_KEYWORDS = (
    "登录无效",
    "登录已过期",
    "登录过期",
    "未登录",
    "请重新登录",
    "重新登录",
    "会话失效",
    "会话过期",
    "登录状态",
)


def is_session_message(message: str) -> bool:
    text = message or ""
    return any(keyword in text for keyword in _SESSION_KEYWORDS)


def _parse_envelope(response, url: str) -> dict:
    if response.status_code in (301, 302, 401, 403):
        raise SessionInvalid(
            f"接口返回 HTTP {response.status_code}，登录态失效或微应用未授权"
        )
    if response.status_code != 200:
        raise HttpStatusError(response.status_code, url, (response.text or "")[:300])
    try:
        payload = response.json()
    except ValueError as exc:
        raise ApiError(-1, "接口返回的不是 JSON，可能被重定向到登录页") from exc
    if not isinstance(payload, dict):
        raise ApiError(-1, "接口返回结构异常")

    code = payload.get("code", 0)
    if code not in (0, "0", None):
        message = str(
            payload.get("msg")
            or payload.get("message")
            or payload.get("error")
            or "接口返回错误"
        )
        if str(code) == "4001" or is_session_message(message):
            raise SessionInvalid(message)
        raise ApiError(code, message)
    return payload


def _parse_json(response, url: str):
    return _parse_envelope(response, url).get("data")


def warm_up(client: UestcHttp):
    """Authorize the micro-app for this session (required exactly once)."""
    url = f"{WARMUP_URL}?_t={now_ms()}"
    response = client.request("GET", url, headers=WARMUP_HEADERS)
    if response.status_code in (301, 302, 401, 403):
        raise SessionInvalid("门户预热被重定向，登录态无效")
    if response.status_code != 200:
        raise HttpStatusError(response.status_code, url, (response.text or "")[:300])
    text = (response.text or "")[:500]
    if is_session_message(text):
        raise SessionInvalid("门户预热返回登录失效")
    return response


@dataclass
class AppEntry:
    reached: bool
    status: int
    url: str


def enter_app(client: UestcHttp) -> AppEntry:
    response = client.follow(CHECK_LOGIN_URL, headers=H5_HEADERS)
    if response.status_code in (401, 403):
        raise SessionInvalid("进入微应用被拒绝（HTTP 401/403）")
    reached = "/daily-checkin-h5/" in (response.url or "")
    return AppEntry(reached=reached, status=response.status_code, url=response.url)


def _text(data: dict, *keys: str, default: str = "") -> str:
    for key in keys:
        value = data.get(key)
        if value is not None and str(value) != "":
            return str(value)
    return default


def _number(data: dict, *keys: str) -> float | None:
    for key in keys:
        value = data.get(key)
        if value is None or value == "":
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _bool(data: dict, *keys: str) -> bool:
    for key in keys:
        value = data.get(key)
        if value is None:
            continue
        if isinstance(value, bool):
            return value
        return str(value).lower() in {"1", "true", "yes", "y"}
    return False


@dataclass
class TodayStatus:
    business_date: str = ""
    record_status_code: str = ""
    checkin_time: str = ""
    distance_meter: float | None = None
    abnormal_flag: bool = False
    location_name: str = ""
    latitude: float | None = None
    longitude: float | None = None
    radius_meter: float | None = None
    raw_keys: tuple[str, ...] = field(default_factory=tuple, repr=False)

    @property
    def checked_in(self) -> bool:
        return self.record_status_code.upper() == "CHECKED_IN"

    @classmethod
    def from_data(cls, data: dict) -> "TodayStatus":
        return cls(
            business_date=_text(data, "businessDate", "date"),
            record_status_code=_text(data, "recordStatusCode", "status"),
            checkin_time=_text(data, "checkinTime", "checkInTime"),
            distance_meter=_number(data, "distanceMeter", "distance"),
            abnormal_flag=_bool(data, "abnormalFlag", "abnormal"),
            location_name=_text(data, "checkinLocationName", "locationName"),
            latitude=_number(data, "checkinLocationLatitude", "latitude"),
            longitude=_number(data, "checkinLocationLongitude", "longitude"),
            radius_meter=_number(data, "checkinRadiusMeter", "radiusMeter"),
            raw_keys=tuple(sorted(data.keys())),
        )

    def safe_summary(self) -> dict:
        return {
            "business_date": self.business_date,
            "record_status_code": self.record_status_code,
            "checkin_time": self.checkin_time,
            "distance_meter": self.distance_meter,
            "abnormal_flag": self.abnormal_flag,
            "location_name": self.location_name,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "radius_meter": self.radius_meter,
        }


@dataclass
class ScheduleInfo:
    schedule_name: str = ""
    start_offset_minute: int | None = None
    end_offset_minute: int | None = None
    effective: bool = False

    @classmethod
    def from_data(cls, data: dict) -> "ScheduleInfo":
        def as_int(key: str) -> int | None:
            value = data.get(key)
            try:
                return int(value)
            except (TypeError, ValueError):
                return None

        return cls(
            schedule_name=_text(data, "scheduleName", "name"),
            start_offset_minute=as_int("studentCheckinStartOffsetMinute"),
            end_offset_minute=as_int("studentCheckinEndOffsetMinute"),
            effective=_bool(data, "effective"),
        )

    def window_text(self) -> str:
        if self.start_offset_minute is None or self.end_offset_minute is None:
            return ""
        return (
            f"{self.start_offset_minute // 60:02d}:{self.start_offset_minute % 60:02d}"
            f"-{self.end_offset_minute // 60:02d}:{self.end_offset_minute % 60:02d}"
        )


@dataclass
class LocationInfo:
    location_id: int | str = ""
    location_name: str = ""
    address: str = ""
    longitude: float | None = None
    latitude: float | None = None
    radius_meter: float | None = None
    group_id: int | str = ""
    group_name: str = ""

    @classmethod
    def from_data(cls, data: dict) -> "LocationInfo":
        return cls(
            location_id=data.get("id", ""),
            location_name=_text(data, "locationName", "name"),
            address=_text(data, "address"),
            longitude=_number(data, "longitude", "lng"),
            latitude=_number(data, "latitude", "lat"),
            radius_meter=_number(data, "radiusMeter", "radius"),
            group_id=data.get("groupId", ""),
            group_name=_text(data, "groupName"),
        )

    def as_dict(self) -> dict:
        return {
            "id": self.location_id,
            "location_name": self.location_name,
            "address": self.address,
            "longitude": self.longitude,
            "latitude": self.latitude,
            "radius_meter": self.radius_meter,
            "group_name": self.group_name,
        }


def fetch_today(client: UestcHttp) -> TodayStatus:
    data = _get_json(client, TODAY_URL)
    if not isinstance(data, dict):
        raise ApiError(-1, "今日打卡记录为空或结构异常")
    return TodayStatus.from_data(data)


def _get_json(client: UestcHttp, url: str):
    response = client.request("GET", url, headers=H5_HEADERS)
    return _parse_json(response, url)


def fetch_schedule(client: UestcHttp) -> ScheduleInfo:
    data = _get_json(client, SCHEDULE_URL)
    if not isinstance(data, dict):
        return ScheduleInfo()
    return ScheduleInfo.from_data(data)


def fetch_locations(client: UestcHttp) -> list[LocationInfo]:
    data = _get_json(client, LOCATIONS_URL)
    if isinstance(data, dict):
        items = data.get("list") or data.get("records") or data.get("data") or []
    else:
        items = data or []
    if not isinstance(items, list):
        return []
    return [LocationInfo.from_data(item) for item in items if isinstance(item, dict)]


def reverse_geocode(client: UestcHttp, latitude: float, longitude: float) -> str:
    url = f"{REVERSE_URL}?{urlencode({'lat': latitude, 'lng': longitude})}"
    data = _get_json(client, url)
    if isinstance(data, str):
        return data
    if isinstance(data, dict):
        for key in ("address", "reverseGeocode", "formattedAddress", "name"):
            value = data.get(key)
            if value:
                return str(value)
    return ""


def submit(client: UestcHttp, payload: dict) -> object:
    response = client.request(
        "POST",
        CHECKIN_URL,
        headers={**H5_HEADERS, "Content-Type": "application/json"},
        json_body=payload,
    )
    return _parse_json(response, CHECKIN_URL)


def build_payload(
    latitude: float,
    longitude: float,
    address: str,
    user_agent: str,
    when: datetime,
    *,
    accuracy: int = 86,
    location_duration_ms: int = 1189,
) -> dict:
    """Build the exact JSON body the H5 page sends.

    Do not "clean up" the empty-string fields: they are part of the captured
    request and the server accepts them verbatim.
    """
    return {
        "latitude": round(float(latitude), 6),
        "longitude": round(float(longitude), 6),
        "reverseGeocode": address or "四川省成都市",
        "clientTime": when.strftime("%Y-%m-%d %H:%M:%S"),
        "userAgent": user_agent,
        "coordinateSystemCode": "gcj02",
        "accuracy": accuracy,
        "locationDurationMs": location_duration_ms,
        "networkTypeCode": "4g",
        "locationEnabled": False,
        "locationAuthorized": "",
        "locationAccuracy": "",
        "errorMessage": "",
    }

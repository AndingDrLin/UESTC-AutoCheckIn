"""High-level check-in orchestration.

``CheckinService`` is deliberately independent of FastAPI and of the scheduler:
the web UI, the CLI and the background thread all call :meth:`run`.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from ..config import Settings
from ..db import Database
from ..notifier import Notifier
from ..security import SecretBox, SecretMismatch
from . import cas, checkin
from .errors import (
    ApiError,
    HttpStatusError,
    LoginFailed,
    MFARequired,
    NetworkError,
    PageChanged,
    ServiceError,
    SessionInvalid,
)
from .http import UestcHttp

logger = logging.getLogger(__name__)


class RetryableError(Exception):
    """Transient condition; the scheduler should try again later."""


@dataclass
class Outcome:
    ok: bool
    fatal: bool = False
    message: str = ""
    detail: dict = field(default_factory=dict)
    record_status: str = ""
    verified: bool = False
    warning: bool = False

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "fatal": self.fatal,
            "message": self.message,
            "detail": self.detail,
            "record_status": self.record_status,
            "verified": self.verified,
            "warning": self.warning,
        }


def _looks_like_already_checked_in(message: str) -> bool:
    return any(
        keyword in (message or "")
        for keyword in ("已打卡", "已签到", "已经打卡", "重复打卡", "请勿重复", "已完成")
    )


def _is_fatal_submit_error(message: str) -> bool:
    text = message or ""
    # Wrong coordinates will never fix themselves during the window.
    if any(keyword in text for keyword in ("范围", "距离", "定位", "坐标")):
        return True
    if any(keyword in text for keyword in ("学号", "账号不存在", "用户不存在", "账号异常")):
        return True
    if "异常" in text:
        return True
    return False


class CheckinService:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        box: SecretBox,
        notifier: Notifier | None = None,
        *,
        client_factory: Callable[[dict], UestcHttp] | None = None,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings
        self.db = db
        self.box = box
        self.notifier = notifier
        self._client_factory = client_factory or self._default_client
        self._now = now or (lambda: datetime.now(self.settings.tz))
        self._sleep = sleep

    # -- helpers ------------------------------------------------------------
    def _default_client(self, user: dict) -> UestcHttp:
        client = UestcHttp(
            user_agent=self.settings.user_agent,
            timeout=self.settings.http_timeout,
        )
        if user.get("fingerprint"):
            cas.set_fingerprint_cookie(client, user["fingerprint"])
        return client

    def _log(
        self,
        user: dict,
        level: str,
        event: str,
        message: str,
        detail: dict | None = None,
    ) -> None:
        self.db.add_log(
            message,
            user_id=user.get("id"),
            user_name=user.get("name", ""),
            level=level,
            event=event,
            detail=detail,
        )
        log = logger.info if level.upper() in {"INFO", "DEBUG"} else logger.warning
        log("[%s] %s", event, message)

    def _restore_session(self, user: dict, client: UestcHttp) -> bool:
        row = self.db.get_session(int(user["id"]))
        if not row or row.get("invalid") or not row.get("cookies_enc"):
            return False
        try:
            cookies = json.loads(self.box.decrypt(row["cookies_enc"]))
        except (SecretMismatch, ValueError, json.JSONDecodeError) as exc:
            raise ServiceError(str(exc)) from exc
        session = cookies.get("SESSION") if isinstance(cookies, dict) else None
        if not session:
            return False
        cas.inject_session(client, session)
        return True

    def _save_session(self, user: dict, client: UestcHttp) -> None:
        cookies = client.export_cookies()
        if not cookies.get("SESSION"):
            return
        encrypted = self.box.encrypt(
            json.dumps(cookies, separators=(",", ":"), ensure_ascii=False)
        )
        self.db.save_session(int(user["id"]), encrypted)

    def _login_allowed(self, user: dict, now: datetime) -> tuple[bool, str]:
        return self.db.can_login(
            int(user["id"]),
            now.date().isoformat(),
            int(now.timestamp()),
            self.settings.login_cooldown_sec,
            self.settings.max_logins_per_day,
        )

    def _perform_login(self, user: dict, client: UestcHttp, now: datetime) -> None:
        allowed, reason = self._login_allowed(user, now)
        if not allowed:
            raise RetryableError(reason)
        try:
            password = self.box.decrypt(user["password_enc"])
        except SecretMismatch as exc:
            raise ServiceError(str(exc)) from exc

        try:
            cas.login(
                client,
                user["student_id"],
                password,
                user.get("fingerprint", ""),
            )
        except (MFARequired, LoginFailed, PageChanged) as exc:
            raise ServiceError(str(exc)) from exc

        self._save_session(user, client)
        self.db.record_login(
            int(user["id"]), now.date().isoformat(), int(now.timestamp())
        )
        self._log(user, "INFO", "login", "统一身份认证登录成功")

        try:
            checkin.warm_up(client)
            self.db.mark_warmup(int(user["id"]), int(now.timestamp()))
        except SessionInvalid as exc:
            raise RetryableError(f"登录成功，但门户预热失败：{exc}") from exc
        except HttpStatusError as exc:
            raise RetryableError(f"登录成功，但门户预热返回 HTTP {exc.status}") from exc

    def _prepare(
        self, user: dict, client: UestcHttp, now: datetime
    ) -> tuple[checkin.TodayStatus, bool]:
        """Return today's record plus whether a fresh login was used."""
        if self._restore_session(user, client):
            try:
                checkin.warm_up(client)
                entry = checkin.enter_app(client)
                if not entry.reached:
                    raise SessionInvalid("微应用未放行（checkLogin 未跳转到 H5）")
                today = checkin.fetch_today(client)
                logger.debug("reused cached session for %s", user.get("name"))
                return today, False
            except SessionInvalid:
                self.db.mark_session_invalid(int(user["id"]))
                client.clear_cookie("SESSION")
            except NetworkError:
                raise
            except HttpStatusError as exc:
                self.db.mark_session_invalid(int(user["id"]))
                client.clear_cookie("SESSION")
                logger.info("cached session HTTP %s, will re-login", exc.status)

        self._perform_login(user, client, now)

        entry = checkin.enter_app(client)
        if not entry.reached:
            # A fresh warm-up is cheap; retry the authorization once.
            checkin.warm_up(client)
            entry = checkin.enter_app(client)
        if not entry.reached:
            raise RetryableError(
                "登录成功，但微应用仍未放行（checkLogin 未跳转到 H5），稍后重试"
            )
        return checkin.fetch_today(client), True

    def _address_for(
        self, user: dict, client: UestcHttp, latitude: float, longitude: float
    ) -> str:
        address = (user.get("address") or "").strip()
        if address and not self.settings.reverse_geocode:
            return address
        try:
            resolved = checkin.reverse_geocode(client, latitude, longitude)
            if resolved:
                return resolved
        except (NetworkError, ApiError, SessionInvalid, HttpStatusError) as exc:
            logger.debug("reverse geocode failed: %s", exc)
        return address or "四川省成都市"

    def _verify(self, client: UestcHttp) -> tuple[bool, checkin.TodayStatus | None]:
        last: checkin.TodayStatus | None = None
        for delay in (0, 2, 5):
            if delay:
                self._sleep(delay)
            try:
                last = checkin.fetch_today(client)
            except SessionInvalid:
                break
            except (NetworkError, ApiError, HttpStatusError):
                continue
            if last.checked_in:
                return True, last
        return False, last

    # -- public API ---------------------------------------------------------
    def run(self, user: dict, manual: bool = False) -> Outcome:
        try:
            return self._run_inner(user, manual)
        except ServiceError as exc:
            self._log(user, "ERROR", "fatal", str(exc))
            return Outcome(False, True, str(exc))
        except RetryableError as exc:
            self._log(user, "WARNING", "retry", str(exc))
            return Outcome(False, False, str(exc))
        except SecretMismatch as exc:
            self._log(user, "ERROR", "fatal", str(exc))
            return Outcome(False, True, str(exc))
        except NetworkError as exc:
            self._log(user, "WARNING", "network", f"网络错误：{exc}")
            return Outcome(False, False, f"网络错误：{exc}")
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("unexpected check-in error")
            self._log(user, "ERROR", "error", f"未预期错误：{exc}")
            return Outcome(False, False, f"未预期错误：{type(exc).__name__}: {exc}")

    def _run_inner(self, user: dict, manual: bool) -> Outcome:
        now = self._now()
        client = self._client_factory(user)
        try:
            try:
                today, login_used = self._prepare(user, client, now)
            except RetryableError as exc:
                return Outcome(False, False, str(exc), {"stage": "prepare"})
            except (ServiceError, MFARequired, LoginFailed, PageChanged) as exc:
                return Outcome(False, True, str(exc), {"stage": "prepare"})
            except NetworkError:
                raise
            except SessionInvalid as exc:
                return Outcome(
                    False, False, f"会话失效且未能重新登录：{exc}", {"stage": "prepare"}
                )
            except (HttpStatusError, ApiError) as exc:
                return Outcome(
                    False, False, f"读取打卡信息失败：{exc}", {"stage": "prepare"}
                )

            if today.checked_in:
                summary = today.safe_summary()
                message = "今日已打卡"
                if today.checkin_time:
                    message += f"（{today.checkin_time}）"
                if today.abnormal_flag:
                    message += "，平台标记为异常，请人工确认"
                self._log(user, "INFO", "already", message, summary)
                return Outcome(
                    True,
                    False,
                    message,
                    summary,
                    record_status=today.record_status_code,
                    verified=True,
                    warning=today.abnormal_flag,
                )

            schedule = None
            try:
                schedule = checkin.fetch_schedule(client)
            except (NetworkError, ApiError, HttpStatusError, SessionInvalid) as exc:
                logger.debug("schedule lookup failed: %s", exc)

            latitude = float(user["latitude"])
            longitude = float(user["longitude"])
            address = self._address_for(user, client, latitude, longitude)
            payload = checkin.build_payload(
                latitude,
                longitude,
                address,
                self.settings.user_agent,
                self._now(),
            )

            try:
                result = checkin.submit(client, payload)
            except SessionInvalid as exc:
                self.db.mark_session_invalid(int(user["id"]))
                self._log(user, "WARNING", "session", f"提交时会话失效：{exc}")
                return Outcome(
                    False,
                    False,
                    "提交打卡时会话失效，将在下一轮重试",
                    {"stage": "submit"},
                )
            except ApiError as exc:
                if _looks_like_already_checked_in(exc.message):
                    verified, after = self._verify(client)
                    if verified and after is not None:
                        return Outcome(
                            True,
                            False,
                            "今日已打卡",
                            after.safe_summary(),
                            record_status=after.record_status_code,
                            verified=True,
                            warning=after.abnormal_flag,
                        )
                fatal = _is_fatal_submit_error(exc.message)
                self._log(
                    user,
                    "ERROR" if fatal else "WARNING",
                    "submit",
                    f"提交打卡失败：{exc.message}",
                    {"code": exc.code},
                )
                return Outcome(
                    False,
                    fatal,
                    f"提交打卡失败：{exc.message}",
                    {"stage": "submit", "code": exc.code},
                )
            except NetworkError as exc:
                self._log(user, "WARNING", "submit", f"提交打卡网络错误：{exc}")
                return Outcome(
                    False, False, f"提交打卡网络错误：{exc}", {"stage": "submit"}
                )
            except HttpStatusError as exc:
                return Outcome(
                    False, False, f"提交打卡异常：{exc}", {"stage": "submit"}
                )

            if result is False:
                return Outcome(
                    False, False, "提交接口返回 data=false，稍后重试", {"stage": "submit"}
                )

            verified, after = self._verify(client)
            if verified and after is not None:
                summary = after.safe_summary()
                message = "打卡成功"
                if after.checkin_time:
                    message += f"：{after.checkin_time}"
                if after.distance_meter is not None:
                    message += f"，距打卡点 {after.distance_meter:.1f} 米"
                warning = after.abnormal_flag
                if warning:
                    message += "；平台标记为异常，请人工确认"
                self._log(
                    user, "WARNING" if warning else "INFO", "success", message, summary
                )
                return Outcome(
                    True,
                    False,
                    message,
                    summary,
                    record_status=after.record_status_code,
                    verified=True,
                    warning=warning,
                )

            # The submit call said code=0 but the read-back did not confirm it.
            self._log(
                user,
                "WARNING",
                "unverified",
                "提交接口返回成功，但复查未确认",
                {"schedule": schedule.window_text() if schedule else ""},
            )
            return Outcome(
                True,
                False,
                "提交接口返回成功，但复查未确认，请在平台核对",
                {"stage": "verify"},
                verified=False,
                warning=True,
            )
        finally:
            client.close()

    def test(self, user: dict) -> Outcome:
        """Dry-run the whole chain without submitting a check-in."""
        now = self._now()
        client = self._client_factory(user)
        try:
            today, login_used = self._prepare(user, client, now)
            schedule = checkin.fetch_schedule(client)
            locations = checkin.fetch_locations(client)
            detail = {
                "session_valid": True,
                "login_used": login_used,
                "today": today.safe_summary(),
                "schedule": {
                    "name": schedule.schedule_name,
                    "window": schedule.window_text(),
                    "effective": schedule.effective,
                },
                "locations": [loc.as_dict() for loc in locations[:100]],
                "location_count": len(locations),
            }
            message = "链路测试成功"
            if login_used:
                message += "（本次使用了统一身份认证登录）"
            self._log(user, "INFO", "test", message, {"login_used": login_used})
            return Outcome(True, False, message, detail)
        except ServiceError as exc:
            return Outcome(False, True, str(exc))
        except RetryableError as exc:
            return Outcome(False, False, str(exc))
        except NetworkError as exc:
            return Outcome(False, False, f"网络错误：{exc}")
        except (MFARequired, LoginFailed, PageChanged) as exc:
            return Outcome(False, True, str(exc))
        except SessionInvalid as exc:
            return Outcome(False, False, f"会话无效：{exc}")
        except Exception as exc:  # pragma: no cover
            logger.exception("unexpected test error")
            return Outcome(False, False, f"未预期错误：{type(exc).__name__}: {exc}")
        finally:
            client.close()

    def fetch_locations(self, user: dict) -> list[dict]:
        now = self._now()
        client = self._client_factory(user)
        try:
            self._prepare(user, client, now)
            locations = checkin.fetch_locations(client)
            return [loc.as_dict() for loc in locations]
        except ServiceError:
            raise
        except RetryableError as exc:
            raise ServiceError(str(exc)) from exc
        except NetworkError as exc:
            raise ServiceError(f"网络错误：{exc}") from exc
        except (MFARequired, LoginFailed, PageChanged) as exc:
            raise ServiceError(str(exc)) from exc
        except SessionInvalid as exc:
            raise ServiceError(f"会话无效：{exc}") from exc
        except (ApiError, HttpStatusError) as exc:
            raise ServiceError(f"拉取打卡点失败：{exc}") from exc
        finally:
            client.close()

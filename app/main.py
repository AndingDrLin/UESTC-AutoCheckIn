"""FastAPI application: admin UI, JSON helpers and the process lifecycle."""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import math
from contextlib import asynccontextmanager
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from .config import (
    BASE_DIR,
    PLATFORM_WINDOW_END,
    PLATFORM_WINDOW_START,
    Settings,
    load_settings,
    parse_hhmm,
    setup_logging,
)
from .db import Database, DuplicateStudentId
from .notifier import Notifier
from .scheduler import CheckinScheduler
from .security import (
    SESSION_COOKIE,
    DEFAULT_SESSION_TTL,
    SecretBox,
    check_admin_password,
    new_session,
    verify_session,
)
from .uestc.errors import ServiceError
from .uestc.service import CheckinService, Outcome

logger = logging.getLogger(__name__)

STATUS_TEXT = {
    "pending": "待执行",
    "running": "执行中",
    "success": "成功",
    "failed": "失败",
    "missed": "错过",
    "none": "未生成",
}
STATUS_CLASS = {
    "pending": "warn",
    "running": "info",
    "success": "ok",
    "failed": "bad",
    "missed": "muted",
    "none": "muted",
}
WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


class LoginRequired(Exception):
    """Raised when an HTML page is requested without a valid admin session."""


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def platform_is_open(now: datetime) -> bool:
    current = now.time()
    return PLATFORM_WINDOW_START <= current <= PLATFORM_WINDOW_END


def public_user(user: dict) -> dict:
    data = {key: value for key, value in user.items() if key != "password_enc"}
    data["has_password"] = bool(user.get("password_enc"))
    return data


def _local_datetime(value: Any, tz) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz)
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(tz)


def redirect_with(url: str, message: str = "", level: str = "ok") -> RedirectResponse:
    if message:
        separator = "&" if "?" in url else "?"
        url = f"{url}{separator}msg={quote(message[:400])}&level={quote(level)}"
    return RedirectResponse(url, status_code=303)


def check_csrf(auth: dict, token: str) -> None:
    expected = str(auth.get("csrf") or "")
    if not token or not expected or not hmac.compare_digest(str(token), expected):
        raise HTTPException(status_code=400, detail="表单已过期，请刷新页面后重试")


def _parse_float(value: str) -> float | None:
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def validate_user_fields(raw: dict[str, str], *, require_password: bool) -> tuple[dict, str, list[str]]:
    """Validate an HTML form.  Returns (typed fields, plain password, errors)."""
    errors: list[str] = []

    name = (raw.get("name") or "").strip()
    if not (1 <= len(name) <= 64):
        errors.append("姓名长度需在 1–64 个字符之间")

    student_id = (raw.get("student_id") or "").strip()
    if not student_id:
        errors.append("请填写学号")
    elif not all(ch.isalnum() for ch in student_id) or not (4 <= len(student_id) <= 32):
        errors.append("学号应为 4–32 位字母或数字")

    password = raw.get("password") or ""
    if require_password and not password:
        errors.append("请填写统一身份认证密码")
    if len(password) > 128:
        errors.append("密码过长（最多 128 个字符）")

    fingerprint = (raw.get("fingerprint") or "").strip().upper().replace(" ", "")
    if fingerprint and (len(fingerprint) != 32 or any(c not in "0123456789ABCDEF" for c in fingerprint)):
        errors.append("浏览器指纹应为 32 位十六进制字符（0–9 / A–F）")

    latitude = _parse_float(raw.get("latitude", ""))
    longitude = _parse_float(raw.get("longitude", ""))
    if latitude is None or not (-90 <= latitude <= 90):
        errors.append("纬度应为 -90 到 90 之间的数字")
    if longitude is None or not (-180 <= longitude <= 180):
        errors.append("经度应为 -180 到 180 之间的数字")
    if latitude is not None and longitude is not None and abs(latitude) < 1e-9 and abs(longitude) < 1e-9:
        errors.append("请填写真实的宿舍坐标（0,0 不是有效位置）")

    window_start = None
    window_end = None
    try:
        window_start = parse_hhmm(raw.get("window_start", ""))
    except ValueError:
        errors.append("签到开始时间格式应为 HH:MM")
    try:
        window_end = parse_hhmm(raw.get("window_end", ""))
    except ValueError:
        errors.append("签到结束时间格式应为 HH:MM")
    if window_start and window_end and window_end <= window_start:
        errors.append("签到结束时间必须晚于开始时间")

    notify_url = (raw.get("notify_url") or "").strip()
    if notify_url and not (notify_url.startswith("http://") or notify_url.startswith("https://")):
        errors.append("通知 Webhook 必须以 http:// 或 https:// 开头")

    dorm_building = (raw.get("dorm_building") or "").strip()[:100]
    dorm_room = (raw.get("dorm_room") or "").strip()[:50]
    address = (raw.get("address") or "").strip()[:200]
    notes = (raw.get("notes") or "").strip()[:500]

    if errors:
        return {}, password, errors

    fields = {
        "name": name,
        "student_id": student_id,
        "fingerprint": fingerprint,
        "dorm_building": dorm_building,
        "dorm_room": dorm_room,
        "address": address,
        "latitude": round(float(latitude), 6),
        "longitude": round(float(longitude), 6),
        "window_start": window_start.strftime("%H:%M"),
        "window_end": window_end.strftime("%H:%M"),
        "notify_url": notify_url[:500],
        "notes": notes,
        "enabled": 1,
    }
    return fields, password, []


def _safe_session_info(row: dict | None) -> dict | None:
    if not row:
        return None
    return {
        "saved_at": row.get("saved_at"),
        "last_login_epoch": row.get("last_login_epoch"),
        "last_warmup_epoch": row.get("last_warmup_epoch"),
        "invalid": bool(row.get("invalid")),
        "logins_date": row.get("logins_date"),
        "logins_today": row.get("logins_today"),
    }


# ---------------------------------------------------------------------------
# application factory
# ---------------------------------------------------------------------------
def create_app(settings: Settings | None = None) -> FastAPI:
    cfg = settings or load_settings()
    setup_logging(cfg.log_level)

    db = Database(cfg.db_path)
    box = SecretBox(cfg.app_secret)
    notifier = Notifier(cfg)
    service = CheckinService(cfg, db, box, notifier)
    scheduler = CheckinScheduler(cfg, db, service, notifier)

    templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))
    templates.env.filters["localtime"] = lambda value: (
        _local_datetime(value, cfg.tz).strftime("%Y-%m-%d %H:%M:%S")
        if _local_datetime(value, cfg.tz)
        else "—"
    )
    templates.env.filters["hm"] = lambda value: (
        _local_datetime(value, cfg.tz).strftime("%H:%M:%S")
        if _local_datetime(value, cfg.tz)
        else "—"
    )
    templates.env.globals["app_version"] = cfg.version

    def current_session(request: Request) -> dict | None:
        token = request.cookies.get(SESSION_COOKIE)
        if not token:
            return None
        return verify_session(cfg.app_secret, token)

    def require_admin(request: Request) -> dict:
        auth = current_session(request)
        if not auth:
            raise LoginRequired()
        return auth

    def require_api_admin(request: Request) -> dict:
        auth = current_session(request)
        if not auth:
            raise HTTPException(status_code=401, detail="登录已过期，请重新登录")
        return auth

    def render(
        request: Request,
        name: str,
        *,
        auth: dict | None = None,
        msg: str = "",
        level: str = "ok",
        status_code: int = 200,
        **context: Any,
    ):
        base = {
            "request": request,
            "session": auth,
            "csrf": (auth or {}).get("csrf", ""),
            "msg": msg,
            "level": level,
            "now": datetime.now(cfg.tz),
            "app_version": cfg.version,
            "settings": cfg.public_dict(),
            "platform_window": (
                f"{PLATFORM_WINDOW_START.strftime('%H:%M')}–"
                f"{PLATFORM_WINDOW_END.strftime('%H:%M')}"
            ),
        }
        base.update(context)
        return templates.TemplateResponse(request, name, base, status_code=status_code)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        scheduler.start()
        try:
            yield
        finally:
            scheduler.stop()

    app = FastAPI(
        title="UESTC 晚安成电 · 自动打卡",
        version=cfg.version,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )
    app.state.settings = cfg
    app.state.db = db
    app.state.box = box
    app.state.service = service
    app.state.scheduler = scheduler

    static_dir = BASE_DIR / "app" / "static"
    static_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.exception_handler(LoginRequired)
    async def _login_required(_: Request, __: LoginRequired):
        return RedirectResponse("/login", status_code=303)

    # -- authentication -----------------------------------------------------
    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request):
        if current_session(request):
            return RedirectResponse("/", status_code=303)
        return render(request, "login.html")

    @app.post("/login", response_class=HTMLResponse)
    async def login_submit(request: Request, password: str = Form("")):
        if not check_admin_password(cfg.admin_password, password):
            await asyncio.sleep(0.6)
            return render(
                request,
                "login.html",
                msg="管理密码错误，请重试",
                level="error",
                status_code=401,
            )
        token, _ = new_session(cfg.app_secret)
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            SESSION_COOKIE,
            token,
            max_age=DEFAULT_SESSION_TTL,
            httponly=True,
            samesite="lax",
            secure=cfg.cookie_secure,
            path="/",
        )
        db.add_log("管理员登录", level="INFO", event="admin.login")
        return response

    @app.post("/logout")
    def logout(auth: dict = Depends(require_admin), csrf: str = Form("", alias="_csrf")):
        check_csrf(auth, csrf)
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    # -- dashboard ----------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def dashboard(
        request: Request,
        auth: dict = Depends(require_admin),
        msg: str = "",
        level: str = "ok",
    ):
        now = datetime.now(cfg.tz)
        date_str = now.date().isoformat()
        users = db.list_users()
        plans = {int(p["user_id"]): p for p in db.list_plans_for_date(date_str)}
        rows: list[dict] = []
        for user in users:
            plan = plans.get(int(user["id"]))
            result: dict = {}
            if plan and plan.get("result"):
                try:
                    result = json.loads(plan["result"])
                except (TypeError, ValueError):
                    result = {}
            status = plan["status"] if plan else "none"
            rows.append(
                {
                    "user": public_user(user),
                    "plan": plan,
                    "result": result,
                    "status": status,
                    "status_text": STATUS_TEXT.get(status, status),
                    "status_class": STATUS_CLASS.get(status, "muted"),
                    "planned_time": plan["planned_time"] if plan else "—",
                    "last_error": (plan or {}).get("last_error", ""),
                    "session_info": _safe_session_info(db.get_session(int(user["id"]))),
                    "can_checkin": bool(user["enabled"]) and platform_is_open(now),
                }
            )

        stats = {
            "total": len(rows),
            "enabled": sum(1 for row in rows if row["user"]["enabled"]),
            "success": sum(1 for row in rows if row["status"] == "success"),
            "pending": sum(1 for row in rows if row["status"] in ("pending", "running")),
            "failed": sum(1 for row in rows if row["status"] in ("failed", "missed")),
        }
        pending_rows = [
            row
            for row in rows
            if row["plan"] and row["status"] in ("pending", "running")
        ]
        next_row = min(
            pending_rows,
            key=lambda row: int(row["plan"]["planned_epoch"]),
            default=None,
        )
        return render(
            request,
            "dashboard.html",
            auth=auth,
            msg=msg,
            level=level,
            rows=rows,
            stats=stats,
            next_row=next_row,
            today_label=f"{now.strftime('%Y-%m-%d')} {WEEKDAYS[now.weekday()]}",
            now_text=now.strftime("%H:%M:%S"),
            platform_open=platform_is_open(now),
            scheduler_status=scheduler.status(),
            default_password=cfg.is_default_password,
        )

    # -- users --------------------------------------------------------------
    def _new_user_defaults() -> dict:
        return {
            "id": None,
            "name": "",
            "student_id": "",
            "fingerprint": "",
            "dorm_building": "",
            "dorm_room": "",
            "address": "",
            "latitude": "",
            "longitude": "",
            "window_start": cfg.window_start.strftime("%H:%M"),
            "window_end": cfg.window_end.strftime("%H:%M"),
            "notify_url": "",
            "notes": "",
            "enabled": 1,
            "has_password": False,
        }

    @app.get("/users/new", response_class=HTMLResponse)
    def user_new(request: Request, auth: dict = Depends(require_admin)):
        return render(
            request,
            "user_form.html",
            auth=auth,
            mode="new",
            user=_new_user_defaults(),
            errors=[],
            action="/users/new",
        )

    @app.post("/users/new", response_class=HTMLResponse)
    def user_create(
        request: Request,
        auth: dict = Depends(require_admin),
        csrf: str = Form("", alias="_csrf"),
        name: str = Form(""),
        student_id: str = Form(""),
        password: str = Form(""),
        fingerprint: str = Form(""),
        dorm_building: str = Form(""),
        dorm_room: str = Form(""),
        address: str = Form(""),
        latitude: str = Form(""),
        longitude: str = Form(""),
        window_start: str = Form("21:30"),
        window_end: str = Form("23:00"),
        notify_url: str = Form(""),
        notes: str = Form(""),
        enabled: str = Form("0"),
    ):
        check_csrf(auth, csrf)
        raw = {
            "name": name,
            "student_id": student_id,
            "password": password,
            "fingerprint": fingerprint,
            "dorm_building": dorm_building,
            "dorm_room": dorm_room,
            "address": address,
            "latitude": latitude,
            "longitude": longitude,
            "window_start": window_start,
            "window_end": window_end,
            "notify_url": notify_url,
            "notes": notes,
        }
        fields, plain_password, errors = validate_user_fields(
            raw, require_password=True
        )
        fields["enabled"] = 1 if enabled in ("1", "on", "true", "yes") else 0
        if not errors:
            fields["password_enc"] = box.encrypt(plain_password)
            try:
                user_id = db.create_user(**fields)
            except DuplicateStudentId:
                errors.append("该学号已存在，请勿重复添加")
            else:
                db.add_log(
                    f"新增用户 {fields['name']}",
                    user_id=user_id,
                    user_name=fields["name"],
                    event="user.create",
                )
                scheduler.wake()
                return redirect_with("/", f"已添加 {fields['name']}，今晚将自动安排打卡")
        form_user = {"id": None, **raw, "enabled": 1 if enabled in ("1", "on", "true", "yes") else 0}
        return render(
            request,
            "user_form.html",
            auth=auth,
            mode="new",
            user=form_user,
            errors=errors,
            action="/users/new",
            status_code=400,
        )

    @app.get("/users/{user_id}/edit", response_class=HTMLResponse)
    def user_edit(user_id: int, request: Request, auth: dict = Depends(require_admin)):
        user = db.get_user(user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="用户不存在")
        return render(
            request,
            "user_form.html",
            auth=auth,
            mode="edit",
            user=public_user(user),
            errors=[],
            action=f"/users/{user_id}/edit",
        )

    @app.post("/users/{user_id}/edit", response_class=HTMLResponse)
    def user_update(
        user_id: int,
        request: Request,
        auth: dict = Depends(require_admin),
        csrf: str = Form("", alias="_csrf"),
        name: str = Form(""),
        student_id: str = Form(""),
        password: str = Form(""),
        fingerprint: str = Form(""),
        dorm_building: str = Form(""),
        dorm_room: str = Form(""),
        address: str = Form(""),
        latitude: str = Form(""),
        longitude: str = Form(""),
        window_start: str = Form("21:30"),
        window_end: str = Form("23:00"),
        notify_url: str = Form(""),
        notes: str = Form(""),
        enabled: str = Form("0"),
    ):
        check_csrf(auth, csrf)
        existing = db.get_user(user_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="用户不存在")
        raw = {
            "name": name,
            "student_id": student_id,
            "password": password,
            "fingerprint": fingerprint,
            "dorm_building": dorm_building,
            "dorm_room": dorm_room,
            "address": address,
            "latitude": latitude,
            "longitude": longitude,
            "window_start": window_start,
            "window_end": window_end,
            "notify_url": notify_url,
            "notes": notes,
        }
        fields, plain_password, errors = validate_user_fields(
            raw, require_password=False
        )
        fields["enabled"] = 1 if enabled in ("1", "on", "true", "yes") else 0
        if not errors:
            if plain_password:
                fields["password_enc"] = box.encrypt(plain_password)
            try:
                db.update_user(user_id, **fields)
            except DuplicateStudentId:
                errors.append("该学号已被其他用户使用")
            else:
                # Credentials or schedule changed: force a fresh session/plan.
                if plain_password or fields["student_id"] != existing["student_id"]:
                    db.clear_session(user_id)
                today = datetime.now(cfg.tz).date().isoformat()
                db.delete_pending_plan(user_id, today)
                db.add_log(
                    f"更新用户 {fields['name']}",
                    user_id=user_id,
                    user_name=fields["name"],
                    event="user.update",
                )
                scheduler.wake()
                return redirect_with("/", f"已保存 {fields['name']} 的配置")
        form_user = {
            "id": user_id,
            **raw,
            "enabled": 1 if enabled in ("1", "on", "true", "yes") else 0,
            "has_password": bool(existing.get("password_enc")),
        }
        return render(
            request,
            "user_form.html",
            auth=auth,
            mode="edit",
            user=form_user,
            errors=errors,
            action=f"/users/{user_id}/edit",
            status_code=400,
        )

    @app.post("/users/{user_id}/delete")
    def user_delete(
        user_id: int,
        auth: dict = Depends(require_admin),
        csrf: str = Form("", alias="_csrf"),
    ):
        check_csrf(auth, csrf)
        user = db.get_user(user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="用户不存在")
        db.delete_user(user_id)
        db.add_log(
            f"删除用户 {user['name']}",
            user_id=user_id,
            user_name=user["name"],
            event="user.delete",
        )
        return redirect_with("/", f"已删除 {user['name']} 及其计划")

    @app.post("/users/{user_id}/toggle")
    def user_toggle(
        user_id: int,
        auth: dict = Depends(require_admin),
        csrf: str = Form("", alias="_csrf"),
    ):
        check_csrf(auth, csrf)
        user = db.get_user(user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="用户不存在")
        new_value = 0 if int(user["enabled"]) else 1
        db.update_user(user_id, enabled=new_value)
        if not new_value:
            today = datetime.now(cfg.tz).date().isoformat()
            db.delete_pending_plan(user_id, today)
        else:
            scheduler.wake()
        db.add_log(
            f"{'启用' if new_value else '停用'}用户 {user['name']}",
            user_id=user_id,
            user_name=user["name"],
            event="user.toggle",
        )
        return redirect_with("/", f"已{'启用' if new_value else '停用'} {user['name']}")

    # -- check-in actions ---------------------------------------------------
    @app.post("/users/{user_id}/checkin")
    async def manual_checkin(
        user_id: int,
        request: Request,
        auth: dict = Depends(require_admin),
        csrf: str = Form("", alias="_csrf"),
    ):
        check_csrf(auth, csrf)
        user = db.get_user(user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="用户不存在")
        if not int(user["enabled"]):
            return redirect_with("/", "该用户已停用", "error")
        now = datetime.now(cfg.tz)
        if not platform_is_open(now):
            return redirect_with(
                "/",
                f"当前不在平台打卡时段（{PLATFORM_WINDOW_START.strftime('%H:%M')}–"
                f"{PLATFORM_WINDOW_END.strftime('%H:%M')}），已拒绝提交",
                "error",
            )
        outcome: Outcome = await run_in_threadpool(service.run, user, True)
        if outcome.ok:
            plan = db.get_plan(user_id, now.date().isoformat())
            if plan is not None:
                db.update_plan(
                    int(plan["id"]),
                    status="success",
                    next_attempt_epoch=None,
                    last_error="",
                    result=json.dumps(outcome.to_dict(), ensure_ascii=False),
                )
        db.add_log(
            f"手动打卡：{outcome.message}",
            user_id=user_id,
            user_name=user["name"],
            level="INFO" if outcome.ok else ("ERROR" if outcome.fatal else "WARNING"),
            event="manual.checkin",
            detail=outcome.detail,
        )
        level = "ok" if outcome.ok else ("error" if outcome.fatal else "warn")
        return redirect_with("/", f"{user['name']}：{outcome.message}", level)

    @app.post("/users/{user_id}/test", response_class=HTMLResponse)
    async def user_test(
        user_id: int,
        request: Request,
        auth: dict = Depends(require_admin),
        csrf: str = Form("", alias="_csrf"),
    ):
        check_csrf(auth, csrf)
        user = db.get_user(user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="用户不存在")
        outcome: Outcome = await run_in_threadpool(service.test, user)
        return render(
            request,
            "test_result.html",
            auth=auth,
            user=public_user(user),
            outcome=outcome,
            detail=outcome.detail or {},
            locations=(outcome.detail or {}).get("locations", []),
        )

    @app.post("/users/{user_id}/locations")
    async def user_locations(
        user_id: int,
        request: Request,
        auth: dict = Depends(require_api_admin),
    ):
        check_csrf(auth, request.headers.get("X-CSRF-Token", ""))
        user = db.get_user(user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="用户不存在")
        try:
            locations = await run_in_threadpool(service.fetch_locations, user)
            return {"ok": True, "locations": locations}
        except ServiceError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        except Exception:
            logger.exception("fetch locations failed")
            return JSONResponse(
                {"ok": False, "error": "拉取打卡点失败，请查看日志"},
                status_code=500,
            )

    @app.post("/users/{user_id}/apply-location")
    def apply_location(
        user_id: int,
        auth: dict = Depends(require_admin),
        csrf: str = Form("", alias="_csrf"),
        latitude: str = Form(""),
        longitude: str = Form(""),
        location_name: str = Form(""),
        address: str = Form(""),
    ):
        check_csrf(auth, csrf)
        user = db.get_user(user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="用户不存在")
        lat = _parse_float(latitude)
        lng = _parse_float(longitude)
        if lat is None or lng is None or not (-90 <= lat <= 90) or not (-180 <= lng <= 180):
            return redirect_with(f"/users/{user_id}/edit", "坐标无效", "error")
        db.update_user(
            user_id,
            latitude=round(lat, 6),
            longitude=round(lng, 6),
            dorm_building=location_name[:100] or user.get("dorm_building", ""),
            address=address[:200] or user.get("address", ""),
        )
        today = datetime.now(cfg.tz).date().isoformat()
        db.delete_pending_plan(user_id, today)
        scheduler.wake()
        db.add_log(
            f"更新打卡点：{location_name or address or '自定义坐标'}",
            user_id=user_id,
            user_name=user["name"],
            event="user.location",
        )
        return redirect_with(f"/users/{user_id}/edit", "已更新打卡点坐标")

    # -- logs ---------------------------------------------------------------
    @app.get("/logs", response_class=HTMLResponse)
    def logs_page(
        request: Request,
        auth: dict = Depends(require_admin),
        user_id: str = "",
        page: int = 1,
    ):
        uid = int(user_id) if user_id.isdigit() else None
        page = max(1, page)
        per_page = 50
        total = db.count_logs(uid)
        pages = max(1, math.ceil(total / per_page))
        page = min(page, pages)
        rows = db.list_logs(uid, limit=per_page, offset=(page - 1) * per_page)
        return render(
            request,
            "logs.html",
            auth=auth,
            rows=rows,
            users=[
                {"id": u["id"], "name": u["name"], "student_id": u["student_id"]}
                for u in db.list_users()
            ],
            selected_user=uid,
            page=page,
            pages=pages,
            total=total,
        )

    @app.post("/logs/clear")
    def logs_clear(
        auth: dict = Depends(require_admin),
        csrf: str = Form("", alias="_csrf"),
    ):
        check_csrf(auth, csrf)
        db.clear_logs()
        db.add_log("管理员清空日志", event="admin.clear_logs")
        return redirect_with("/logs", "日志已清空")

    # -- settings -----------------------------------------------------------
    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request, auth: dict = Depends(require_admin)):
        info = cfg.public_dict()
        info["db_size"] = db.db_size()
        info["scheduler"] = scheduler.status()
        info["user_count"] = db.count_users()
        info["log_count"] = db.count_logs()
        info["platform_window"] = (
            f"{PLATFORM_WINDOW_START.strftime('%H:%M')}–"
            f"{PLATFORM_WINDOW_END.strftime('%H:%M')}"
        )
        return render(request, "settings.html", auth=auth, info=info)

    @app.post("/plans/regenerate")
    def plans_regenerate(
        auth: dict = Depends(require_admin),
        csrf: str = Form("", alias="_csrf"),
    ):
        check_csrf(auth, csrf)
        today = datetime.now(cfg.tz).date().isoformat()
        removed = db.delete_pending_plans_for_date(today)
        scheduler.wake()
        db.add_log(
            f"重新生成今日计划（清除 {removed} 条待执行）",
            event="plans.regenerate",
        )
        return redirect_with(
            "/", f"已清除 {removed} 条待执行计划，调度器将在数秒内重新随机安排"
        )

    # -- health -------------------------------------------------------------
    @app.get("/healthz")
    def healthz():
        now = datetime.now(cfg.tz)
        return {
            "status": "ok",
            "time": now.isoformat(),
            "version": cfg.version,
            "scheduler": scheduler.status(),
            "users": db.count_users(),
        }

    return app

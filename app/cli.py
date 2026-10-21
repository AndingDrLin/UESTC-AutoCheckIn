"""Account administration, explicit reporting, and optional personal check-in CLI."""
from __future__ import annotations

import argparse
import getpass
import json
import math
import sys
from datetime import date, datetime, time
from typing import Any

from .config import (
    PLATFORM_WINDOW_END, PLATFORM_WINDOW_START, Settings, load_settings,
    parse_hhmm, setup_logging,
)
from .db import Database, DuplicateStudentId
from .notifier import Notifier
from .scheduler import CheckinScheduler
from .security import SecretBox, SecretMismatch
from .uestc.admin_service import AdminService
from .uestc.cas import fetch_login_page
from .uestc.errors import UestcError
from .uestc.http import UestcHttp
from .uestc.service import CheckinService


def _context(settings: Settings | None = None):
    cfg = settings or load_settings()
    db = Database(cfg.db_path)
    box = SecretBox(cfg.app_secret)
    notifier = Notifier(cfg)
    service = CheckinService(cfg, db, box, notifier)
    admin = AdminService(cfg, db, box, notifier)
    scheduler = CheckinScheduler(cfg, db, service, notifier, admin_service=admin)
    return cfg, db, box, service, scheduler, admin


def _emit(args: argparse.Namespace, payload: Any) -> None:
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    elif isinstance(payload, str):
        print(payload)
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _require_account(db: Database, student_id: str) -> dict:
    account = db.get_user_by_student_id(student_id)
    if account is None:
        raise ValueError(f"未找到大学账号 {student_id}")
    return account


def _public_account(account: dict) -> dict:
    return {key: value for key, value in account.items() if key != "password_enc"}


def _prompt_password(value: str | None, *, confirm: bool = False) -> str:
    if value is not None:
        if not value:
            raise ValueError("密码不能为空")
        return value
    first = getpass.getpass("统一身份认证密码（输入不回显）: ")
    if confirm and first != getpass.getpass("再次输入密码: "):
        raise ValueError("两次输入的密码不一致")
    if not first:
        raise ValueError("密码不能为空")
    return first


def _check_coords(lat: float, lng: float) -> None:
    if not math.isfinite(lat) or not math.isfinite(lng) or not (-90 <= lat <= 90 and -180 <= lng <= 180):
        raise ValueError("坐标超出合法范围")
    if abs(lat) < 1e-9 and abs(lng) < 1e-9:
        raise ValueError("(0,0) 不是有效的个人打卡坐标")


def _window(start: str, end: str) -> tuple[str, str]:
    first, last = parse_hhmm(start), parse_hhmm(end)
    if not PLATFORM_WINDOW_START <= first < last <= PLATFORM_WINDOW_END:
        raise ValueError("个人打卡窗口必须在 21:00–23:30 内，且结束时间晚于开始时间")
    return first.strftime("%H:%M"), last.strftime("%H:%M")


def _validate_personal(account: dict) -> None:
    _check_coords(float(account["latitude"]), float(account["longitude"]))
    _window(account["window_start"], account["window_end"])


def _configuration(args: argparse.Namespace, cfg: Settings, account: dict | None = None) -> dict:
    existing = account or {}
    fields: dict[str, Any] = {}
    for option, column in (
        ("name", "name"), ("building", "dorm_building"), ("room", "dorm_room"),
        ("address", "address"), ("notify_url", "notify_url"), ("notes", "notes"),
    ):
        value = getattr(args, option, None)
        if value is not None:
            fields[column] = value.strip()
    if "name" in fields and not fields["name"]:
        raise ValueError("账号名称不能为空")
    fingerprint = getattr(args, "fingerprint", None)
    if fingerprint is not None:
        text = fingerprint.strip().upper().replace(" ", "")
        if text and (len(text) != 32 or any(c not in "0123456789ABCDEF" for c in text)):
            raise ValueError("浏览器指纹应为 32 位十六进制字符")
        fields["fingerprint"] = text
    lat, lng = getattr(args, "lat", None), getattr(args, "lng", None)
    if (lat is None) != (lng is None):
        raise ValueError("--lat 和 --lng 必须一起提供")
    if lat is not None:
        if not math.isfinite(lat) or not math.isfinite(lng) or not (-90 <= lat <= 90 and -180 <= lng <= 180):
            raise ValueError("坐标超出合法范围")
        fields.update(latitude=round(lat, 6), longitude=round(lng, 6))
    start, end = getattr(args, "window_start", None), getattr(args, "window_end", None)
    if start is not None or end is not None or account is None:
        first, last = _window(
            start if start is not None else existing.get("window_start", cfg.window_start.strftime("%H:%M")),
            end if end is not None else existing.get("window_end", cfg.window_end.strftime("%H:%M")),
        )
        fields.update(window_start=first, window_end=last)
    if existing.get("enabled"):
        _validate_personal({**existing, **fields})
    return fields


def _day(value: str | None, cfg: Settings) -> str:
    return date.fromisoformat(value).isoformat() if value else datetime.now(cfg.tz).date().isoformat()


def cmd_account(args: argparse.Namespace) -> int:
    cfg, db, box, _, scheduler, _ = _context()
    action = args.action
    if action == "list":
        _emit(args, [_public_account(account) for account in db.list_users()])
        return 0
    if action == "add":
        student_id = args.student_id.strip()
        if len(student_id) < 4 or not student_id.isalnum():
            raise ValueError("大学学号应为 4 位以上的字母或数字")
        if db.get_user_by_student_id(student_id):
            raise ValueError(f"大学账号 {student_id} 已存在")
        fields = _configuration(args, cfg)
        fields.update(student_id=student_id, enabled=int(args.personal_enabled))
        if args.personal_enabled:
            _validate_personal({"latitude": 0, "longitude": 0, **fields})
        fields["password_enc"] = box.encrypt(_prompt_password(args.password))
        account_id = db.create_user(**fields)
        db.add_log(f"CLI 新增大学账号 {student_id}", user_id=account_id, user_name=fields["name"], event="cli.account.add")
        if args.personal_enabled:
            scheduler.ensure_plans()
        _emit(args, _public_account(db.get_user(account_id)))
        return 0
    account = _require_account(db, args.account)
    account_id = int(account["id"])
    if action == "show":
        _emit(args, _public_account(account))
    elif action == "remove":
        if not args.yes and input(f"删除账号 {account['student_id']} 及其计划？[y/N] ").strip().lower() not in {"y", "yes"}:
            _emit(args, {"status": "cancelled"})
            return 0
        db.delete_user(account_id)
        db.add_log(f"CLI 删除大学账号 {account['student_id']}", user_name=account["name"], event="cli.account.remove")
        _emit(args, {"status": "removed", "account": account["student_id"]})
    elif action == "password":
        db.update_user(account_id, password_enc=box.encrypt(_prompt_password(args.password, confirm=True)))
        db.clear_session(account_id)
        db.add_log(f"CLI 更新大学账号密码 {account['student_id']}", user_id=account_id, user_name=account["name"], event="cli.account.password")
        _emit(args, {"status": "password_updated", "account": account["student_id"]})
    elif action == "update":
        fields = _configuration(args, cfg, account)
        if fields:
            db.update_user(account_id, **fields)
            if "fingerprint" in fields and fields["fingerprint"] != account["fingerprint"]:
                db.clear_session(account_id)
            db.add_log(f"CLI 更新大学账号 {account['student_id']}：{', '.join(sorted(fields))}", user_id=account_id, user_name=account["name"], event="cli.account.update")
            db.delete_pending_plan(account_id, _day(None, cfg))
            if account["enabled"]:
                scheduler.ensure_plans()
        _emit(args, _public_account(db.get_user(account_id)))
    return 0


def cmd_admin(args: argparse.Namespace) -> int:
    cfg, db, _, _, _, admin = _context()
    account = _require_account(db, args.account)
    if args.command == "class":
        payload = admin.classes(account, refresh=args.refresh)
    elif args.command == "student":
        payload = admin.roster(account, _day(args.date, cfg), class_key=args.class_key, keyword=args.keyword, refresh=args.refresh)
    elif args.options:
        payload = admin.options(account, location=args.location, refresh=args.refresh)
    else:
        targets = [{"student_id": value, "class_key": args.class_key} for value in args.student_id]
        targets.extend({"student_name": value, "class_key": args.class_key} for value in args.student_name)
        if not targets:
            raise ValueError("必须通过 --student-id 或 --student-name 明确选择上报对象")
        if not args.location or not args.reason:
            raise ValueError("上报必须提供 --location 和 --reason")
        preview = admin.prepare(account, _day(args.date, cfg), targets, args.location, args.reason, args.remark)
        payload = {"account": account["student_id"], "status": "preview", "preview": preview}
        if args.submit:
            result = admin.submit(account, preview["id"])
            payload.update(status=result["status"], result=result)
        _emit(args, payload)
        return 0 if payload["status"] in {"preview", "acknowledged"} else 1
    _emit(args, {"account": account["student_id"], "data": payload})
    return 0


def cmd_self(args: argparse.Namespace) -> int:
    cfg, db, _, service, scheduler, _ = _context()
    account = _require_account(db, args.account)
    account_id = int(account["id"])
    if args.action == "config":
        fields = _configuration(args, cfg, account)
        if fields:
            db.update_user(account_id, **fields)
            if "fingerprint" in fields and fields["fingerprint"] != account["fingerprint"]:
                db.clear_session(account_id)
            db.add_log(f"CLI 更新个人配置 {account['student_id']}：{', '.join(sorted(fields))}", user_id=account_id, user_name=account["name"], event="cli.self.config")
            db.delete_pending_plan(account_id, _day(None, cfg))
            if account["enabled"]:
                scheduler.ensure_plans()
        _emit(args, _public_account(db.get_user(account_id)))
    elif args.action in {"enable", "disable"}:
        enabled = args.action == "enable"
        if enabled:
            _validate_personal(account)
        db.update_user(account_id, enabled=int(enabled))
        db.add_log(f"CLI {'启用' if enabled else '停用'}个人自动打卡 {account['student_id']}", user_id=account_id, user_name=account["name"], event=f"cli.self.{args.action}")
        if enabled:
            scheduler.ensure_plans()
        else:
            db.delete_pending_plan(account_id, _day(None, cfg))
        _emit(args, {"account": account["student_id"], "personal_enabled": enabled})
    elif args.action == "status":
        _emit(args, {"account": _public_account(account), "plans": [
            plan for plan in db.list_plans_for_date(_day(None, cfg)) if int(plan["user_id"]) == account_id
        ], "logs": db.list_logs(user_id=account_id, limit=10)})
    else:
        if args.submit:
            _validate_personal(account)
            now = datetime.now(cfg.tz)
            if not PLATFORM_WINDOW_START <= now.time().replace(tzinfo=None) <= PLATFORM_WINDOW_END:
                raise ValueError("实际个人打卡仅允许在平台时段 21:00–23:30 内执行")
            outcome = service.run(account, manual=True)
            if outcome.ok:
                plan = db.get_plan(account_id, now.date().isoformat())
                if plan is not None:
                    db.update_plan(int(plan["id"]), status="success", next_attempt_epoch=None,
                                   last_error="", result=json.dumps(outcome.to_dict(), ensure_ascii=False))
            db.add_log(f"CLI 手动打卡：{outcome.message}", user_id=account_id, user_name=account["name"],
                       level="INFO" if outcome.ok else ("ERROR" if outcome.fatal else "WARNING"),
                       event="manual.checkin", detail=outcome.detail)
        else:
            outcome = service.test(account)
        _emit(args, {"account": account["student_id"], "submitted": args.submit, **outcome.to_dict()})
        return 0 if outcome.ok else 1
    return 0


def cmd_rule(args: argparse.Namespace) -> int:
    _, db, _, _, _, admin = _context()
    account = _require_account(db, args.account)
    account_id = int(account["id"])
    if args.action == "list":
        payload = db.list_report_rules(account_id)
    elif args.action == "jobs":
        payload = db.list_report_jobs(account_id)
    elif args.action == "add":
        rule_id = admin.add_rule(account, args.student_id, args.location, args.reason, args.remark,
                                 args.start_date, args.end_date, args.weekdays, args.run_time, enabled=not args.disabled)
        payload = {"id": rule_id, "status": "created"}
    else:
        if not any(int(rule["id"]) == args.rule_id for rule in db.list_report_rules(account_id)):
            raise ValueError("所选账号不存在该上报规则")
        if args.action == "remove":
            db.delete_report_rule(args.rule_id)
        else:
            db.set_report_rule_enabled(args.rule_id, args.action == "enable")
        db.add_log(f"CLI {args.action} 上报规则 #{args.rule_id}（账号 {account['student_id']}）",
                   user_id=account_id, user_name=account["name"], event=f"cli.rule.{args.action}")
        payload = {"id": args.rule_id, "status": args.action}
    _emit(args, {"account": account["student_id"], "data": payload})
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    cfg, db, _, _, scheduler, _ = _context()
    day = _day(args.date, cfg)
    if args.action == "regenerate":
        db.delete_pending_plans_for_date(day)
        scheduler.ensure_plans(datetime.combine(date.fromisoformat(day), time(12), tzinfo=cfg.tz))
    _emit(args, {"date": day, "personal_plans": db.list_plans_for_date(day)})
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    cfg = load_settings()
    setup_logging(cfg.log_level)
    import uvicorn
    uvicorn.run("app.asgi:app", host=args.host or cfg.host, port=args.port or cfg.port,
                log_level=(args.log_level or cfg.log_level).lower(), proxy_headers=True, forwarded_allow_ips="*")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    cfg, db, box, _, _, _ = _context()
    encryption_ok = box.decrypt(box.encrypt("self-check")) == "self-check"
    payload = {"version": cfg.version, "database": str(cfg.db_path), "database_bytes": db.db_size(),
               "timezone": cfg.tz_name, "accounts": db.count_users(), "encryption_ok": encryption_ok,
               "default_admin_password": cfg.is_default_password, "scheduler_enabled": cfg.scheduler_enabled}
    ok = encryption_ok and not cfg.is_default_password
    if args.network:
        try:
            with UestcHttp(cfg.user_agent, timeout=cfg.http_timeout) as client:
                fetch_login_page(client)
            payload["network"] = "ok"
        except Exception as exc:  # Diagnostics must describe network failures.
            payload["network"] = str(exc)
            ok = False
    _emit(args, payload)
    return 0 if ok else 1


def _output(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="输出 JSON")


def _acting(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--account", required=True, help="执行操作的大学账号学号（不是本地 ID）")
    _output(parser)


def _config_options(parser: argparse.ArgumentParser) -> None:
    for name in ("fingerprint", "building", "room", "address", "window-start", "window-end", "notify-url", "notes"):
        parser.add_argument("--" + name)
    parser.add_argument("--lat", type=float)
    parser.add_argument("--lng", type=float)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description="大学账号管理、班级上报与可选个人打卡")
    groups = parser.add_subparsers(dest="command", required=True)
    account = groups.add_parser("account", help="大学账号与凭据管理（默认不启用个人打卡）")
    actions = account.add_subparsers(dest="action", required=True)
    for action in ("add", "list", "show", "update", "remove", "password"):
        command = actions.add_parser(action)
        command.set_defaults(func=cmd_account)
        if action in {"add", "list"}:
            _output(command)
        else:
            _acting(command)
        if action == "add":
            command.add_argument("--student-id", required=True)
            command.add_argument("--name", required=True)
            command.add_argument("--personal-enabled", action="store_true")
        elif action == "update":
            command.add_argument("--name")
        if action in {"add", "update"}:
            _config_options(command)
        if action in {"add", "password"}:
            command.add_argument("--password", help="省略则安全交互输入")
        if action == "remove":
            command.add_argument("--yes", action="store_true")
    for group in ("class", "student"):
        commands = groups.add_parser(group).add_subparsers(dest="action", required=True)
        command = commands.add_parser("list")
        _acting(command)
        command.add_argument("--refresh", action="store_true")
        if group == "student":
            command.add_argument("--date")
            command.add_argument("--class-key", default="")
            command.add_argument("--keyword", default="")
        command.set_defaults(func=cmd_admin)
    report = groups.add_parser("report", help="明确选择对象；默认预览，--submit 才实际上报")
    _acting(report)
    report.add_argument("--date")
    report.add_argument("--class-key", default="")
    report.add_argument("--student-id", action="append", default=[], help="目标学号，可重复")
    report.add_argument("--student-name", action="append", default=[], help="目标姓名，必须唯一匹配；可重复")
    report.add_argument("--location", default="")
    report.add_argument("--reason", default="")
    report.add_argument("--remark", default="")
    mode = report.add_mutually_exclusive_group()
    mode.add_argument("--options", action="store_true", help="读取学校上报选项")
    mode.add_argument("--submit", action="store_true")
    report.add_argument("--refresh", action="store_true")
    report.set_defaults(func=cmd_admin)
    personal = groups.add_parser("self", help="独立的可选个人打卡")
    actions = personal.add_subparsers(dest="action", required=True)
    for action in ("config", "enable", "disable", "run", "status"):
        command = actions.add_parser(action)
        _acting(command)
        command.set_defaults(func=cmd_self)
        if action == "config":
            _config_options(command)
        if action == "run":
            command.add_argument("--submit", action="store_true", help="实际提交；默认只测试链路")
    rules = groups.add_parser("rule").add_subparsers(dest="action", required=True)
    for action in ("add", "list", "enable", "disable", "remove", "jobs"):
        command = rules.add_parser(action)
        _acting(command)
        command.set_defaults(func=cmd_rule)
        if action == "add":
            for name in ("student-id", "location", "reason", "start-date", "end-date", "weekdays", "run-time"):
                command.add_argument("--" + name, required=True)
            command.add_argument("--remark", default="")
            command.add_argument("--disabled", action="store_true")
        elif action in {"enable", "disable", "remove"}:
            command.add_argument("--rule-id", type=int, required=True)
    plans = groups.add_parser("plan", help="个人打卡计划（上报任务见 rule jobs）").add_subparsers(dest="action", required=True)
    for action in ("show", "regenerate"):
        command = plans.add_parser(action)
        command.add_argument("--date")
        _output(command)
        command.set_defaults(func=cmd_plan)
    doctor = groups.add_parser("doctor")
    doctor.add_argument("--network", action="store_true")
    _output(doctor)
    doctor.set_defaults(func=cmd_doctor)
    serve = groups.add_parser("serve")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.add_argument("--log-level")
    serve.set_defaults(func=cmd_serve)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (ValueError, DuplicateStudentId, UestcError, SecretMismatch) as exc:
        if getattr(args, "json", False):
            print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False))
        else:
            print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

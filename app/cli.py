"""Administrative CLI.

Examples::

    python -m app.cli user add --name 张三 --student-id 2020000000000 \
        --building 本科13栋 --fingerprint AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
    python -m app.cli user list
    python -m app.cli plan show
    python -m app.cli run --student-id 2020000000000 --dry-run
    python -m app.cli doctor --network
    python -m app.cli serve
"""
from __future__ import annotations

import argparse
import getpass
import json
import sys
from datetime import datetime, time
from typing import Any

from .config import Settings, load_settings, parse_hhmm, setup_logging
from .db import Database, DuplicateStudentId
from .notifier import Notifier
from .scheduler import CheckinScheduler
from .security import SecretBox, SecretMismatch
from .uestc.cas import fetch_login_page
from .uestc.dorms import DEFAULT_ADDRESS, jitter, normalize_building
from .uestc.errors import UestcError
from .uestc.http import UestcHttp
from .uestc.service import CheckinService


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _context(settings: Settings | None = None):
    cfg = settings or load_settings()
    db = Database(cfg.db_path)
    box = SecretBox(cfg.app_secret)
    notifier = Notifier(cfg)
    service = CheckinService(cfg, db, box, notifier)
    scheduler = CheckinScheduler(cfg, db, service, notifier)
    return cfg, db, box, service, scheduler


def _die(message: str, code: int = 1) -> int:
    print(f"错误：{message}", file=sys.stderr)
    return code


def _require_user(db: Database, student_id: str) -> dict | None:
    user = db.get_user_by_student_id(student_id)
    if user is None:
        print(f"未找到学号为 {student_id} 的用户", file=sys.stderr)
    return user


def _prompt_password(value: str | None, *, confirm: bool = False) -> str:
    if value:
        return value
    first = getpass.getpass("统一身份认证密码（输入不回显）: ")
    if confirm:
        second = getpass.getpass("再次输入密码: ")
        if first != second:
            raise ValueError("两次输入的密码不一致")
    if not first:
        raise ValueError("密码不能为空")
    return first


def _parse_window(cfg: Settings, start: str | None, end: str | None) -> tuple[str, str]:
    start_time = parse_hhmm(start) if start else cfg.window_start
    end_time = parse_hhmm(end) if end else cfg.window_end
    if end_time <= start_time:
        raise ValueError("结束时间必须晚于开始时间")
    return start_time.strftime("%H:%M"), end_time.strftime("%H:%M")


def _check_coords(lat: float, lng: float) -> None:
    if not (-90 <= lat <= 90) or not (-180 <= lng <= 180):
        raise ValueError("坐标超出合法范围")
    if abs(lat) < 1e-9 and abs(lng) < 1e-9:
        raise ValueError("(0,0) 不是有效坐标")


def _fingerprint(value: str | None) -> str:
    text = (value or "").strip().upper().replace(" ", "")
    if text and (len(text) != 32 or any(c not in "0123456789ABCDEF" for c in text)):
        raise ValueError("浏览器指纹应为 32 位十六进制字符")
    return text


def _coords_for_building(db: Database, building: str) -> tuple[str, float, float]:
    """按楼栋基准坐标生成一个带扰动的位置。"""
    name = normalize_building(building)
    if not name:
        raise ValueError("楼栋名应形如「本科13栋」")
    row = db.get_building(name)
    if not row:
        raise ValueError(
            f"楼栋「{name}」还没有基准坐标：可在 Web 看板/设置页添加，或直接用 --lat/--lng 指定"
        )
    latitude, longitude = jitter(float(row["latitude"]), float(row["longitude"]))
    return name, latitude, longitude


def _print_user(user: dict, db: Database) -> None:
    session = db.get_session(int(user["id"]))
    print(f"用户 #{user['id']}  {user['name']}  ({user['student_id']})")
    print(f"  状态      : {'启用' if user['enabled'] else '停用'}")
    print(f"  宿舍楼    : {user['dorm_building'] or '—'}")
    print(f"  坐标      : {user['latitude']:.6f}, {user['longitude']:.6f}")
    print(f"  打卡窗口  : {user['window_start']}–{user['window_end']}")
    print(f"  指纹      : {user['fingerprint'] or '未配置'}")
    print(f"  密码      : {'已保存（加密）' if user['password_enc'] else '缺失'}")
    print(f"  通知      : {user['notify_url'] or '—'}")
    if session:
        state = "已失效" if session["invalid"] else "已缓存"
        print(
            f"  会话      : {state}，最近登录 "
            f"{datetime.fromtimestamp(int(session['last_login_epoch'])).isoformat() if session['last_login_epoch'] else '—'}"
            f"，今日登录 {session['logins_today'] or 0} 次"
        )
    else:
        print("  会话      : 尚未登录")


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------
def cmd_user_add(args: argparse.Namespace) -> int:
    cfg, db, box, _, scheduler = _context()
    try:
        window_start, window_end = _parse_window(cfg, args.window_start, args.window_end)
        fingerprint = _fingerprint(args.fingerprint)
        password = _prompt_password(args.password)
        if args.lat is not None or args.lng is not None:
            if args.lat is None or args.lng is None:
                raise ValueError("--lat 与 --lng 需要同时提供")
            _check_coords(args.lat, args.lng)
            building = normalize_building(args.building or "") or (args.building or "").strip()
            latitude, longitude = round(args.lat, 6), round(args.lng, 6)
        else:
            if not args.building:
                raise ValueError("请用 --building 指定宿舍楼（或用 --lat/--lng 指定坐标）")
            building, latitude, longitude = _coords_for_building(db, args.building)
    except ValueError as exc:
        return _die(str(exc))
    if len(args.student_id) < 4 or not args.student_id.isalnum():
        return _die("学号应为 4 位以上的字母或数字")
    if db.get_user_by_student_id(args.student_id):
        return _die(f"学号 {args.student_id} 已存在")
    try:
        user_id = db.create_user(
            name=args.name.strip(),
            student_id=args.student_id.strip(),
            password_enc=box.encrypt(password),
            fingerprint=fingerprint,
            dorm_building=building,
            address=DEFAULT_ADDRESS,
            latitude=latitude,
            longitude=longitude,
            window_start=window_start,
            window_end=window_end,
            notify_url=(args.notify_url or "").strip(),
            notes=(args.notes or "").strip(),
            enabled=0 if args.disabled else 1,
        )
    except DuplicateStudentId as exc:
        return _die(str(exc))
    db.add_log(f"CLI 新增用户 {args.name}", user_id=user_id, user_name=args.name, event="cli.user.add")
    if not args.disabled:
        scheduler.ensure_plans()
    print(f"已添加用户 #{user_id} {args.name}（{args.student_id}）")
    print(f"宿舍楼：{building}，坐标：{latitude:.6f}, {longitude:.6f}")
    print(f"打卡窗口：{window_start}–{window_end}；今天将由调度器随机安排。")
    return 0


def cmd_user_update(args: argparse.Namespace) -> int:
    cfg, db, box, _, scheduler = _context()
    user = _require_user(db, args.student_id)
    if user is None:
        return 1
    fields: dict[str, Any] = {}
    try:
        if args.name is not None:
            fields["name"] = args.name.strip()
        if args.fingerprint is not None:
            fields["fingerprint"] = _fingerprint(args.fingerprint)
        if args.building is not None and args.lat is None and args.lng is None:
            name, latitude, longitude = _coords_for_building(db, args.building)
            fields["dorm_building"] = name
            fields["latitude"] = latitude
            fields["longitude"] = longitude
        if args.notify_url is not None:
            fields["notify_url"] = args.notify_url.strip()
        if args.notes is not None:
            fields["notes"] = args.notes.strip()
        if args.lat is not None and args.lng is not None:
            _check_coords(args.lat, args.lng)
            fields["latitude"] = round(args.lat, 6)
            fields["longitude"] = round(args.lng, 6)
        elif (args.lat is None) != (args.lng is None):
            raise ValueError("--lat 与 --lng 需要同时提供")
        if args.window_start is not None or args.window_end is not None:
            start, end = _parse_window(cfg, args.window_start, args.window_end)
            fields["window_start"] = start
            fields["window_end"] = end
        if args.disable:
            fields["enabled"] = 0
        elif args.enable:
            fields["enabled"] = 1
        password = args.password or ""
        if password:
            fields["password_enc"] = box.encrypt(password)
    except ValueError as exc:
        return _die(str(exc))
    if not fields:
        print("没有需要更新的字段。")
        return 0
    db.update_user(int(user["id"]), **fields)
    if "password_enc" in fields:
        db.clear_session(int(user["id"]))
    db.delete_pending_plan(int(user["id"]), datetime.now(cfg.tz).date().isoformat())
    db.add_log(
        "CLI 更新用户 " + str(fields.get("name", user["name"])),
        user_id=int(user["id"]),
        user_name=str(fields.get("name", user["name"])),
        event="cli.user.update",
    )
    if fields.get("enabled", 1):
        scheduler.ensure_plans()
    print(f"已更新用户 {user['student_id']}：{', '.join(sorted(fields))}")
    return 0


def cmd_user_list(args: argparse.Namespace) -> int:
    _, db, _, _, _ = _context()
    users = db.list_users()
    if not users:
        print("还没有用户。")
        return 0
    print(f"{'ID':<4} {'姓名':<12} {'学号':<16} {'状态':<5} {'窗口':<12} {'宿舍'}")
    print("-" * 88)
    for user in users:
        dorm = user['dorm_building'] or "—"
        print(
            f"{user['id']:<4} {user['name']:<12} {user['student_id']:<16} "
            f"{'启用' if user['enabled'] else '停用':<5} "
            f"{user['window_start'] + '-' + user['window_end']:<12} {dorm}"
        )
    return 0


def cmd_user_show(args: argparse.Namespace) -> int:
    _, db, _, _, _ = _context()
    user = _require_user(db, args.student_id)
    if user is None:
        return 1
    _print_user(user, db)
    return 0


def cmd_user_remove(args: argparse.Namespace) -> int:
    _, db, _, _, _ = _context()
    user = _require_user(db, args.student_id)
    if user is None:
        return 1
    if not args.yes:
        answer = input(f"确认删除 {user['name']}（{user['student_id']}）及其全部计划？[y/N] ")
        if answer.strip().lower() not in {"y", "yes"}:
            print("已取消。")
            return 0
    db.delete_user(int(user["id"]))
    db.add_log(f"CLI 删除用户 {user['name']}", user_name=user["name"], event="cli.user.delete")
    print(f"已删除用户 {user['student_id']}。")
    return 0


def cmd_user_password(args: argparse.Namespace) -> int:
    _, db, box, _, _ = _context()
    user = _require_user(db, args.student_id)
    if user is None:
        return 1
    try:
        password = _prompt_password(args.password, confirm=True)
    except ValueError as exc:
        return _die(str(exc))
    db.update_user(int(user["id"]), password_enc=box.encrypt(password))
    db.clear_session(int(user["id"]))
    db.add_log("CLI 更新密码", user_id=int(user["id"]), user_name=user["name"], event="cli.user.password")
    print(f"已更新 {user['student_id']} 的密码，旧会话已清除。")
    return 0


def cmd_user_toggle(args: argparse.Namespace) -> int:
    cfg, db, _, _, scheduler = _context()
    user = _require_user(db, args.student_id)
    if user is None:
        return 1
    value = 1 if args.enable else 0
    db.update_user(int(user["id"]), enabled=value)
    if value:
        scheduler.ensure_plans()
    else:
        db.delete_pending_plan(int(user["id"]), datetime.now(cfg.tz).date().isoformat())
    db.add_log(
        f"CLI {'启用' if value else '停用'}用户 {user['name']}",
        user_id=int(user["id"]),
        user_name=user["name"],
        event="cli.user.toggle",
    )
    print(f"已{'启用' if value else '停用'} {user['student_id']}。")
    return 0


def cmd_plan_show(args: argparse.Namespace) -> int:
    cfg, db, _, _, _ = _context()
    day = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else datetime.now(cfg.tz).date()
    plans = db.list_plans_for_date(day.isoformat())
    if not plans:
        print(f"{day.isoformat()} 还没有生成计划。")
        return 0
    users = {int(u["id"]): u for u in db.list_users()}
    print(f"{day.isoformat()} 共 {len(plans)} 条计划：")
    for plan in plans:
        user = users.get(int(plan["user_id"]), {})
        print(
            f"  {plan['planned_time']}  {user.get('name', '?'):<12}  "
            f"{plan['status']:<8} 尝试 {plan['attempts']} 次"
            + (f"  {plan['last_error']}" if plan["last_error"] else "")
        )
    return 0


def cmd_plan_regenerate(args: argparse.Namespace) -> int:
    cfg, db, _, _, scheduler = _context()
    day = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else datetime.now(cfg.tz).date()
    removed = db.delete_pending_plans_for_date(day.isoformat())
    # Noon gives the planner a stable "now" inside the target day.
    scheduler.ensure_plans(datetime.combine(day, time(12, 0), tzinfo=cfg.tz))
    plans = db.list_plans_for_date(day.isoformat())
    print(f"已清除 {removed} 条待执行计划，{day.isoformat()} 现有 {len(plans)} 条。")
    for plan in plans:
        if plan["status"] in {"pending", "running"}:
            print(f"  {plan['planned_time']}  {plan['status']}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    _, db, _, service, _ = _context()
    user = _require_user(db, args.student_id)
    if user is None:
        return 1
    outcome = service.test(user) if args.dry_run else service.run(user, manual=True)
    payload = outcome.to_dict()
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        label = "链路测试" if args.dry_run else "打卡"
        print(f"{label}结果：{'成功' if outcome.ok else '失败'}")
        print(f"  {outcome.message}")
        if outcome.record_status:
            print(f"  记录状态：{outcome.record_status}")
        if outcome.detail:
            summary = {
                key: value
                for key, value in outcome.detail.items()
                if key in {"today", "schedule", "session_valid", "login_used", "location_count"}
            }
            if summary:
                print("  详情：")
                print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 0 if outcome.ok else 1


def cmd_serve(args: argparse.Namespace) -> int:
    cfg = load_settings()
    setup_logging(cfg.log_level)
    import uvicorn

    uvicorn.run(
        "app.asgi:app",
        host=args.host or cfg.host,
        port=args.port or cfg.port,
        log_level=(args.log_level or cfg.log_level).lower(),
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    cfg, db, box, _, scheduler = _context()
    print("UESTC 晚安成电 · 自动打卡 自检")
    print(f"  版本        : {cfg.version}")
    print(f"  数据目录    : {cfg.data_dir}")
    print(f"  数据库      : {cfg.db_path}（{db.db_size()} 字节）")
    print(f"  时区        : {cfg.tz_name}（当前 {datetime.now(cfg.tz).isoformat()}）")
    print(f"  窗口        : {cfg.window_start.strftime('%H:%M')}–{cfg.window_end.strftime('%H:%M')}")
    print(f"  平台时段    : 21:00–23:30")
    print(f"  会话密钥    : {cfg.secret_source}")
    print(f"  管理密码    : {'默认值（请修改）' if cfg.is_default_password else '已自定义'}")
    print(f"  用户数      : {db.count_users()}")
    today = datetime.now(cfg.tz).date().isoformat()
    print(f"  今日计划    : {len(db.list_plans_for_date(today))} 条")
    print(f"  调度器      : {'启用' if cfg.scheduler_enabled else '禁用'}")
    ok = True
    try:
        probe = "self-check"
        if box.decrypt(box.encrypt(probe)) != probe:
            print("  加密自检    : 失败（解密结果不一致）")
            ok = False
        else:
            print("  加密自检    : 通过")
    except SecretMismatch as exc:
        print(f"  加密自检    : 失败（{exc}）")
        ok = False
    if cfg.is_default_password:
        ok = False
    if args.network:
        print("  网络检查    : 正在访问统一身份认证登录页…")
        try:
            with UestcHttp(cfg.user_agent, timeout=cfg.http_timeout) as client:
                page = fetch_login_page(client)
            print(f"  网络检查    : 通过（execution {len(page.execution)} 字符，salt {len(page.salt)} 字符）")
        except UestcError as exc:
            print(f"  网络检查    : 失败（{exc}）")
            ok = False
        except Exception as exc:  # noqa: BLE001 - diagnostics should not crash
            print(f"  网络检查    : 失败（{type(exc).__name__}: {exc}）")
            ok = False
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli",
        description="UESTC 晚安成电自动打卡 · 管理与诊断工具",
    )
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="启动 Web 服务与后台调度器")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument("--log-level", default=None)
    serve.set_defaults(func=cmd_serve)

    user = sub.add_parser("user", help="用户管理")
    user_sub = user.add_subparsers(dest="user_command", required=True)

    add = user_sub.add_parser("add", help="新增用户")
    add.add_argument("--name", required=True)
    add.add_argument("--student-id", required=True)
    add.add_argument("--password", default=None, help="省略则交互输入")
    add.add_argument("--fingerprint", default=None)
    add.add_argument("--building", default=None)
    add.add_argument("--room", default=None)
    add.add_argument("--address", default=None)
    add.add_argument("--lat", type=float, default=None, help="覆盖楼栋坐标（可选）")
    add.add_argument("--lng", type=float, default=None, help="覆盖楼栋坐标（可选）")
    add.add_argument("--window-start", default=None)
    add.add_argument("--window-end", default=None)
    add.add_argument("--notify-url", default=None)
    add.add_argument("--notes", default=None)
    add.add_argument("--disabled", action="store_true")
    add.set_defaults(func=cmd_user_add)

    update = user_sub.add_parser("update", help="修改用户")
    update.add_argument("student_id")
    update.add_argument("--name")
    update.add_argument("--password", default=None)
    update.add_argument("--fingerprint")
    update.add_argument("--building")
    update.add_argument("--lat", type=float)
    update.add_argument("--lng", type=float)
    update.add_argument("--window-start")
    update.add_argument("--window-end")
    update.add_argument("--notify-url")
    update.add_argument("--notes")
    update.add_argument("--enable", action="store_true")
    update.add_argument("--disable", action="store_true")
    update.set_defaults(func=cmd_user_update)

    listing = user_sub.add_parser("list", help="列出用户")
    listing.set_defaults(func=cmd_user_list)

    show = user_sub.add_parser("show", help="查看用户")
    show.add_argument("student_id")
    show.set_defaults(func=cmd_user_show)

    remove = user_sub.add_parser("remove", help="删除用户")
    remove.add_argument("student_id")
    remove.add_argument("--yes", action="store_true")
    remove.set_defaults(func=cmd_user_remove)

    password = user_sub.add_parser("password", help="修改统一身份认证密码")
    password.add_argument("student_id")
    password.add_argument("--password", default=None)
    password.set_defaults(func=cmd_user_password)

    enable = user_sub.add_parser("enable", help="启用用户")
    enable.add_argument("student_id")
    enable.set_defaults(func=cmd_user_toggle, enable=True, disable=False)

    disable = user_sub.add_parser("disable", help="停用用户")
    disable.add_argument("student_id")
    disable.set_defaults(func=cmd_user_toggle, enable=False, disable=True)

    plan = sub.add_parser("plan", help="计划管理")
    plan_sub = plan.add_subparsers(dest="plan_command", required=True)
    plan_show = plan_sub.add_parser("show", help="查看某日计划")
    plan_show.add_argument("--date", default=None, help="YYYY-MM-DD，默认今天")
    plan_show.set_defaults(func=cmd_plan_show)
    plan_regen = plan_sub.add_parser("regenerate", help="重新随机某日计划")
    plan_regen.add_argument("--date", default=None, help="YYYY-MM-DD，默认今天")
    plan_regen.set_defaults(func=cmd_plan_regenerate)

    run = sub.add_parser("run", help="立即执行一次打卡（或 --dry-run 只测试）")
    run.add_argument("--student-id", required=True)
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--json", action="store_true")
    run.set_defaults(func=cmd_run)

    doctor = sub.add_parser("doctor", help="自检")
    doctor.add_argument("--network", action="store_true", help="额外检查学校登录页是否可达")
    doctor.set_defaults(func=cmd_doctor)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())

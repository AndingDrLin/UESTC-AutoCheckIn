"""Daily plan generation, retries and the background worker."""
from __future__ import annotations

import json
import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from .config import Settings, parse_hhmm
from .db import Database, utc_now_iso
from .notifier import Notifier
from .planning import next_retry_epoch, random_times_in_window, window_bounds
from .uestc.service import CheckinService, Outcome

logger = logging.getLogger(__name__)


class CheckinScheduler:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        service: CheckinService,
        notifier: Notifier | None = None,
        *,
        rng: random.Random | None = None,
        max_workers: int = 4,
        admin_service=None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.service = service
        self.notifier = notifier
        self.admin_service = admin_service
        self._rng = rng or random.SystemRandom()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="checkin"
        )
        self._running: set[int] = set()
        self._running_lock = threading.Lock()
        self._tick_lock = threading.Lock()
        self.started_at: datetime | None = None
        self.last_tick: datetime | None = None
        self._admin_future = None

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        if not self.settings.scheduler_enabled:
            logger.warning("scheduler disabled by SCHEDULER_ENABLED=false")
            return
        if self._thread and self._thread.is_alive():
            return
        self.db.recover_running_plans()
        if self.admin_service is not None:
            self.db.recover_report_jobs()
        self.started_at = datetime.now(self.settings.tz)
        self._thread = threading.Thread(
            target=self._loop, name="checkin-scheduler", daemon=True
        )
        self._thread.start()
        logger.info(
            "scheduler started, window %s %s",
            self.settings.window_text(),
            self.settings.tz_name,
        )

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._executor.shutdown(wait=False, cancel_futures=True)

    def wake(self) -> None:
        self._wake.set()

    def ensure_plans(self, now: datetime | None = None) -> None:
        """Create today's plans without dispatching due check-ins (CLI helper)."""
        with self._tick_lock:
            self._ensure_plans(now or datetime.now(self.settings.tz))

    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def status(self) -> dict:
        return {
            "enabled": self.settings.scheduler_enabled,
            "alive": self.is_alive(),
            "last_tick": self.last_tick.isoformat() if self.last_tick else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "running": len(self._running),
        }

    # -- main loop ----------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # pragma: no cover - keep the scheduler alive
                logger.exception("scheduler tick failed")
            self._wake.wait(5)
            self._wake.clear()

    def tick(self) -> None:
        with self._tick_lock:
            now = datetime.now(self.settings.tz)
            self.last_tick = now
            if self.admin_service is not None and (self._admin_future is None or self._admin_future.done()):
                try:
                    self._admin_future = self._executor.submit(self._run_admin_tick, now)
                except RuntimeError:
                    logger.warning('report dispatcher is shutting down')
            self._ensure_plans(now)
            self._expire(now)
            due = self.db.list_due_plans(int(now.timestamp()))
            if not due:
                return
            for plan in due:
                user = self.db.get_user(int(plan["user_id"]))
                if user is None:
                    self.db.update_plan(
                        int(plan["id"]), status="failed", last_error="用户已被删除"
                    )
                    continue
                if not int(user.get("enabled", 0)):
                    self.db.update_plan(
                        int(plan["id"]), status="failed", last_error="用户已停用"
                    )
                    continue
                try:
                    _, end_dt = window_bounds(
                        now.date(),
                        parse_hhmm(user["window_start"]),
                        parse_hhmm(user["window_end"]),
                        self.settings.tz,
                    )
                except ValueError:
                    self.db.update_plan(
                        int(plan["id"]),
                        status="failed",
                        last_error="打卡时段配置无效",
                    )
                    continue
                if now >= end_dt:
                    self.db.update_plan(
                        int(plan["id"]),
                        status="failed",
                        next_attempt_epoch=None,
                        last_error="窗口已结束，未能完成打卡",
                    )
                    continue
                self._claim_and_submit(plan, user)

    def _run_admin_tick(self, now):
        try:
            self.admin_service.tick(now)
        except Exception:
            logger.exception('administrator reporting tick failed')

    # -- plan generation ----------------------------------------------------
    def _ensure_plans(self, now: datetime) -> None:
        users = self.db.list_users(enabled_only=True)
        if not users:
            return
        date_str = now.date().isoformat()
        existing = {
            int(p["user_id"]) for p in self.db.list_plans_for_date(date_str)
        }
        groups: dict[tuple[str, str], list[dict]] = {}
        for user in users:
            if int(user["id"]) in existing:
                continue
            groups.setdefault(
                (user["window_start"], user["window_end"]), []
            ).append(user)

        for (window_start, window_end), group in groups.items():
            try:
                start, end = window_bounds(
                    now.date(),
                    parse_hhmm(window_start),
                    parse_hhmm(window_end),
                    self.settings.tz,
                )
            except ValueError:
                for user in group:
                    self.db.insert_plan(
                        int(user["id"]),
                        date_str,
                        int(now.timestamp()),
                        now.strftime("%H:%M:%S"),
                        status="failed",
                        last_error="打卡时段配置无效",
                    )
                continue

            effective_start = max(start, now + timedelta(seconds=20))
            if end <= effective_start + timedelta(seconds=30):
                for user in group:
                    self.db.insert_plan(
                        int(user["id"]),
                        date_str,
                        int(end.timestamp()),
                        end.strftime("%H:%M:%S"),
                        status="missed",
                        last_error="窗口已过，今天未安排自动打卡",
                    )
                continue

            times = random_times_in_window(
                effective_start,
                end,
                len(group),
                self.settings.min_user_gap_sec,
                self._rng,
            )
            for user, planned in zip(group, times):
                self.db.insert_plan(
                    int(user["id"]),
                    date_str,
                    int(planned.timestamp()),
                    planned.strftime("%H:%M:%S"),
                )
                logger.info(
                    "planned check-in for %s at %s",
                    user.get("name"),
                    planned.strftime("%H:%M:%S"),
                )

    # -- expiry -------------------------------------------------------------
    def _expire(self, now: datetime) -> None:
        today = now.date().isoformat()
        self.db.expire_pending_before(today, "窗口已结束，未完成打卡")

        for plan in self.db.list_plans_for_date(today):
            if plan["status"] not in ("pending", "running"):
                continue
            user = self.db.get_user(int(plan["user_id"]))
            if user is None:
                continue
            try:
                _, end_dt = window_bounds(
                    now.date(),
                    parse_hhmm(user["window_start"]),
                    parse_hhmm(user["window_end"]),
                    self.settings.tz,
                )
            except ValueError:
                continue
            if now <= end_dt:
                continue
            if plan["status"] == "running":
                # Give the worker a grace period before declaring it failed.
                try:
                    updated = datetime.fromisoformat(plan["updated_at"])
                except ValueError:
                    updated = now - timedelta(hours=1)
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=self.settings.tz)
                if (now - updated).total_seconds() < 300:
                    continue
            self.db.update_plan(
                int(plan["id"]),
                status="failed",
                next_attempt_epoch=None,
                last_error="窗口已结束，未能完成打卡",
            )
            self._notify(user, "failed", "今日自动打卡窗口已结束，未能完成打卡")

    # -- execution ----------------------------------------------------------
    def _claim_and_submit(self, plan: dict, user: dict) -> None:
        plan_id = int(plan["id"])
        with self._running_lock:
            if plan_id in self._running:
                return
            self._running.add(plan_id)
        if not self.db.claim_plan(plan_id, utc_now_iso()):
            with self._running_lock:
                self._running.discard(plan_id)
            return
        try:
            self._executor.submit(self._run_plan, plan_id, int(user["id"]))
        except RuntimeError:
            self.db.update_plan(
                plan_id, status="pending", last_error="调度器正在关闭"
            )
            with self._running_lock:
                self._running.discard(plan_id)

    def _run_plan(self, plan_id: int, user_id: int) -> None:
        try:
            plan = self.db.get_plan_by_id(plan_id)
            user = self.db.get_user(user_id)
            if plan is None:
                return
            if user is None or not int(user.get("enabled", 0)):
                self.db.update_plan(
                    plan_id, status="failed", last_error="用户已停用或已删除"
                )
                return

            outcome: Outcome = self.service.run(user)
            attempts = int(plan.get("attempts") or 1)
            now = datetime.now(self.settings.tz)
            try:
                _, end_dt = window_bounds(
                    now.date(),
                    parse_hhmm(user["window_start"]),
                    parse_hhmm(user["window_end"]),
                    self.settings.tz,
                )
                end_epoch = int(end_dt.timestamp())
            except ValueError:
                end_epoch = int(now.timestamp())

            result_json = json.dumps(outcome.to_dict(), ensure_ascii=False)
            if outcome.ok:
                self.db.update_plan(
                    plan_id,
                    status="success",
                    next_attempt_epoch=None,
                    last_error="",
                    result=result_json,
                )
                self._notify(
                    user,
                    "warning" if outcome.warning else "success",
                    outcome.message,
                )
                return

            if outcome.fatal:
                self.db.update_plan(
                    plan_id,
                    status="failed",
                    next_attempt_epoch=None,
                    last_error=outcome.message,
                    result=result_json,
                )
                self._notify(user, "failed", outcome.message)
                return

            retry = None
            if attempts < self.settings.max_attempts:
                retry = next_retry_epoch(
                    int(now.timestamp()),
                    end_epoch,
                    self.settings.retry_interval_sec,
                    attempts,
                    self._rng,
                )
            if retry is None:
                self.db.update_plan(
                    plan_id,
                    status="failed",
                    next_attempt_epoch=None,
                    last_error=(outcome.message + "（已停止重试）")[:1000],
                    result=result_json,
                )
                self._notify(
                    user,
                    "failed",
                    f"{outcome.message}；今日自动打卡未完成，请人工处理",
                )
            else:
                self.db.update_plan(
                    plan_id,
                    status="pending",
                    next_attempt_epoch=retry,
                    last_error=outcome.message,
                    result=result_json,
                )
                logger.info(
                    "retry scheduled for %s at %s",
                    user.get("name"),
                    datetime.fromtimestamp(retry, self.settings.tz).strftime("%H:%M:%S"),
                )
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("plan %s execution crashed", plan_id)
            try:
                self.db.update_plan(
                    plan_id,
                    status="pending",
                    next_attempt_epoch=int(time.time()) + 120,
                    last_error=f"调度异常：{exc}",
                )
            except Exception:
                pass
        finally:
            with self._running_lock:
                self._running.discard(plan_id)

    def _notify(self, user: dict, event: str, message: str) -> None:
        if not self.notifier:
            return
        try:
            self.notifier.notify(user, event, message)
        except Exception:  # pragma: no cover - notification must never break
            logger.exception("notification failed")

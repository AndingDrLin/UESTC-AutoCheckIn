"""Application configuration, loaded from environment variables.

The service is intentionally configuration-light: everything that matters for
daily operation can be set through Docker Compose's ``environment`` block.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

BASE_DIR = Path(__file__).resolve().parent.parent

#: A real desktop Chrome UA captured from the official H5 check-in page.
#: Keep this byte-for-byte identical to the browser UA that produced the
#: fingerprint; the server correlates the two.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"
)

#: The platform's real submission window (observed via the schedule API).
#: It is wider than the default 21:00-23:00 window we schedule in.
PLATFORM_WINDOW_START = time(21, 0)
PLATFORM_WINDOW_END = time(23, 30)

VERSION = "1.0.0"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "y"}


def _env_int(name: str, default: int, minimum: int | None = None) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        value = default
    else:
        try:
            value = int(raw.strip())
        except ValueError as exc:
            raise ValueError(f"{name} must be an integer, got {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {value}")
    return value


def parse_hhmm(value: str | None, default: time | None = None) -> time:
    """Parse ``HH:MM`` (also accepts ``H:MM``) into :class:`datetime.time`."""
    if value is None or str(value).strip() == "":
        if default is not None:
            return default
        raise ValueError("empty time value")
    text = str(value).strip()
    parts = text.split(":")
    if len(parts) != 2:
        raise ValueError(f"invalid HH:MM value: {value!r}")
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise ValueError(f"invalid HH:MM value: {value!r}") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"invalid HH:MM value: {value!r}")
    return time(hour, minute)


def load_tz(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        logging.getLogger(__name__).warning(
            "timezone %r not available, falling back to Asia/Shanghai", name
        )
        return ZoneInfo("Asia/Shanghai")


@dataclass
class Settings:
    """Runtime settings.  All fields have defaults so tests can build one."""

    data_dir: Path = field(default_factory=lambda: BASE_DIR / "data")
    db_path: Path | None = None
    admin_password: str = "changeme"
    app_secret: str = ""
    secret_source: str = "explicit"
    tz_name: str = "Asia/Shanghai"
    window_start: time = time(21, 0)
    window_end: time = time(23, 0)
    retry_interval_sec: int = 180
    max_attempts: int = 20
    min_user_gap_sec: int = 30
    login_cooldown_sec: int = 600
    max_logins_per_day: int = 3
    http_timeout: int = 20
    user_agent: str = DEFAULT_USER_AGENT
    scheduler_enabled: bool = True
    reverse_geocode: bool = False
    cookie_secure: bool = False
    notify_webhook: str = ""
    log_level: str = "INFO"
    host: str = "0.0.0.0"
    port: int = 8000
    version: str = VERSION
    tz: ZoneInfo = field(init=False)

    def __post_init__(self) -> None:
        self.data_dir = Path(self.data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if self.db_path is None:
            self.db_path = self.data_dir / "checkin.db"
        else:
            self.db_path = Path(self.db_path)
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        if self.window_end <= self.window_start:
            raise ValueError(
                "CHECKIN_WINDOW_END must be later than CHECKIN_WINDOW_START "
                f"({self.window_start} -> {self.window_end})"
            )
        self.tz = load_tz(self.tz_name)

    @property
    def is_default_password(self) -> bool:
        return self.admin_password in {"", "changeme", "change-me", "password"}

    def window_text(self) -> str:
        return f"{self.window_start.strftime('%H:%M')}-{self.window_end.strftime('%H:%M')}"

    def public_dict(self) -> dict:
        """Settings that are safe to show in the admin UI."""
        return {
            "version": self.version,
            "data_dir": str(self.data_dir),
            "db_path": str(self.db_path),
            "tz_name": self.tz_name,
            "window_start": self.window_start.strftime("%H:%M"),
            "window_end": self.window_end.strftime("%H:%M"),
            "retry_interval_sec": self.retry_interval_sec,
            "max_attempts": self.max_attempts,
            "min_user_gap_sec": self.min_user_gap_sec,
            "login_cooldown_sec": self.login_cooldown_sec,
            "max_logins_per_day": self.max_logins_per_day,
            "http_timeout": self.http_timeout,
            "host": self.host,
            "port": self.port,
            "scheduler_enabled": self.scheduler_enabled,
            "reverse_geocode": self.reverse_geocode,
            "cookie_secure": self.cookie_secure,
            "notify_webhook": bool(self.notify_webhook),
            "secret_source": self.secret_source,
            "is_default_password": self.is_default_password,
        }


def load_settings() -> Settings:
    """Build :class:`Settings` from the process environment."""
    data_dir = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data"))).expanduser()
    data_dir.mkdir(parents=True, exist_ok=True)

    # Imported lazily so ``config`` can be imported without cryptography during
    # very early startup / tooling.
    from .security import load_or_create_secret

    env_secret = os.getenv("APP_SECRET", "").strip()
    if env_secret:
        secret, secret_source = env_secret, "env"
    else:
        secret, secret_source = load_or_create_secret(data_dir), "file"

    return Settings(
        data_dir=data_dir,
        db_path=Path(os.getenv("DB_PATH", str(data_dir / "checkin.db"))),
        admin_password=os.getenv("ADMIN_PASSWORD", "changeme"),
        app_secret=secret,
        secret_source=secret_source,
        tz_name=os.getenv("TZ", "Asia/Shanghai"),
        window_start=parse_hhmm(os.getenv("CHECKIN_WINDOW_START", "21:00")),
        window_end=parse_hhmm(os.getenv("CHECKIN_WINDOW_END", "23:00")),
        retry_interval_sec=_env_int("RETRY_INTERVAL_SEC", 180, 30),
        max_attempts=_env_int("MAX_ATTEMPTS", 20, 1),
        min_user_gap_sec=_env_int("MIN_USER_GAP_SEC", 30, 0),
        login_cooldown_sec=_env_int("LOGIN_COOLDOWN_SEC", 600, 0),
        max_logins_per_day=_env_int("MAX_LOGINS_PER_DAY", 3, 1),
        http_timeout=_env_int("HTTP_TIMEOUT", 20, 5),
        user_agent=os.getenv("USER_AGENT", DEFAULT_USER_AGENT).strip()
        or DEFAULT_USER_AGENT,
        scheduler_enabled=_env_bool("SCHEDULER_ENABLED", True),
        reverse_geocode=_env_bool("REVERSE_GEOCODE", False),
        cookie_secure=_env_bool("COOKIE_SECURE", False),
        notify_webhook=os.getenv("NOTIFY_WEBHOOK", "").strip(),
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO",
        host=os.getenv("HOST", "0.0.0.0"),
        port=_env_int("PORT", 8000, 1),
    )


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # requests/urllib3 are chatty at DEBUG; keep them at WARNING.
    logging.getLogger("urllib3").setLevel(logging.WARNING)

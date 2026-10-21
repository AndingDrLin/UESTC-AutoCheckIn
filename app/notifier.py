"""Optional out-of-band notifications (ServerChan, Bark, generic webhook)."""
from __future__ import annotations

import logging
from urllib.parse import quote

import requests

from .config import Settings

logger = logging.getLogger(__name__)


class Notifier:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def notify(self, user: dict, event: str, message: str) -> None:
        url = (user.get("notify_url") or self.settings.notify_webhook or "").strip()
        if not url:
            return
        title = f"{user.get('name', '')} 自动打卡"
        try:
            if "{title}" in url or "{body}" in url:
                target = url.replace("{title}", quote(title)).replace(
                    "{body}", quote(message)
                )
                response = requests.get(target, timeout=10)
            elif "ftqq.com" in url or url.rstrip("/").endswith(".send"):
                # ServerChan: https://sctapi.ftqq.com/<key>.send
                response = requests.post(
                    url, data={"title": title, "desp": message}, timeout=10
                )
            else:
                response = requests.post(
                    url,
                    json={
                        "event": event,
                        "title": title,
                        "user": user.get("name", ""),
                        "message": message,
                    },
                    timeout=10,
                )
            if response.status_code >= 400:
                logger.warning(
                    "webhook returned HTTP %s for user %s",
                    response.status_code,
                    user.get("student_id"),
                )
        except requests.RequestException as exc:
            logger.warning("webhook failed for user %s: %s", user.get("student_id"), exc)

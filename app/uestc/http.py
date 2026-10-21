"""Small ``requests`` wrapper: manual redirects, cookie control, safe logging."""
from __future__ import annotations

import json
import logging
import re
from urllib.parse import urljoin

import requests

from .errors import NetworkError

logger = logging.getLogger(__name__)

_TICKET_RE = re.compile(r"(ticket=)[^&\s\"']+")


def mask_url(url: str) -> str:
    """Hide the CAS service ticket so logs can be pasted into issues."""
    return _TICKET_RE.sub(r"\1***", url)


class UestcHttp:
    def __init__(self, user_agent: str, timeout: int = 20) -> None:
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept": "*/*",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Accept-Encoding": "gzip, deflate",
                "Connection": "close",
            }
        )

    # -- core request -------------------------------------------------------
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict | None = None,
        data: dict | bytes | None = None,
        json_body: dict | None = None,
        allow_redirects: bool = False,
        timeout: int | None = None,
    ) -> requests.Response:
        kwargs: dict = {
            "headers": dict(headers or {}),
            "allow_redirects": allow_redirects,
            "timeout": timeout or self.timeout,
        }
        if json_body is not None:
            kwargs["data"] = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            kwargs["headers"].setdefault("Content-Type", "application/json")
        elif data is not None:
            kwargs["data"] = data

        try:
            response = self.session.request(method.upper(), url, **kwargs)
        except requests.RequestException as exc:
            raise NetworkError(f"{method.upper()} {mask_url(url)} 失败: {exc}") from exc

        logger.debug(
            "%s %s -> %s (Location: %s)",
            method.upper(),
            mask_url(url),
            response.status_code,
            mask_url(response.headers.get("Location", "")),
        )
        return response

    def follow(
        self,
        url: str,
        *,
        headers: dict | None = None,
        max_hops: int = 10,
    ) -> requests.Response:
        """Follow 3xx responses one hop at a time so each step is observable."""
        current = url
        for _ in range(max_hops):
            response = self.request("GET", current, headers=headers)
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("Location")
                if not location:
                    return response
                current = urljoin(current, location)
                continue
            return response
        raise NetworkError("重定向次数过多")

    # -- cookies ------------------------------------------------------------
    def set_cookie(self, name: str, value: str, domain: str, path: str = "/") -> None:
        self.session.cookies.set(name, value, domain=domain, path=path)

    def get_cookie(self, name: str) -> str | None:
        for cookie in self.session.cookies:
            if cookie.name == name:
                return cookie.value
        return None

    def clear_cookie(self, name: str) -> None:
        for cookie in list(self.session.cookies):
            if cookie.name == name:
                try:
                    self.session.cookies.clear(cookie.domain, cookie.path, cookie.name)
                except KeyError:
                    pass

    def export_cookies(self, names: tuple[str, ...] = ("SESSION",)) -> dict[str, str]:
        out: dict[str, str] = {}
        for cookie in self.session.cookies:
            if cookie.name in names:
                out[cookie.name] = cookie.value
        return out

    def import_cookies(self, cookies: dict[str, str], domain: str) -> None:
        for name, value in cookies.items():
            if value:
                self.set_cookie(name, value, domain=domain)

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:  # pragma: no cover - best effort
            pass

    def __enter__(self) -> "UestcHttp":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

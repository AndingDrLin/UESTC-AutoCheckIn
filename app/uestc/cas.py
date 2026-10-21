"""Unified identity (CAS) login for UESTC.

Flow (matches the browser exactly):

1. GET the login page, scrape ``execution`` and ``pwdEncryptSalt``.
2. Inject the trusted ``MULTIFACTOR_BROWSER_FINGERPRINT`` cookie.
3. POST the encrypted password.
4. On a 302 carrying ``ticket=ST-...``, follow it to the portal to obtain the
   ``SESSION`` cookie.
"""
from __future__ import annotations

import html as html_lib
import logging
import re
import time
from dataclasses import dataclass
from urllib.parse import quote

from .crypto import encrypt_password
from .errors import LoginFailed, MFARequired, PageChanged, SessionInvalid
from .http import UestcHttp

logger = logging.getLogger(__name__)

CAS_BASE = "https://idas.uestc.edu.cn/authserver"
SERVICE = "https://jzsz.uestc.edu.cn/"
LOGIN_URL = f"{CAS_BASE}/login?service={quote(SERVICE, safe='')}"
BFP_URL = f"{CAS_BASE}/bfp/info"

_RE_EXECUTION = re.compile(
    r"""name\s*=\s*["']execution["'][^>]*?value\s*=\s*["']([^"']+)["']""",
    re.IGNORECASE | re.DOTALL,
)
_RE_EXECUTION_REV = re.compile(
    r"""value\s*=\s*["']([^"']+)["'][^>]*?name\s*=\s*["']execution["']""",
    re.IGNORECASE | re.DOTALL,
)
_RE_SALT = re.compile(
    r"""id\s*=\s*["']pwdEncryptSalt["'][^>]*?value\s*=\s*["']([^"']*)["']""",
    re.IGNORECASE | re.DOTALL,
)
_RE_SALT_REV = re.compile(
    r"""value\s*=\s*["']([^"']*)["'][^>]*?id\s*=\s*["']pwdEncryptSalt["']""",
    re.IGNORECASE | re.DOTALL,
)
_RE_MFA_LOCATION = re.compile(
    r"isMultifactor|reAuthLoginView|authLogin|multifactor|二次验证|多因素认证",
    re.IGNORECASE,
)
# The body of an ordinary failed login page can contain unrelated script or
# link text such as "authLogin"; only match the unambiguous MFA markers there.
_RE_MFA_BODY = re.compile(
    r"isMultifactor|reAuthLoginView|二次验证|多因素认证",
    re.IGNORECASE,
)
_RE_ERROR_BLOCK = re.compile(
    r"""<(?:span|div|p)[^>]*(?:id|class)\s*=\s*["'][^"']*(?:msg|error|auth_error)[^"']*["'][^>]*>(.*?)</(?:span|div|p)>""",
    re.IGNORECASE | re.DOTALL,
)
_RE_JSON_MSG = re.compile(r'"msg"\s*:\s*"([^"]+)"')

_LOGIN_PAGE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}

_LOGIN_POST_HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded",
    "Origin": "https://idas.uestc.edu.cn",
    "Referer": LOGIN_URL,
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
}


@dataclass
class LoginPage:
    execution: str
    salt: str


@dataclass
class LoginResult:
    session: str
    final_url: str


def fetch_login_page(client: UestcHttp) -> LoginPage:
    response = client.follow(LOGIN_URL, headers=_LOGIN_PAGE_HEADERS)
    if response.status_code != 200:
        raise PageChanged(
            f"统一身份认证登录页返回 HTTP {response.status_code}，可能被风控或网络受限"
        )
    text = response.text
    match = _RE_EXECUTION.search(text) or _RE_EXECUTION_REV.search(text)
    salt_match = _RE_SALT.search(text) or _RE_SALT_REV.search(text)
    if not match or not salt_match:
        raise PageChanged(
            "登录页结构已变化：未找到 execution 或 pwdEncryptSalt，"
            "请检查 https://idas.uestc.edu.cn/authserver/login 是否改版"
        )
    return LoginPage(execution=match.group(1), salt=salt_match.group(1))


def set_fingerprint_cookie(client: UestcHttp, fingerprint: str) -> None:
    value = (fingerprint or "").strip().upper()
    if not value:
        return
    # Same as the browser after a successful trusted login.
    client.set_cookie(
        "MULTIFACTOR_BROWSER_FINGERPRINT",
        value,
        domain="idas.uestc.edu.cn",
        path="/",
    )


def report_fingerprint(client: UestcHttp, fingerprint: str) -> None:
    """Best-effort equivalent of the browser's ``/authserver/bfp/info`` call."""
    value = (fingerprint or "").strip().upper()
    if not value:
        return
    try:
        client.request(
            "GET",
            BFP_URL,
            headers={
                "Accept": "application/json, text/plain, */*",
                "Referer": LOGIN_URL,
                "Sec-Fetch-Site": "same-origin",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Dest": "empty",
            },
        )
    except Exception:  # pragma: no cover - optional telemetry only
        logger.debug("bfp/info call failed (ignored)", exc_info=True)


def submit_login(
    client: UestcHttp,
    username: str,
    password: str,
    page: LoginPage,
) -> "object":
    encrypted = encrypt_password(password, page.salt)
    data = {
        "username": username,
        "password": encrypted,
        "captcha": "",
        "_eventId": "submit",
        "cllt": "userNameLogin",
        "dllt": "generalLogin",
        "lt": "",
        "execution": page.execution,
    }
    return client.request(
        "POST", LOGIN_URL, data=data, headers=_LOGIN_POST_HEADERS
    )


def _clean_html(fragment: str) -> str:
    text = re.sub(r"<[^>]+>", " ", fragment)
    text = html_lib.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def extract_login_error(text: str) -> str:
    block = _RE_ERROR_BLOCK.search(text)
    if block:
        cleaned = _clean_html(block.group(1))
        if cleaned:
            return cleaned[:300]
    json_msg = _RE_JSON_MSG.search(text)
    if json_msg:
        return json_msg.group(1)[:300]
    for phrase in (
        "用户名或密码错误",
        "您提供的用户名或者密码有误",
        "密码错误",
        "账号不存在",
        "用户不存在",
        "账号被锁定",
        "账户被锁定",
        "验证码错误",
        "登录失败",
    ):
        if phrase in text:
            return phrase
    return ""


def classify_login(response) -> tuple[str, str]:
    """Return ``(verdict, detail)`` where verdict is OK / MFA / FAIL."""
    location = response.headers.get("Location", "") or ""
    body = response.text or ""
    if location and "ticket=ST-" in location:
        return "OK", location
    if location and _RE_MFA_LOCATION.search(location):
        return "MFA", location
    if _RE_MFA_BODY.search(body):
        return "MFA", ""

    if response.status_code == 200:
        error = extract_login_error(body)
        return "FAIL", error or "CAS 未返回票据（密码错误、账号异常或页面结构变化）"
    if location:
        return "FAIL", f"CAS 返回了意外的重定向：{location[:200]}"
    return "FAIL", f"CAS 返回 HTTP {response.status_code}，未拿到票据"


def login(
    client: UestcHttp,
    username: str,
    password: str,
    fingerprint: str = "",
) -> LoginResult:
    """Perform a full CAS login and return the portal ``SESSION`` cookie."""
    # Never let an anonymous SESSION overwrite the authenticated one.
    client.clear_cookie("SESSION")

    page = fetch_login_page(client)
    if fingerprint:
        set_fingerprint_cookie(client, fingerprint)

    response = submit_login(client, username, password, page)
    verdict, detail = classify_login(response)

    if verdict == "MFA":
        raise MFARequired(
            "统一身份认证要求二次验证。请用浏览器在常用设备上登录一次，"
            "从开发者工具复制 MULTIFACTOR_BROWSER_FINGERPRINT Cookie 填入用户配置。"
        )
    if verdict != "OK":
        raise LoginFailed(detail or "统一身份认证登录失败")

    final = client.follow(detail)
    session = client.get_cookie("SESSION")
    if not session:
        raise LoginFailed(
            f"已拿到 CAS 票据，但门户未下发 SESSION（HTTP {final.status_code}）。"
            "请确认 https://jzsz.uestc.edu.cn 可访问。"
        )
    logger.info("CAS login succeeded for %s", username)
    return LoginResult(session=session, final_url=final.url)


def inject_session(client: UestcHttp, session: str) -> None:
    if not session:
        raise SessionInvalid("没有可用的 SESSION")
    client.set_cookie("SESSION", session, domain="jzsz.uestc.edu.cn", path="/")


def now_ms() -> int:
    return int(time.time() * 1000)

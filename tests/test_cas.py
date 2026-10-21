"""CAS login tests with a fake HTTP client (no network access)."""
from __future__ import annotations

import pytest

from app.uestc.cas import (
    LOGIN_URL,
    LoginPage,
    classify_login,
    login,
    submit_login,
)
from app.uestc.errors import LoginFailed, MFARequired


class FakeResponse:
    def __init__(self, status_code: int = 200, text: str = "", headers=None, url: str = ""):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}
        self.url = url


class FakeClient:
    def __init__(self, login_page_html: str, post_response: FakeResponse) -> None:
        self.login_page_html = login_page_html
        self.post_response = post_response
        self.requests: list[dict] = []
        self.follows: list[str] = []
        self.cleared: list[str] = []
        self.cookies: list[tuple[str, str, str]] = []
        self.session_value = "SESSION-abc"

    def clear_cookie(self, name: str) -> None:
        self.cleared.append(name)

    def set_cookie(self, name: str, value: str, domain: str, path: str = "/") -> None:
        self.cookies.append((name, value, domain))

    def get_cookie(self, name: str):
        return self.session_value if name == "SESSION" else None

    def follow(self, url: str, headers=None, max_hops: int = 10) -> FakeResponse:
        self.follows.append(url)
        if url == LOGIN_URL:
            return FakeResponse(200, self.login_page_html)
        return FakeResponse(200, url=url)

    def request(self, method: str, url: str, **kwargs) -> FakeResponse:
        self.requests.append({"method": method, "url": url, **kwargs})
        return self.post_response


LOGIN_HTML = """
<form>
  <input name="execution" value="exec-123" />
  <input id="pwdEncryptSalt" value="0123456789abcdef" />
</form>
"""


def test_fetch_login_page_fields_and_post_form():
    page = LoginPage(execution="exec-123", salt="0123456789abcdef")
    post = FakeResponse(302, headers={"Location": "https://jzsz.uestc.edu.cn/?ticket=ST-1"})
    client = FakeClient(LOGIN_HTML, post)

    from app.uestc.cas import fetch_login_page

    fetched = fetch_login_page(client)
    assert fetched.execution == "exec-123"
    assert fetched.salt == "0123456789abcdef"

    submit_login(client, "20200001", "plain-password", page)
    call = client.requests[-1]
    assert call["method"] == "POST"
    assert call["url"] == LOGIN_URL
    data = call["data"]
    assert data["username"] == "20200001"
    assert data["execution"] == "exec-123"
    assert data["lt"] == ""
    assert data["_eventId"] == "submit"
    assert data["cllt"] == "userNameLogin"
    assert data["dllt"] == "generalLogin"
    assert data["password"] != "plain-password"
    assert "." not in data["password"]
    assert len(data["password"]) % 4 == 0


def test_login_success_clears_old_session_and_gets_portal_session():
    post = FakeResponse(302, headers={"Location": "https://jzsz.uestc.edu.cn/?ticket=ST-2"})
    client = FakeClient(LOGIN_HTML, post)

    result = login(client, "20200001", "plain-password", "ABCDEF0123456789ABCDEF0123456789")

    assert client.cleared == ["SESSION"]
    assert client.follows[0] == LOGIN_URL
    assert result.session == "SESSION-abc"
    assert any(name == "MULTIFACTOR_BROWSER_FINGERPRINT" for name, _, _ in client.cookies)


def test_login_mfa_and_wrong_password_are_distinguished():
    mfa = FakeResponse(302, headers={"Location": "https://idas.uestc.edu.cn/authserver/reAuthLoginView"})
    with pytest.raises(MFARequired):
        login(FakeClient(LOGIN_HTML, mfa), "20200001", "x", "")

    failed = FakeResponse(200, '<div id="msg">用户名或密码错误</div>')
    with pytest.raises(LoginFailed):
        login(FakeClient(LOGIN_HTML, failed), "20200001", "x", "")


def test_classify_login_does_not_treat_incidental_authlogin_as_mfa():
    response = FakeResponse(200, '<script src="/authLogin.js"></script><div>密码错误</div>')
    verdict, _ = classify_login(response)
    assert verdict == "FAIL"

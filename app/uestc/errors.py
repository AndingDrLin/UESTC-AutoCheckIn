"""Exception types used by the UESTC client.

The distinction that matters operationally:

* :class:`SessionInvalid` -- retryable by logging in again.
* :class:`MFARequired` / :class:`LoginFailed` / :class:`PageChanged` -- fatal
  until the user fixes credentials / fingerprint / the site changes.
* :class:`NetworkError` / :class:`HttpStatusError` -- transient, retry later.
"""
from __future__ import annotations


class UestcError(Exception):
    """Base class for every expected client failure."""


class NetworkError(UestcError):
    """Connection failure, timeout, TLS error, too many redirects."""


class SessionInvalid(UestcError):
    """The portal session is missing/expired or the micro-app was not authorized."""


class MFARequired(UestcError):
    """CAS asked for a second factor; the fingerprint is not trusted."""


class LoginFailed(UestcError):
    """CAS rejected the username/password or returned an unknown page."""


class PageChanged(UestcError):
    """The login page no longer contains the fields we rely on."""


class HttpStatusError(UestcError):
    def __init__(self, status: int, url: str, body: str = "") -> None:
        self.status = status
        self.url = url
        self.body = body
        super().__init__(f"HTTP {status} for {url}")


class ApiError(UestcError):
    def __init__(self, code: int | str, message: str) -> None:
        self.code = code
        self.message = message or "接口返回错误"
        super().__init__(f"[{code}] {self.message}")


class ServiceError(UestcError):
    """A clean, user-facing error raised by the service layer."""

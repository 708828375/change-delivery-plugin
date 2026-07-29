from __future__ import annotations

import json
import ipaddress
import time
from collections.abc import Callable
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from change_delivery.errors import DeliveryError


class HttpMethodNotAllowed(DeliveryError):
    """Raised when a caller attempts a mutating HTTP method."""


class HttpReadError(DeliveryError):
    """Raised when a read-only HTTP request cannot produce JSON."""


class Response(Protocol):
    headers: object

    def __enter__(self) -> Response: ...

    def __exit__(self, *args: object) -> object: ...

    def read(self, size: int = -1) -> bytes: ...

    def geturl(self) -> str: ...


Opener = Callable[..., Response]
_MAX_BODY_BYTES = 65_536
_MAX_RETRY_AFTER_SECONDS = 5.0
_RETRYABLE_HTTP_STATUS = frozenset({408, 429})


def validate_read_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme.casefold() != "https" or not parsed.hostname:
        raise ValueError("read-only HTTP URL must use HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("credentials are forbidden in HTTP URLs")
    if parsed.query or parsed.fragment:
        raise ValueError("query and fragment are forbidden in HTTP URLs")
    if "%" in parsed.hostname:
        raise ValueError("scoped IP literals are forbidden")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        return
    if not address.is_global:
        raise ValueError("non-global IP literals are forbidden")


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


def _default_opener(request: Request, *, timeout: float) -> Response:
    return build_opener(_NoRedirectHandler()).open(request, timeout=timeout)


class ReadOnlyHttpClient:
    def __init__(
        self,
        *,
        timeout_seconds: float = 10.0,
        attempts: int = 3,
        opener: Opener = _default_opener,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("HTTP timeout must be positive")
        if attempts != 3:
            raise ValueError("read-only HTTP must use exactly three attempts")
        self._timeout_seconds = float(timeout_seconds)
        self._attempts = attempts
        self._opener = opener
        self._sleep = sleep
        self._monotonic = monotonic

    def get_json(self, url: str) -> object:
        return self.request_json("GET", url)

    def request_json(self, method: str, url: str) -> object:
        if method != "GET":
            raise HttpMethodNotAllowed("read-only HTTP permits GET only")
        validate_read_url(url)

        request = Request(url, method="GET")
        deadline = self._monotonic() + self._timeout_seconds
        for attempt in range(self._attempts):
            try:
                remaining = deadline - self._monotonic()
                if remaining <= 0:
                    raise HttpReadError("GET exceeded total deadline")
                with self._opener(
                    request,
                    timeout=remaining,
                ) as response:
                    if response.geturl() != url:
                        raise HttpReadError("HTTP redirect is forbidden")
                    body = self._read_bounded(response, deadline)
                return json.loads(body)
            except HTTPError as error:
                if 300 <= error.code < 400:
                    raise HttpReadError("HTTP redirect is forbidden") from error
                if not (
                    error.code in _RETRYABLE_HTTP_STATUS
                    or 500 <= error.code < 600
                ):
                    raise HttpReadError(
                        f"GET returned HTTP status {error.code}"
                    ) from error
                delay = self._retry_delay(error, attempt)
                self._retry_or_raise(error, attempt, deadline, delay)
            except (URLError, TimeoutError) as error:
                self._retry_or_raise(
                    error,
                    attempt,
                    deadline,
                    float(2**attempt),
                )
            except (UnicodeError, json.JSONDecodeError) as error:
                raise HttpReadError("GET response was not valid JSON") from error

        raise AssertionError("unreachable")

    def _read_bounded(self, response: Response, deadline: float) -> bytes:
        raw_length = getattr(response.headers, "get", lambda _key: None)(
            "Content-Length"
        )
        if raw_length is not None:
            try:
                declared_length = int(raw_length)
            except (TypeError, ValueError) as error:
                raise HttpReadError("invalid Content-Length") from error
            if declared_length < 0 or declared_length > _MAX_BODY_BYTES:
                raise HttpReadError("GET response body is too large")

        chunks: list[bytes] = []
        total = 0
        while True:
            if self._monotonic() >= deadline:
                raise HttpReadError("GET exceeded total deadline")
            chunk = response.read(min(4096, _MAX_BODY_BYTES + 1 - total))
            if self._monotonic() >= deadline:
                raise HttpReadError("GET exceeded total deadline")
            if not chunk:
                break
            total += len(chunk)
            if total > _MAX_BODY_BYTES:
                raise HttpReadError("GET response body is too large")
            chunks.append(chunk)
        return b"".join(chunks)

    def _retry_or_raise(
        self,
        error: Exception,
        attempt: int,
        deadline: float,
        delay: float,
    ) -> None:
        if attempt == self._attempts - 1:
            raise HttpReadError("GET failed after three attempts") from error
        remaining = deadline - self._monotonic()
        if remaining <= 0 or delay >= remaining:
            raise HttpReadError("GET exceeded total deadline") from error
        self._sleep(delay)

    @staticmethod
    def _retry_delay(error: HTTPError, attempt: int) -> float:
        retry_after = error.headers.get("Retry-After")
        if retry_after is not None:
            try:
                return min(
                    max(float(retry_after), 0.0),
                    _MAX_RETRY_AFTER_SECONDS,
                )
            except ValueError:
                pass
        return float(2**attempt)

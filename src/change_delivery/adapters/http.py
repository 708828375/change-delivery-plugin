from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Protocol
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from change_delivery.errors import DeliveryError


class HttpMethodNotAllowed(DeliveryError):
    """Raised when a caller attempts a mutating HTTP method."""


class HttpReadError(DeliveryError):
    """Raised when a read-only HTTP request cannot produce JSON."""


class Response(Protocol):
    def __enter__(self) -> Response: ...

    def __exit__(self, *args: object) -> object: ...

    def read(self) -> bytes: ...


Opener = Callable[..., Response]


class ReadOnlyHttpClient:
    def __init__(
        self,
        *,
        timeout_seconds: float = 10.0,
        attempts: int = 3,
        opener: Opener = urlopen,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("HTTP timeout must be positive")
        if attempts != 3:
            raise ValueError("read-only HTTP must use exactly three attempts")
        self._timeout_seconds = float(timeout_seconds)
        self._attempts = attempts
        self._opener = opener
        self._sleep = sleep

    def get_json(self, url: str) -> object:
        return self.request_json("GET", url)

    def request_json(self, method: str, url: str) -> object:
        if method != "GET":
            raise HttpMethodNotAllowed("read-only HTTP permits GET only")
        parsed = urlsplit(url)
        if parsed.scheme.casefold() != "https" or not parsed.hostname:
            raise ValueError("read-only HTTP URL must use HTTPS")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("credentials are forbidden in HTTP URLs")

        request = Request(url, method="GET")
        for attempt in range(self._attempts):
            try:
                with self._opener(
                    request,
                    timeout=self._timeout_seconds,
                ) as response:
                    body = response.read()
                return json.loads(body)
            except (URLError, TimeoutError) as error:
                if attempt == self._attempts - 1:
                    raise HttpReadError(
                        "GET failed after three attempts"
                    ) from error
                self._sleep(float(2**attempt))
            except (UnicodeError, json.JSONDecodeError) as error:
                raise HttpReadError("GET response was not valid JSON") from error

        raise AssertionError("unreachable")

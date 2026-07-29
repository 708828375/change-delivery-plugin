from __future__ import annotations

from abc import ABC, abstractmethod
import http.client
import ipaddress
import json
import multiprocessing
import socket
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from urllib.error import URLError
from urllib.parse import urlsplit

from change_delivery.errors import DeliveryError


class HttpMethodNotAllowed(DeliveryError):
    """Raised when a caller attempts a mutating HTTP method."""


class HttpReadError(DeliveryError):
    """Raised when a secure read cannot produce bounded JSON."""


class SecureHttpResponse(Protocol):
    status: int
    headers: object

    def __enter__(self) -> SecureHttpResponse: ...

    def __exit__(self, *args: object) -> object: ...

    def read(self, size: int = -1) -> bytes: ...

    def read1(self, size: int = -1) -> bytes: ...

    def geturl(self) -> str: ...

    def set_read_timeout(self, timeout: float) -> None: ...


class SecureHttpTransport(ABC):
    @abstractmethod
    def get(self, url: str, *, timeout: float) -> SecureHttpResponse:
        """Resolve, attest, pin and open one non-redirecting HTTPS GET."""


class AddressResolver(ABC):
    @abstractmethod
    def resolve(
        self,
        hostname: str,
        port: int,
        *,
        deadline: float,
    ) -> tuple[str, ...]:
        """Return one frozen address set before the absolute deadline."""


@dataclass(frozen=True)
class AddressAttestation:
    url: str
    hostname: str
    port: int
    path: str
    addresses: tuple[str, ...]
    pinned_ip: str


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


def validate_public_addresses(addresses: tuple[str, ...]) -> tuple[str, ...]:
    if not addresses:
        raise HttpReadError("HTTPS hostname returned no addresses")
    try:
        parsed = tuple(ipaddress.ip_address(address) for address in addresses)
    except ValueError as error:
        raise HttpReadError("HTTPS resolver returned an invalid address") from error
    if any(not address.is_global for address in parsed):
        raise HttpReadError("HTTPS hostname must resolve only to public addresses")
    return tuple(str(address) for address in parsed)


def frozen_address_attestations(
    url: str,
    addresses: tuple[str, ...],
) -> tuple[AddressAttestation, ...]:
    validate_read_url(url)
    parsed = urlsplit(url)
    assert parsed.hostname is not None
    frozen = validate_public_addresses(tuple(dict.fromkeys(addresses)))
    port = parsed.port or 443
    path = parsed.path or "/"
    return tuple(
        AddressAttestation(
            url=url,
            hostname=parsed.hostname,
            port=port,
            path=path,
            addresses=frozen,
            pinned_ip=address,
        )
        for address in frozen
    )


def _system_dns_worker(hostname: str, port: int) -> tuple[str, ...]:
    answers = socket.getaddrinfo(
        hostname,
        port,
        family=socket.AF_UNSPEC,
        type=socket.SOCK_STREAM,
        proto=socket.IPPROTO_TCP,
    )
    return tuple(dict.fromkeys(str(answer[4][0]) for answer in answers))


def _resolver_process(
    sender: object,
    worker: Callable[[str, int], tuple[str, ...]],
    hostname: str,
    port: int,
) -> None:
    try:
        result = worker(hostname, port)
        sender.send(("ok", result))
    except BaseException as error:
        sender.send(("error", type(error).__name__))
    finally:
        sender.close()


class MultiprocessAddressResolver(AddressResolver):
    def __init__(
        self,
        *,
        worker: Callable[[str, int], tuple[str, ...]] = _system_dns_worker,
    ) -> None:
        self._worker = worker

    def resolve(
        self,
        hostname: str,
        port: int,
        *,
        deadline: float,
    ) -> tuple[str, ...]:
        context = multiprocessing.get_context("spawn")
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(
            target=_resolver_process,
            args=(sender, self._worker, hostname, port),
            daemon=True,
        )
        process.start()
        sender.close()
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not receiver.poll(remaining):
                raise HttpReadError("DNS resolution exceeded hard deadline")
            status, payload = receiver.recv()
            if status != "ok":
                raise HttpReadError("HTTPS hostname resolution failed")
            if not isinstance(payload, tuple) or any(
                not isinstance(item, str) for item in payload
            ):
                raise HttpReadError("HTTPS resolver returned invalid output")
            return payload
        finally:
            receiver.close()
            if process.is_alive():
                process.terminate()
            process.join(0.5)
            if process.is_alive():
                process.kill()
                process.join()


def _read_exchange_body(
    connection: _PinnedHTTPSConnection,
    response: http.client.HTTPResponse,
    deadline: float,
) -> bytes:
    raw_length = response.headers.get("Content-Length")
    if raw_length is not None:
        try:
            declared = int(raw_length)
        except ValueError as error:
            raise HttpReadError("invalid Content-Length") from error
        if declared < 0 or declared > _MAX_BODY_BYTES:
            raise HttpReadError("GET response body is too large")
    read1 = getattr(response, "read1", None)
    if not callable(read1):
        raise HttpReadError("HTTP response lacks single-read support")
    chunks: list[bytes] = []
    total = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or connection.sock is None:
            raise HttpReadError("HTTPS exchange exceeded hard deadline")
        connection.sock.settimeout(remaining)
        chunk = read1(min(4096, _MAX_BODY_BYTES + 1 - total))
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > _MAX_BODY_BYTES:
            raise HttpReadError("GET response body is too large")
        chunks.append(chunk)


def _secure_exchange_worker(url: str, timeout: float) -> dict[str, object]:
    validate_read_url(url)
    deadline = time.monotonic() + timeout
    parsed = urlsplit(url)
    assert parsed.hostname is not None
    addresses = _system_dns_worker(parsed.hostname, parsed.port or 443)
    attestations = frozen_address_attestations(url, addresses)
    last_error: OSError | None = None
    for attestation in attestations:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise HttpReadError("HTTPS exchange exceeded hard deadline")
        connection = _PinnedHTTPSConnection(
            attestation.hostname,
            attestation.pinned_ip,
            attestation.port,
            remaining,
        )
        try:
            connection.request("GET", attestation.path)
            if connection.sock is None:
                raise HttpReadError("HTTPS socket is unavailable")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HttpReadError("HTTPS exchange exceeded hard deadline")
            connection.sock.settimeout(remaining)
            response = connection.getresponse()
            safe_headers = {
                key: value
                for key in ("Content-Length", "Retry-After")
                if (value := response.headers.get(key)) is not None
            }
            body = (
                b""
                if 300 <= response.status < 400
                else _read_exchange_body(connection, response, deadline)
            )
            return {
                "status": response.status,
                "headers": safe_headers,
                "body": body,
                "url": url,
            }
        except OSError as error:
            last_error = error
        finally:
            connection.close()
    raise HttpReadError(
        "HTTPS connection failed for all frozen addresses"
    ) from last_error


def _exchange_process(
    sender: object,
    worker: Callable[[str, float], dict[str, object]],
    url: str,
    timeout: float,
) -> None:
    try:
        sender.send(("ok", worker(url, timeout)))
    except BaseException as error:
        sender.send(("error", type(error).__name__))
    finally:
        sender.close()


class _BufferedSecureResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        status = payload.get("status")
        headers = payload.get("headers")
        body = payload.get("body")
        url = payload.get("url")
        if (
            not isinstance(status, int)
            or not isinstance(headers, dict)
            or any(
                not isinstance(key, str) or not isinstance(value, str)
                for key, value in headers.items()
            )
            or not isinstance(body, bytes)
            or len(body) > _MAX_BODY_BYTES
            or not isinstance(url, str)
        ):
            raise HttpReadError("secure exchange returned invalid output")
        self.status = status
        self.headers = headers
        self._body = body
        self._url = url
        self._offset = 0

    def __enter__(self) -> _BufferedSecureResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        return self.read1(size)

    def read1(self, size: int = -1) -> bytes:
        if size < 0:
            size = len(self._body) - self._offset
        chunk = self._body[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk

    def geturl(self) -> str:
        return self._url

    def set_read_timeout(self, timeout: float) -> None:
        if timeout <= 0:
            raise HttpReadError("GET exceeded total deadline")


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(
        self,
        hostname: str,
        pinned_ip: str,
        port: int,
        timeout: float,
    ) -> None:
        super().__init__(
            hostname,
            port=port,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
        self._pinned_ip = pinned_ip

    def connect(self) -> None:
        raw = socket.create_connection(
            (self._pinned_ip, self.port),
            self.timeout,
            self.source_address,
        )
        self.sock = self._context.wrap_socket(
            raw,
            server_hostname=self.host,
        )


class PinnedHttpsTransport(SecureHttpTransport):
    def __init__(
        self,
        *,
        worker: Callable[[str, float], dict[str, object]] = _secure_exchange_worker,
    ) -> None:
        self._worker = worker

    def get(self, url: str, *, timeout: float) -> SecureHttpResponse:
        validate_read_url(url)
        if timeout <= 0:
            raise HttpReadError("HTTPS exchange exceeded hard deadline")
        context = multiprocessing.get_context("spawn")
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(
            target=_exchange_process,
            args=(sender, self._worker, url, timeout),
            daemon=True,
        )
        started = time.monotonic()
        process.start()
        sender.close()
        try:
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0 or not receiver.poll(remaining):
                raise HttpReadError("HTTPS exchange exceeded hard deadline")
            status, payload = receiver.recv()
            if status != "ok" or not isinstance(payload, dict):
                raise HttpReadError("secure HTTPS exchange failed")
            return _BufferedSecureResponse(payload)
        finally:
            receiver.close()
            if process.is_alive():
                process.terminate()
            process.join(0.5)
            if process.is_alive():
                process.kill()
                process.join()


class ReadOnlyHttpClient:
    def __init__(
        self,
        *,
        timeout_seconds: float = 10.0,
        attempts: int = 3,
        transport: SecureHttpTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("HTTP timeout must be positive")
        if attempts != 3:
            raise ValueError("read-only HTTP must use exactly three attempts")
        selected = transport or PinnedHttpsTransport()
        if not isinstance(selected, SecureHttpTransport):
            raise TypeError("transport must be a trusted SecureHttpTransport")
        self._timeout_seconds = float(timeout_seconds)
        self._attempts = attempts
        self._transport = selected
        self._sleep = sleep
        self._monotonic = monotonic

    def get_json(self, url: str) -> object:
        return self.request_json("GET", url)

    def request_json(self, method: str, url: str) -> object:
        if method != "GET":
            raise HttpMethodNotAllowed("read-only HTTP permits GET only")
        validate_read_url(url)
        deadline = self._monotonic() + self._timeout_seconds
        for attempt in range(self._attempts):
            try:
                remaining = deadline - self._monotonic()
                if remaining <= 0:
                    raise HttpReadError("GET exceeded total deadline")
                with self._transport.get(url, timeout=remaining) as response:
                    if response.geturl() != url:
                        raise HttpReadError("HTTP redirect is forbidden")
                    if 300 <= response.status < 400:
                        raise HttpReadError("HTTP redirect is forbidden")
                    if response.status >= 400:
                        if (
                            response.status in _RETRYABLE_HTTP_STATUS
                            or 500 <= response.status < 600
                        ):
                            delay = self._retry_delay(response, attempt)
                            self._retry_or_raise(
                                HttpReadError("retryable HTTP status"),
                                attempt,
                                deadline,
                                delay,
                            )
                            continue
                        raise HttpReadError(
                            f"GET returned HTTP status {response.status}"
                        )
                    body = self._read_bounded(response, deadline)
                return json.loads(body)
            except (URLError, TimeoutError, OSError) as error:
                self._retry_or_raise(
                    error,
                    attempt,
                    deadline,
                    float(2**attempt),
                )
            except (UnicodeError, json.JSONDecodeError) as error:
                raise HttpReadError("GET response was not valid JSON") from error
        raise AssertionError("unreachable")

    def _read_bounded(
        self,
        response: SecureHttpResponse,
        deadline: float,
    ) -> bytes:
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
        set_read_timeout = getattr(response, "set_read_timeout", None)
        if not callable(set_read_timeout):
            raise HttpReadError("transport cannot enforce read deadline")
        while True:
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise HttpReadError("GET exceeded total deadline")
            try:
                set_read_timeout(remaining)
            except HttpReadError:
                raise
            except Exception as error:
                raise HttpReadError(
                    "transport cannot enforce read deadline"
                ) from error
            read1 = getattr(response, "read1", None)
            if not callable(read1):
                raise HttpReadError(
                    "transport lacks a single-read body contract"
                )
            chunk = read1(min(4096, _MAX_BODY_BYTES + 1 - total))
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
    def _retry_delay(
        response: SecureHttpResponse,
        attempt: int,
    ) -> float:
        retry_after = response.headers.get("Retry-After")
        if retry_after is not None:
            try:
                return min(
                    max(float(retry_after), 0.0),
                    _MAX_RETRY_AFTER_SECONDS,
                )
            except ValueError:
                pass
        return float(2**attempt)

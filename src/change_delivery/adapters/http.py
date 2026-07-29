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


Connector = Callable[[AddressAttestation, float], SecureHttpResponse]
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


class _LiveResponse:
    def __init__(
        self,
        connection: _PinnedHTTPSConnection,
        response: http.client.HTTPResponse,
        url: str,
    ) -> None:
        self._connection = connection
        self._response = response
        self._url = url
        self.status = response.status
        self.headers = response.headers

    def __enter__(self) -> _LiveResponse:
        return self

    def __exit__(self, *args: object) -> None:
        self._connection.close()

    def read(self, size: int = -1) -> bytes:
        return self._response.read(size)

    def read1(self, size: int = -1) -> bytes:
        read1 = getattr(self._response, "read1", None)
        if not callable(read1):
            raise HttpReadError("HTTP response lacks single-read support")
        return read1(size)

    def geturl(self) -> str:
        return self._url

    def set_read_timeout(self, timeout: float) -> None:
        stream = getattr(self._response, "fp", None)
        raw = getattr(stream, "raw", None)
        sock = getattr(raw, "_sock", None)
        if sock is None or not hasattr(sock, "settimeout"):
            raise HttpReadError("transport cannot enforce read deadline")
        sock.settimeout(timeout)


def _connect_pinned(
    attestation: AddressAttestation,
    timeout: float,
) -> SecureHttpResponse:
    connection = _PinnedHTTPSConnection(
        attestation.hostname,
        attestation.pinned_ip,
        attestation.port,
        timeout,
    )
    try:
        connection.request("GET", attestation.path)
        response = connection.getresponse()
    except Exception:
        connection.close()
        raise
    return _LiveResponse(connection, response, attestation.url)


class PinnedHttpsTransport(SecureHttpTransport):
    def __init__(
        self,
        *,
        resolver: AddressResolver | None = None,
        connector: Connector = _connect_pinned,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        selected = resolver or MultiprocessAddressResolver()
        if not isinstance(selected, AddressResolver):
            raise TypeError("resolver must enforce an absolute deadline")
        self._resolver = selected
        self._connector = connector
        self._monotonic = monotonic

    def get(self, url: str, *, timeout: float) -> SecureHttpResponse:
        validate_read_url(url)
        parsed = urlsplit(url)
        assert parsed.hostname is not None
        port = parsed.port or 443
        deadline = self._monotonic() + timeout
        try:
            addresses = self._resolver.resolve(
                parsed.hostname,
                port,
                deadline=deadline,
            )
        except OSError as error:
            raise HttpReadError("HTTPS hostname resolution failed") from error
        addresses = tuple(dict.fromkeys(addresses))
        if not addresses:
            raise HttpReadError("HTTPS hostname returned no addresses")
        try:
            parsed_addresses = tuple(
                ipaddress.ip_address(address) for address in addresses
            )
        except ValueError as error:
            raise HttpReadError("HTTPS resolver returned an invalid address") from error
        if any(not address.is_global for address in parsed_addresses):
            raise HttpReadError("HTTPS hostname must resolve only to public addresses")
        path = parsed.path or "/"
        frozen_addresses = tuple(str(address) for address in parsed_addresses)
        last_error: OSError | None = None
        for address in frozen_addresses:
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise HttpReadError("HTTPS connect exceeded hard deadline")
            attestation = AddressAttestation(
                url=url,
                hostname=parsed.hostname,
                port=port,
                path=path,
                addresses=frozen_addresses,
                pinned_ip=address,
            )
            try:
                return self._connector(attestation, remaining)
            except OSError as error:
                last_error = error
        raise HttpReadError(
            "HTTPS connection failed for all frozen addresses"
        ) from last_error


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

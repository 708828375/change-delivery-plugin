from collections.abc import Mapping
import re


_AUTHORIZATION = re.compile(
    r"(\bauthorization[ \t]*:[ \t]*(?:bearer|basic)[ \t]+)"
    r"[^\s,;]+",
    flags=re.IGNORECASE,
)
_COOKIE = re.compile(
    r"(\bcookie[ \t]*:[ \t]*)[^\r\n]*?"
    r"(?=(?<!;)[ \t]+(?:status|trace|trace_id|traceparent)[ \t]*=|[\r\n]|$)",
    flags=re.IGNORECASE,
)
_SENSITIVE_KEYS = (
    r"(?:access_token|refresh_token|api_key|client_secret|password|token)"
)
_JSON_CREDENTIAL = re.compile(
    rf'("(?P<key>{_SENSITIVE_KEYS})"[ \t]*:[ \t]*)'
    r'"(?:\\.|[^"\\\r\n])*"',
    flags=re.IGNORECASE,
)
_FORM_CREDENTIAL = re.compile(
    rf"\b({_SENSITIVE_KEYS})([ \t]*=[ \t]*)"
    r"(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s;&\r\n]*)",
    flags=re.IGNORECASE,
)


def redact_text(text: str, secrets: Mapping[str, str]) -> str:
    """Return text with configured and conventional credential values masked."""
    redacted = text
    secret_values = sorted(
        {value for value in secrets.values() if value},
        key=len,
        reverse=True,
    )
    for secret in secret_values:
        redacted = redacted.replace(secret, "***")

    redacted = _AUTHORIZATION.sub(r"\1***", redacted)
    redacted = _COOKIE.sub(r"\1***", redacted)
    redacted = _JSON_CREDENTIAL.sub(r'\1"***"', redacted)
    return _FORM_CREDENTIAL.sub(r"\1\2***", redacted)

from collections.abc import Mapping
import re


_AUTHORIZATION_BEARER = re.compile(
    r"^([ \t]*authorization[ \t]*:[ \t]*bearer[ \t]+)[^\r\n]*",
    flags=re.IGNORECASE | re.MULTILINE,
)
_COOKIE = re.compile(
    r"^([ \t]*cookie[ \t]*:[ \t]*)[^\r\n]*",
    flags=re.IGNORECASE | re.MULTILINE,
)
_NAMED_CREDENTIAL = re.compile(
    r"\b(password|token)([ \t]*=[ \t]*)"
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

    redacted = _AUTHORIZATION_BEARER.sub(r"\1***", redacted)
    redacted = _COOKIE.sub(r"\1***", redacted)
    return _NAMED_CREDENTIAL.sub(r"\1\2***", redacted)

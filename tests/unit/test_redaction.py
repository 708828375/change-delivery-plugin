import pytest

from change_delivery.redaction import redact_text


def test_redacts_overlapping_secrets_longest_first() -> None:
    assert redact_text("token=abcdef and abc", {"SHORT": "abc", "LONG": "abcdef"}) == (
        "token=*** and ***"
    )


def test_redacts_literal_regex_characters_and_unicode() -> None:
    text = "密码：密钥.*[值]；再次出现 密钥.*[值]"

    assert redact_text(text, {"UNICODE_SECRET": "密钥.*[值]"}) == (
        "密码：***；再次出现 ***"
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Authorization: Bearer abc.def", "Authorization: Bearer ***"),
        ("authorization: bearer abc", "authorization: bearer ***"),
        ("password=hunter2", "password=***"),
        ("PASSWORD = hunter2", "PASSWORD = ***"),
        ("token=abc", "token=***"),
        ("prefix token=abc suffix", "prefix token=*** suffix"),
    ],
)
def test_redacts_well_known_credentials_at_line_end_or_inline(
    text: str,
    expected: str,
) -> None:
    assert redact_text(text, {}) == expected


def test_redacts_multiline_headers_and_entire_cookie_value() -> None:
    text = (
        "safe line\n"
        "Cookie: session=secret; preference=dark; next=still-secret\n"
        "Authorization: Bearer final-secret\n"
        "next line"
    )

    assert redact_text(text, {}) == (
        "safe line\n"
        "Cookie: ***\n"
        "Authorization: Bearer ***\n"
        "next line"
    )


def test_redaction_preserves_crlf_line_boundaries() -> None:
    text = "Cookie: first=one; second=two\r\nsafe"

    assert redact_text(text, {}) == "Cookie: ***\r\nsafe"


def test_ignores_empty_secret_values() -> None:
    assert redact_text("ordinary text", {"EMPTY": ""}) == "ordinary text"

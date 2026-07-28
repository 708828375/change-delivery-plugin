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
        ("Authorization: Basic dXNlcjpwYXNz", "Authorization: Basic ***"),
        (
            "INFO Authorization: Bearer abc status=200 trace_id=trace-1",
            "INFO Authorization: Bearer *** status=200 trace_id=trace-1",
        ),
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


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "INFO request Authorization: Bearer line-secret",
            "INFO request Authorization: Bearer ***",
        ),
        (
            "[http] AUTHORIZATION: BEARER structured-secret",
            "[http] AUTHORIZATION: BEARER ***",
        ),
        (
            "INFO response Cookie: session=one; preference=two",
            "INFO response Cookie: ***",
        ),
        (
            '{"level":"debug"} cOoKiE: session=case-secret; second=hidden',
            '{"level":"debug"} cOoKiE: ***',
        ),
    ],
)
def test_redacts_headers_after_log_or_structured_prefix(
    text: str,
    expected: str,
) -> None:
    assert redact_text(text, {}) == expected


def test_redacts_non_line_start_headers_across_multiple_lines() -> None:
    text = (
        "INFO first Authorization: Bearer first-secret\n"
        "[access] Cookie: session=second-secret; preference=dark\r\n"
        "safe"
    )

    assert redact_text(text, {}) == (
        "INFO first Authorization: Bearer ***\n"
        "[access] Cookie: ***\r\n"
        "safe"
    )


def test_cookie_redaction_preserves_following_structured_log_fields() -> None:
    text = (
        "INFO Cookie: session=one; preference=two "
        "status=200 trace_id=trace-1\r\nsafe"
    )

    assert redact_text(text, {}) == (
        "INFO Cookie: *** status=200 trace_id=trace-1\r\nsafe"
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "?access_token=access-secret&status=ok",
            "?access_token=***&status=ok",
        ),
        (
            "refresh_token=refresh-secret&next=one",
            "refresh_token=***&next=one",
        ),
        ("API_KEY=key-secret", "API_KEY=***"),
        ("client_secret=client-secret", "client_secret=***"),
        (
            '{"access_token": "json-secret", "status": "ok"}',
            '{"access_token": "***", "status": "ok"}',
        ),
        (
            '{"REFRESH_TOKEN":"refresh","api_key":"key","client_secret":"client"}',
            '{"REFRESH_TOKEN":"***","api_key":"***","client_secret":"***"}',
        ),
        (
            r'{"api_key":"escaped-\"-secret","status":"ok"}',
            r'{"api_key":"***","status":"ok"}',
        ),
    ],
)
def test_redacts_sensitive_query_form_and_json_fields(
    text: str,
    expected: str,
) -> None:
    assert redact_text(text, {}) == expected


def test_cookie_field_named_status_remains_inside_redacted_header() -> None:
    text = "Cookie: session=one; status=cookie-value status=200"

    assert redact_text(text, {}) == "Cookie: *** status=200"


def test_redaction_preserves_crlf_line_boundaries() -> None:
    text = "Cookie: first=one; second=two\r\nsafe"

    assert redact_text(text, {}) == "Cookie: ***\r\nsafe"


def test_ignores_empty_secret_values() -> None:
    assert redact_text("ordinary text", {"EMPTY": ""}) == "ordinary text"

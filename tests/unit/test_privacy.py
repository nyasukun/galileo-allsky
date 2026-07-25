from __future__ import annotations

import pytest
from conftest import add_attribute, attributes_dict
from opentelemetry.proto.common.v1.common_pb2 import AnyValue

from allsky_collector.privacy import (
    CONTENT_DISABLED,
    REDACTED,
    REDACTED_REASONING,
    any_value_to_python,
    is_content_key,
    pseudonymize,
    python_to_any_value,
    redact_text,
    sanitize_attributes,
)


def test_safe_operational_metadata_is_not_misclassified_as_content() -> None:
    safe = (
        "input_tokens",
        "input_token_count",
        "gen_ai.usage.input_tokens",
        "gen_ai.response.id",
        "http.response.status_code",
        "tool_input_size_bytes",
        "gen_ai.usage.reasoning.output_tokens",
    )

    assert all(not is_content_key(key) for key in safe)
    assert is_content_key("gen_ai.input.messages")
    assert is_content_key("http.response.body")
    assert is_content_key("http.response.status_code.body")
    assert is_content_key("tool_input")


def test_sanitize_attributes_redacts_content_reasoning_secrets_and_identity() -> None:
    attributes = []
    add_attribute(attributes, "prompt", "show sk-supersecretvalue")
    add_attribute(attributes, "reasoning.content", "private reasoning")
    add_attribute(attributes, "authorization", "Bearer secret-token-value")
    add_attribute(attributes, "user.email", "person@example.com")
    add_attribute(attributes, "input_token_count", "12")
    add_attribute(attributes, "gen_ai.usage.reasoning.output_tokens", 3)

    sanitized = attributes_dict(
        sanitize_attributes(
            attributes,
            capture_content=False,
            maximum=1024,
            pseudonym_secret="hmac-secret",
        )
    )

    assert sanitized["prompt"] == CONTENT_DISABLED
    assert sanitized["reasoning.content"] == REDACTED_REASONING
    assert sanitized["authorization"] == REDACTED
    assert sanitized["user.email"].startswith("allsky:user.email:")
    assert "person@example.com" not in sanitized["user.email"]
    assert sanitized["input_token_count"] == "12"
    assert sanitized["gen_ai.usage.reasoning.output_tokens"] == 3


def test_capture_content_still_redacts_secret_values() -> None:
    attributes = []
    add_attribute(
        attributes,
        "tool_input",
        {"command": "echo ok", "api_key": "secret-value-123"},
    )

    sanitized = attributes_dict(
        sanitize_attributes(
            attributes,
            capture_content=True,
            maximum=1024,
            pseudonym_secret="hmac-secret",
        )
    )

    assert sanitized["tool_input"] == {
        "command": "echo ok",
        "api_key": REDACTED,
    }


def test_capture_content_redacts_plain_token_keys_and_structured_strings() -> None:
    attributes = []
    add_attribute(attributes, "token", "TOPLEVEL-CANARY")
    add_attribute(
        attributes,
        "tool_parameters",
        (
            '{"token":"JSON-CANARY","command":"echo FOO_TOKEN=ASSIGN-CANARY",'
            '"bash_command":"echo hello","full_command":"cd /tmp && echo hello"}'
        ),
    )

    sanitized = attributes_dict(
        sanitize_attributes(
            attributes,
            capture_content=True,
            maximum=1024,
            pseudonym_secret="hmac-secret",
        )
    )
    wire = repr(sanitized)

    assert sanitized["token"] == REDACTED
    assert "TOPLEVEL-CANARY" not in wire
    assert "JSON-CANARY" not in wire
    assert "ASSIGN-CANARY" not in wire
    assert REDACTED in sanitized["tool_parameters"]
    assert "bash_command" in sanitized["tool_parameters"]
    assert "full_command" in sanitized["tool_parameters"]


def test_capture_disabled_drops_unknown_attributes_and_filters_nested_maps() -> None:
    attributes = []
    add_attribute(
        attributes,
        "metadata",
        {
            "prompt": "CANARY nested prompt",
            "parameters": {"command": "CANARY nested command"},
        },
    )
    add_attribute(
        attributes,
        "event.kind",
        {
            "model": "gpt-test",
            "prompt": "CANARY nested content",
            "unknown": "CANARY unknown value",
        },
    )

    sanitized = attributes_dict(
        sanitize_attributes(
            attributes,
            capture_content=False,
            maximum=1024,
            pseudonym_secret="hmac-secret",
        )
    )

    assert "metadata" not in sanitized
    assert sanitized["event.kind"] == {
        "model": "gpt-test",
        "prompt": CONTENT_DISABLED,
    }
    assert "CANARY" not in repr(sanitized)


def test_attribute_keys_cannot_carry_content_or_secret_canaries() -> None:
    attributes = []
    add_attribute(attributes, "prompt.PRIVATE-CANARY", "value")
    add_attribute(attributes, "secret.PRIVATE-CANARY", "value")
    add_attribute(attributes, "reasoning.PRIVATE-CANARY", "value")
    add_attribute(attributes, "bad key", "value")

    for capture_content in (False, True):
        sanitized = sanitize_attributes(
            attributes,
            capture_content=capture_content,
            maximum=1024,
            pseudonym_secret="hmac-secret",
        )
        assert "PRIVATE-CANARY" not in repr(sanitized)
        assert "bad key" not in repr(sanitized)


def test_operational_metadata_values_require_a_bounded_label_shape() -> None:
    attributes = []
    add_attribute(attributes, "event.kind", "PRIVATE CANARY sentence")
    add_attribute(attributes, "service.name", "codex_cli_rs")

    sanitized = attributes_dict(
        sanitize_attributes(
            attributes,
            capture_content=False,
            maximum=1024,
            pseudonym_secret="hmac-secret",
        )
    )

    assert "PRIVATE CANARY" not in repr(sanitized)
    assert sanitized["service.name"] == "codex_cli_rs"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0.125, 0.125),
        (-1.0, "[operational metadata omitted]"),
        (float("inf"), "[operational metadata omitted]"),
    ],
)
def test_cost_usd_requires_a_finite_nonnegative_number(value: float, expected: object) -> None:
    attributes = []
    add_attribute(attributes, "cost_usd", value)

    sanitized = attributes_dict(
        sanitize_attributes(
            attributes,
            capture_content=False,
            maximum=1024,
            pseudonym_secret="hmac-secret",
        )
    )

    assert sanitized["cost_usd"] == expected


def test_pseudonyms_are_stable_and_namespaced() -> None:
    first = pseudonymize("same", "secret", "session.id")
    second = pseudonymize("same", "secret", "session.id")
    different = pseudonymize("same", "secret", "user.id")

    assert first == second
    assert first != different
    assert first.startswith("allsky:session.id:")


def test_any_value_round_trip_nested_values() -> None:
    value = {"items": [1, True, "x"], "nested": {"n": 2.5}}

    assert any_value_to_python(python_to_any_value(value)) == value


def test_redact_text_handles_common_secret_forms_and_clips() -> None:
    value = (
        "Authorization: Bearer abcdefghijklmnop "
        "token eyJabcdef.abcdefgh.abcdefgh "
        "access_key=secret-value-123 "
        "sk-secretvalue123456789"
    )

    redacted = redact_text(value, 80)

    assert "abcdefghijklmnop" not in redacted
    assert "eyJabcdef" not in redacted
    assert "secret-value-123" not in redacted
    assert "sk-secretvalue" not in redacted
    assert len(redacted) <= 80


def test_redact_text_handles_auth_schemes_and_prefixed_secret_assignments() -> None:
    value = "\n".join(
        (
            "Authorization: Basic QWxhZGRpbjpvcGVuIHNlc2FtZQ==",
            "Proxy-Authorization: Basic cHJveHktc2VjcmV0",
            "GALILEO_API_KEY=APIKEY-CANARY",
            "DB_PASSWORD=PASSWORD-CANARY",
            "OAUTH_CLIENT_SECRET=CLIENT-CANARY",
            "COOKIE=COOKIE-CANARY",
            "PRIVATE_KEY=PRIVATE-CANARY",
            "DB_CREDENTIAL=CREDENTIAL-CANARY",
        )
    )

    redacted = redact_text(value, 4096)

    assert "CANARY" not in redacted
    assert "QWxhZGRp" not in redacted
    assert "cHJveHkt" not in redacted
    assert redacted.count("[REDACTED]") >= 8


def test_redact_text_removes_short_explicit_credentials() -> None:
    redacted = redact_text("PASSWORD=x Authorization: Bearer abc", 4096)

    assert "PASSWORD=x" not in redacted
    assert "Bearer abc" not in redacted
    assert redacted.count("[REDACTED]") == 2


def test_redact_text_removes_plain_reasoning_assignments() -> None:
    value = 'reasoning: PRIVATE-CANARY\n"thinking": "ANOTHER-CANARY"'

    redacted = redact_text(value, 4096)

    assert "PRIVATE-CANARY" not in redacted
    assert "ANOTHER-CANARY" not in redacted
    assert redacted.count(REDACTED_REASONING) == 2


def test_any_value_bytes_are_not_exported() -> None:
    value = AnyValue(bytes_value=b"raw-secret-bytes")

    assert any_value_to_python(value) == "[binary omitted: 16 bytes]"

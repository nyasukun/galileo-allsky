from __future__ import annotations

import json

import pytest
from conftest import add_attribute, attributes_dict
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
    ExportLogsServiceRequest,
)
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)

from allsky_collector.config import Settings
from allsky_collector.privacy import CONTENT_DISABLED, python_to_any_value
from allsky_collector.transform import (
    UINT64_MAX,
    logs_to_traces,
    normalize_traces,
    resolve_agent,
)


def _codex_logs() -> ExportLogsServiceRequest:
    request = ExportLogsServiceRequest()
    resource_logs = request.resource_logs.add()
    add_attribute(resource_logs.resource.attributes, "service.name", "codex_cli_rs")
    add_attribute(
        resource_logs.resource.attributes,
        "galileo.experiment.id",
        "must-not-survive",
    )
    add_attribute(resource_logs.resource.attributes, "host.name", "personal-mac")
    scope_logs = resource_logs.scope_logs.add()
    scope_logs.scope.name = "codex"

    prompt = scope_logs.log_records.add()
    prompt.time_unix_nano = 10_000_000
    prompt.event_name = "codex.user_prompt"
    add_attribute(prompt.attributes, "conversation.id", "raw-conversation")
    add_attribute(prompt.attributes, "user.email", "person@example.com")
    add_attribute(prompt.attributes, "prompt", "Bearer token-that-must-not-leak")
    add_attribute(prompt.attributes, "prompt_length", 31)

    completed = scope_logs.log_records.add()
    completed.time_unix_nano = 20_000_000
    completed.trace_id = b"\xaa" * 16
    completed.event_name = "codex.sse_event"
    add_attribute(completed.attributes, "event.kind", "response.completed")
    add_attribute(completed.attributes, "model", "gpt-test")
    add_attribute(completed.attributes, "input_token_count", "12")
    add_attribute(completed.attributes, "output_token_count", 5)
    add_attribute(completed.attributes, "cached_token_count", 2)

    tool = scope_logs.log_records.add()
    tool.time_unix_nano = 30_000_000
    tool.trace_id = b"\xbb" * 16
    tool.event_name = "codex.tool_result"
    tool.body.CopyFrom(
        python_to_any_value(
            {
                "api_key": "nested-secret-value",
                "result": "ok",
            }
        )
    )
    add_attribute(tool.attributes, "tool", "shell")
    add_attribute(tool.attributes, "call_id", "call-1")
    add_attribute(tool.attributes, "arguments", {"command": "pwd"})
    add_attribute(tool.attributes, "tool_parameters", {"password": "canary-password"})
    add_attribute(tool.attributes, "output", "API_KEY=secret-value-123")
    add_attribute(tool.attributes, "error", "CANARY raw error detail")
    add_attribute(tool.attributes, "galileo.experiment.id", "CANARY-experiment")
    add_attribute(tool.attributes, "duration_ms", "4.5")
    add_attribute(tool.attributes, "success", True)
    return request


def _spans(request: ExportTraceServiceRequest) -> list:
    return [
        span
        for resource_spans in request.resource_spans
        for scope_spans in resource_spans.scope_spans
        for span in scope_spans.spans
    ]


def test_logs_are_one_to_one_valid_genai_spans(settings: Settings) -> None:
    transformed = logs_to_traces(
        _codex_logs(),
        agent="codex-cli",
        settings=settings,
        now_ns=99,
    )
    spans = _spans(transformed.request)

    assert transformed.input_items == transformed.output_spans == 3
    assert [span.name for span in spans] == [
        "invoke_agent Codex CLI",
        "chat gpt-test",
        "execute_tool shell",
    ]
    assert len({span.trace_id for span in spans}) == 1
    assert spans[0].parent_span_id == b""
    assert spans[1].parent_span_id == spans[0].span_id
    assert spans[2].parent_span_id == spans[0].span_id
    assert transformed.diagnostics == {
        "logs.grouping.conversation": 3,
        "logs.grouping.scope_inferred": 2,
    }
    assert [attributes_dict(span.attributes)["openinference.span.kind"] for span in spans] == [
        "AGENT",
        "LLM",
        "TOOL",
    ]
    for span in spans:
        attributes = attributes_dict(span.attributes)
        assert attributes["gen_ai.provider.name"] == "openai"
        assert attributes["gen_ai.system"] == "openai"
        assert json.loads(attributes["gen_ai.input.messages"])
        assert json.loads(attributes["gen_ai.output.messages"])
        assert len(span.trace_id) == 16
        assert len(span.span_id) == 8
        assert span.end_time_unix_nano > span.start_time_unix_nano > 0

    completed = attributes_dict(spans[1].attributes)
    assert completed["gen_ai.usage.input_tokens"] == 12
    assert completed["gen_ai.usage.output_tokens"] == 5
    assert completed["gen_ai.usage.cache_read.input_tokens"] == 2
    assert completed["allsky.content.output.present"] is False

    tool = attributes_dict(spans[2].attributes)
    assert tool["gen_ai.tool.name"] == "shell"
    assert tool["gen_ai.tool.call.id"].startswith("allsky:tool_call:")
    assert tool["call_id"] == tool["gen_ai.tool.call.id"]
    assert tool["arguments"] == CONTENT_DISABLED
    assert tool["output"] == CONTENT_DISABLED


def test_default_privacy_and_routing_cannot_be_overridden(settings: Settings) -> None:
    transformed = logs_to_traces(
        _codex_logs(),
        agent="codex-cli",
        settings=settings,
        now_ns=99,
    )
    wire = transformed.request.SerializeToString()
    resource = transformed.request.resource_spans[0].resource
    resource_attributes = attributes_dict(resource.attributes)
    prompt_attributes = attributes_dict(_spans(transformed.request)[0].attributes)

    assert b"must-not-survive" not in wire
    assert b"personal-mac" not in wire
    assert b"person@example.com" not in wire
    assert b"raw-conversation" not in wire
    assert b"token-that-must-not-leak" not in wire
    assert b"CANARY" not in wire
    assert "galileo.experiment.id" not in resource_attributes
    assert resource_attributes["galileo.project.name"] == settings.project
    assert resource_attributes["galileo.logstream.name"] == "codex-cli"
    assert prompt_attributes["gen_ai.conversation.id"].startswith("allsky:conversation:")


def test_synthetic_ids_are_deterministic(settings: Settings) -> None:
    first = logs_to_traces(
        _codex_logs(),
        agent="codex-cli",
        settings=settings,
        now_ns=1,
    )
    second = logs_to_traces(
        _codex_logs(),
        agent="codex-cli",
        settings=settings,
        now_ns=999,
    )

    assert [(span.trace_id, span.span_id) for span in _spans(first.request)] == [
        (span.trace_id, span.span_id) for span in _spans(second.request)
    ]
    assert all(span.trace_id[:8] != span.span_id for span in _spans(first.request))

    other_secret = Settings(
        api_key="key",
        project="project",
        pseudonym_secret="different-secret",
    )
    third = logs_to_traces(
        _codex_logs(),
        agent="codex-cli",
        settings=other_secret,
        now_ns=1,
    )
    assert [(span.trace_id, span.span_id) for span in _spans(first.request)] != [
        (span.trace_id, span.span_id) for span in _spans(third.request)
    ]


def test_prompt_id_is_hmac_correlated_into_one_synthetic_trace(settings: Settings) -> None:
    request = ExportLogsServiceRequest()
    scope = request.resource_logs.add().scope_logs.add()
    for index, event_name in enumerate(
        ("claude_code.user_prompt", "claude_code.api_request"),
        start=1,
    ):
        record = scope.log_records.add()
        record.event_name = event_name
        record.time_unix_nano = index
        add_attribute(record.attributes, "prompt.id", "raw-prompt-uuid")

    spans = _spans(
        logs_to_traces(
            request,
            agent="claude-cowork",
            settings=settings,
            now_ns=100,
        ).request
    )
    first_attributes = attributes_dict(spans[0].attributes)
    second_attributes = attributes_dict(spans[1].attributes)

    assert spans[0].trace_id == spans[1].trace_id
    assert spans[0].span_id != spans[1].span_id
    assert spans[0].parent_span_id == b""
    assert spans[1].parent_span_id == spans[0].span_id
    assert first_attributes["prompt.id"] == second_attributes["prompt.id"]
    assert first_attributes["prompt.id"].startswith("allsky:prompt:")
    assert (
        first_attributes["gen_ai.conversation.id"]
        == second_attributes["gen_ai.conversation.id"]
        == first_attributes["prompt.id"]
    )
    assert b"raw-prompt-uuid" not in spans[0].SerializeToString()


@pytest.mark.parametrize(
    "correlation_key",
    ("conversation.id", "gen_ai.conversation.id", "session.id"),
)
def test_conversation_identity_is_hmac_correlated_into_one_synthetic_trace(
    settings: Settings,
    correlation_key: str,
) -> None:
    request = ExportLogsServiceRequest()
    scope = request.resource_logs.add().scope_logs.add()
    for index, event_name in enumerate(
        ("codex.user_prompt", "codex.sse_event", "codex.tool_result"),
        start=1,
    ):
        record = scope.log_records.add()
        record.event_name = event_name
        record.time_unix_nano = index
        add_attribute(record.attributes, correlation_key, "raw-conversation")

    spans = _spans(
        logs_to_traces(
            request,
            agent="codex-cli",
            settings=settings,
            now_ns=100,
        ).request
    )

    assert len({span.trace_id for span in spans}) == 1
    assert len({span.span_id for span in spans}) == 3
    assert spans[0].parent_span_id == b""
    assert {span.parent_span_id for span in spans[1:]} == {spans[0].span_id}
    assert {attributes_dict(span.attributes)["gen_ai.conversation.id"] for span in spans} == {
        attributes_dict(spans[0].attributes)["gen_ai.conversation.id"],
    }
    assert attributes_dict(spans[0].attributes)["gen_ai.conversation.id"].startswith(
        "allsky:conversation:"
    )
    assert b"raw-conversation" not in spans[0].SerializeToString()


def test_conversation_identity_takes_precedence_over_prompt_identity(
    settings: Settings,
) -> None:
    request = ExportLogsServiceRequest()
    scope = request.resource_logs.add().scope_logs.add()
    for index, prompt_id in enumerate(("prompt-1", "prompt-2"), start=1):
        record = scope.log_records.add()
        record.event_name = "codex.user_prompt"
        record.time_unix_nano = index
        add_attribute(record.attributes, "conversation.id", "conversation-1")
        add_attribute(record.attributes, "prompt.id", prompt_id)

    spans = _spans(
        logs_to_traces(
            request,
            agent="codex-cli",
            settings=settings,
            now_ns=100,
        ).request
    )

    assert spans[0].trace_id == spans[1].trace_id
    assert spans[0].parent_span_id == b""
    assert spans[1].parent_span_id == spans[0].span_id


def test_correlated_batches_use_distinct_traces_with_one_conversation_id() -> None:
    settings = Settings(
        api_key="key",
        project="project",
        capture_content=True,
        pseudonym_secret="pseudonym",
    )

    def one_record(
        event_name: str,
        timestamp: int,
        *,
        prompt: str | None = None,
    ) -> ExportLogsServiceRequest:
        request = ExportLogsServiceRequest()
        record = request.resource_logs.add().scope_logs.add().log_records.add()
        record.event_name = event_name
        record.time_unix_nano = timestamp
        add_attribute(record.attributes, "conversation.id", "same-conversation")
        if prompt is not None:
            add_attribute(record.attributes, "prompt", prompt)
        return request

    conversation_start = _spans(
        logs_to_traces(
            one_record("codex.conversation_starts", 1),
            agent="codex",
            settings=settings,
        ).request
    )[0]
    user_prompt = _spans(
        logs_to_traces(
            one_record("codex.user_prompt", 2, prompt="visible prompt"),
            agent="codex",
            settings=settings,
        ).request
    )[0]

    assert conversation_start.trace_id != user_prompt.trace_id
    assert conversation_start.span_id != user_prompt.span_id
    assert conversation_start.parent_span_id == b""
    assert user_prompt.parent_span_id == b""
    assert (
        attributes_dict(conversation_start.attributes)["gen_ai.conversation.id"]
        == attributes_dict(user_prompt.attributes)["gen_ai.conversation.id"]
    )
    assert (
        json.loads(attributes_dict(user_prompt.attributes)["gen_ai.input.messages"])[0]["content"]
        == "visible prompt"
    )


def test_uncorrelated_codex_log_with_inbound_trace_id_is_suppressed(
    settings: Settings,
) -> None:
    request = ExportLogsServiceRequest()
    record = request.resource_logs.add().scope_logs.add().log_records.add()
    record.trace_id = b"\xaa" * 16
    record.event_name = "codex.sse_event"

    transformed = logs_to_traces(
        request,
        agent="codex",
        settings=settings,
        now_ns=100,
    )

    assert transformed.input_items == 1
    assert transformed.output_spans == 0
    assert transformed.diagnostics == {
        "logs.suppressed.uncorrelated_trace_id": 1,
    }


def test_capture_content_exports_redacted_payload() -> None:
    settings = Settings(
        api_key="key",
        project="project",
        capture_content=True,
        pseudonym_secret="pseudonym",
    )

    transformed = logs_to_traces(
        _codex_logs(),
        agent="codex-cli",
        settings=settings,
        now_ns=99,
    )
    wire = transformed.request.SerializeToString()
    tool = attributes_dict(_spans(transformed.request)[2].attributes)

    assert b"secret-value-123" not in wire
    assert b"nested-secret-value" not in wire
    assert tool["arguments"] == {"command": "pwd"}
    assert "[REDACTED]" in tool["output"]


def test_claude_tool_details_and_output_event_are_exported_only_after_opt_in() -> None:
    settings = Settings(
        api_key="key",
        project="project",
        capture_content=True,
        pseudonym_secret="pseudonym",
    )
    request = ExportTraceServiceRequest()
    span = request.resource_spans.add().scope_spans.add().spans.add()
    span.name = "claude_code.tool"
    add_attribute(span.attributes, "tool_name", "Bash")
    add_attribute(span.attributes, "full_command", "cd /tmp && echo hello")
    add_attribute(span.attributes, "file_path", "/tmp/example.txt")
    event = span.events.add()
    event.name = "tool.output"
    add_attribute(event.attributes, "payload", "hello")
    add_attribute(event.attributes, "token", "EVENT-SECRET-CANARY")

    output = _spans(
        normalize_traces(
            request,
            agent="claude-code-cli",
            settings=settings,
        ).request
    )[0]
    attributes = attributes_dict(output.attributes)
    input_message = json.loads(attributes["gen_ai.input.messages"])[0]["content"]
    output_message = json.loads(attributes["gen_ai.output.messages"])[0]["content"]
    wire = output.SerializeToString()

    assert input_message == "cd /tmp && echo hello"
    assert json.loads(output_message)["payload"] == "hello"
    assert b"EVENT-SECRET-CANARY" not in wire
    assert attributes["full_command"] == "cd /tmp && echo hello"
    assert attributes["file_path"] == "/tmp/example.txt"


def test_trace_hierarchy_is_preserved_and_invalid_fields_are_repaired(
    settings: Settings,
) -> None:
    request = ExportTraceServiceRequest()
    resource_spans = request.resource_spans.add()
    add_attribute(resource_spans.resource.attributes, "agent.surface", "claude-code-cli")
    scope = resource_spans.scope_spans.add()

    root = scope.spans.add()
    root.trace_id = b"\x10" * 16
    root.span_id = b"\x20" * 8
    root.name = "claude_code.interaction"
    root.start_time_unix_nano = 100
    root.end_time_unix_nano = 200
    add_attribute(root.attributes, "session.id", "raw-session")
    add_attribute(root.attributes, "user_prompt", "sensitive prompt")

    child = scope.spans.add()
    child.trace_id = root.trace_id
    child.span_id = b"\x30" * 8
    child.parent_span_id = root.span_id
    child.name = "claude_code.llm_request"
    add_attribute(child.attributes, "gen_ai.request.model", "claude-test")
    add_attribute(child.attributes, "input_tokens", 4)

    invalid = scope.spans.add()
    invalid.name = "claude_code.tool.execution"
    invalid.parent_span_id = b"bad"
    add_attribute(invalid.attributes, "tool_name", "Read")

    transformed = normalize_traces(
        request,
        agent="claude-code-cli",
        settings=settings,
    )
    root_out, child_out, invalid_out = _spans(transformed.request)

    assert root_out.trace_id == root.trace_id
    assert root_out.span_id == root.span_id
    assert child_out.parent_span_id == root.span_id
    assert child_out.trace_id == root.trace_id
    assert child_out.name == "chat claude-test"
    assert invalid_out.parent_span_id == b""
    assert len(invalid_out.trace_id) == 16
    assert len(invalid_out.span_id) == 8
    assert invalid_out.end_time_unix_nano > invalid_out.start_time_unix_nano > 0


def test_trace_non_attribute_fields_and_reserved_values_are_sanitized(
    settings: Settings,
) -> None:
    request = ExportTraceServiceRequest()
    resource_spans = request.resource_spans.add()
    resource_spans.schema_url = "https://CANARY-resource-schema"
    add_attribute(
        resource_spans.resource.attributes,
        "galileo.experiment.id",
        "CANARY-experiment",
    )
    scope = resource_spans.scope_spans.add()
    scope.schema_url = "https://CANARY-scope-schema"
    scope.scope.name = "CANARY scope name"
    scope.scope.version = "CANARY-version"
    add_attribute(scope.scope.attributes, "tool_parameters", "CANARY-scope-parameters")

    span = scope.spans.add()
    span.name = "claude_code.llm_request"
    span.trace_state = "vendor=CANARY-trace-state"
    span.status.code = 2
    span.status.message = "CANARY status detail"
    add_attribute(span.attributes, "tool_parameters", "CANARY-tool-parameters")
    add_attribute(span.attributes, "error", "CANARY-error-detail")
    add_attribute(span.attributes, "error_type", "timeout")
    add_attribute(span.attributes, "galileo.dataset.input", "CANARY-dataset")
    event = span.events.add()
    event.name = "CANARY event name"
    add_attribute(event.attributes, "tool_parameters", "CANARY-event-parameters")
    link = span.links.add()
    link.trace_state = "vendor=CANARY-link-state"
    add_attribute(link.attributes, "error", "CANARY-link-error")

    transformed = normalize_traces(
        request,
        agent="claude-code-cli",
        settings=settings,
    )
    wire = transformed.request.SerializeToString()
    output_group = transformed.request.resource_spans[0]
    output_scope = output_group.scope_spans[0]
    output = output_scope.spans[0]

    assert b"CANARY" not in wire
    assert output_group.schema_url == ""
    assert output_scope.schema_url == ""
    assert output_scope.scope.name == "allsky_collector/claude-code-cli"
    assert output.trace_state == ""
    assert output.status.message == "timeout"
    assert output.links[0].trace_state == ""
    assert attributes_dict(output.attributes)["error.type"] == "timeout"


def _varint(value: int) -> bytes:
    encoded = bytearray()
    while value > 0x7F:
        encoded.append((value & 0x7F) | 0x80)
        value >>= 7
    encoded.append(value)
    return bytes(encoded)


def test_trace_unknown_protobuf_fields_are_discarded(settings: Settings) -> None:
    request = ExportTraceServiceRequest()
    span = request.resource_spans.add().scope_spans.add().spans.add()
    span.name = "claude_code.interaction"
    canary = b"CANARY unknown protobuf payload"
    unknown_field = _varint((999 << 3) | 2) + _varint(len(canary)) + canary
    parsed = ExportTraceServiceRequest()
    parsed.ParseFromString(request.SerializeToString() + unknown_field)

    assert canary in parsed.SerializeToString()
    transformed = normalize_traces(
        parsed,
        agent="claude-code-cli",
        settings=settings,
    )

    assert canary not in transformed.request.SerializeToString()


def test_duplicate_attributes_cannot_override_collector_values(settings: Settings) -> None:
    request = ExportTraceServiceRequest()
    span = request.resource_spans.add().scope_spans.add().spans.add()
    span.name = "claude_code.llm_request"
    add_attribute(span.attributes, "gen_ai.provider.name", "evil-one")
    add_attribute(span.attributes, "gen_ai.provider.name", "evil-two")

    output = _spans(
        normalize_traces(
            request,
            agent="claude-code-cli",
            settings=settings,
        ).request
    )[0]
    providers = [
        attribute.value.string_value
        for attribute in output.attributes
        if attribute.key == "gen_ai.provider.name"
    ]

    assert providers == ["anthropic"]


def test_semantic_fields_cannot_reinject_secrets_when_capture_is_disabled(
    settings: Settings,
) -> None:
    request = ExportLogsServiceRequest()
    scope = request.resource_logs.add().scope_logs.add()

    model = scope.log_records.add()
    model.event_name = "codex.sse_event"
    add_attribute(model.attributes, "model", "sk-SUPERSECRETVALUE123")

    tool = scope.log_records.add()
    tool.event_name = "codex.tool_result"
    add_attribute(tool.attributes, "tool_name", "sk-TOOLSECRETVALUE123")
    add_attribute(tool.attributes, "call_id", "Bearer call-secret-value")

    transformed = logs_to_traces(
        request,
        agent="codex-cli",
        settings=settings,
        now_ns=100,
    )
    wire = transformed.request.SerializeToString()
    model_span, tool_span = _spans(transformed.request)
    tool_attributes = attributes_dict(tool_span.attributes)

    assert b"SUPERSECRETVALUE" not in wire
    assert b"TOOLSECRETVALUE" not in wire
    assert b"call-secret-value" not in wire
    assert model_span.name == "chat Codex CLI"
    assert tool_span.name == "execute_tool unknown"
    assert tool_attributes["gen_ai.tool.call.id"].startswith("allsky:tool_call:")


def test_unknown_event_names_cannot_carry_content(settings: Settings) -> None:
    logs = ExportLogsServiceRequest()
    record = logs.resource_logs.add().scope_logs.add().log_records.add()
    record.event_name = "codex.PRIVATE-CANARY-message"

    traces = ExportTraceServiceRequest()
    span = traces.resource_spans.add().scope_spans.add().spans.add()
    span.name = "claude_code.PRIVATE-CANARY-message"
    event = span.events.add()
    event.name = "claude_code.PRIVATE-CANARY-event"

    logs_output = logs_to_traces(
        logs,
        agent="codex-cli",
        settings=settings,
        now_ns=100,
    ).request
    traces_output = normalize_traces(
        traces,
        agent="claude-code-cli",
        settings=settings,
    ).request

    assert b"PRIVATE-CANARY" not in logs_output.SerializeToString()
    assert b"PRIVATE-CANARY" not in traces_output.SerializeToString()
    assert attributes_dict(_spans(logs_output)[0].attributes)["event.name"] == "codex-cli.log"
    assert _spans(traces_output)[0].events[0].name == "allsky.span_event"


@pytest.mark.parametrize("duration", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_duration_does_not_crash(
    settings: Settings,
    duration: float,
) -> None:
    request = ExportLogsServiceRequest()
    record = request.resource_logs.add().scope_logs.add().log_records.add()
    record.event_name = "codex.websocket_request"
    add_attribute(record.attributes, "duration_ms", duration)

    span = _spans(
        logs_to_traces(
            request,
            agent="codex-cli",
            settings=settings,
            now_ns=100,
        ).request
    )[0]

    assert span.name.startswith("chat ")
    assert span.end_time_unix_nano > span.start_time_unix_nano


def test_maximum_uint64_start_time_is_repaired(settings: Settings) -> None:
    request = ExportTraceServiceRequest()
    span = request.resource_spans.add().scope_spans.add().spans.add()
    span.name = "claude_code.api_retries_exhausted"
    span.start_time_unix_nano = UINT64_MAX

    output = _spans(
        normalize_traces(
            request,
            agent="claude-code-cli",
            settings=settings,
        ).request
    )[0]

    assert output.name.startswith("chat ")
    assert output.start_time_unix_nano == UINT64_MAX - 1
    assert output.end_time_unix_nano == UINT64_MAX


def test_claude_error_log_sets_error_status(settings: Settings) -> None:
    request = ExportLogsServiceRequest()
    scope = request.resource_logs.add().scope_logs.add()
    record = scope.log_records.add()
    record.event_name = "claude_code.api_error"
    record.severity_number = 17
    add_attribute(record.attributes, "model", "claude-test")
    add_attribute(record.attributes, "error_type", "timeout")

    span = _spans(
        logs_to_traces(
            request,
            agent="claude-code-cli",
            settings=settings,
            now_ns=1_000,
        ).request
    )[0]

    assert span.status.code == 2
    assert span.status.message == "timeout"
    assert attributes_dict(span.attributes)["error.type"] == "timeout"


@pytest.mark.parametrize(
    ("event_name", "kind"),
    [
        ("claude_code.user_prompt", "AGENT"),
        ("claude_code.api_request", "LLM"),
        ("claude_code.api_error", "LLM"),
        ("claude_code.tool_decision", "TOOL"),
        ("claude_code.tool_result", "TOOL"),
        ("claude_code.interaction", "AGENT"),
        ("claude_code.llm_request", "LLM"),
        ("claude_code.tool.execution", "TOOL"),
    ],
)
def test_claude_event_fixtures_cover_documented_classification(
    settings: Settings,
    event_name: str,
    kind: str,
) -> None:
    request = ExportLogsServiceRequest()
    request.resource_logs.add().scope_logs.add().log_records.add().event_name = event_name

    span = _spans(
        logs_to_traces(
            request,
            agent="claude-code-cli",
            settings=settings,
            now_ns=1_000,
        ).request
    )[0]

    assert attributes_dict(span.attributes)["openinference.span.kind"] == kind


def test_source_resolution_prefers_explicit_header(settings: Settings) -> None:
    request = _codex_logs()

    assert resolve_agent(request, "codex") == "codex"
    assert resolve_agent(request, "") == "codex-cli"


def test_source_resolution_rejects_partially_unidentified_batch() -> None:
    request = _codex_logs()
    request.resource_logs.add().scope_logs.add().log_records.add().event_name = "codex.user_prompt"

    with pytest.raises(ValueError, match="any resource group"):
        resolve_agent(request, "")

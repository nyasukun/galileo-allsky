"""Convert agent OTLP signals into Galileo-valid GenAI trace spans."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import time
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
    ExportLogsServiceRequest,
)
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
from opentelemetry.proto.resource.v1.resource_pb2 import Resource
from opentelemetry.proto.trace.v1.trace_pb2 import Span

from .config import AGENTS, Settings
from .privacy import (
    CONTENT_DISABLED,
    any_value_to_python,
    message_json,
    pseudonymize,
    redact_text,
    sanitize_attributes,
    sanitized_text_value,
)

SPAN_KIND_INTERNAL = 1
SPAN_KIND_CLIENT = 3
STATUS_CODE_ERROR = 2
SEVERITY_ERROR = 17
UINT64_MAX = (1 << 64) - 1

SCHEMA_VERSION = "allsky.agent-otel.v1"

_AGENT_METADATA = {
    "codex": ("openai", "Codex"),
    "chatgpt": ("openai", "ChatGPT"),
    "claude-code": ("anthropic", "Claude Code"),
    "claude-cowork": ("anthropic", "Claude Cowork"),
    "codex-cli": ("openai", "Codex CLI"),
    "claude-code-cli": ("anthropic", "Claude Code CLI"),
}
_LOG_PRIMARY_AGENTS = frozenset({"codex", "codex-cli"})

_RESOURCE_AGENT_ALIASES = {
    "codex": "codex",
    "codex-desktop": "codex",
    "chatgpt": "chatgpt",
    "chatgpt-desktop": "chatgpt",
    "claude-code-desktop": "claude-code",
    "claude-cowork": "claude-cowork",
    "cowork": "claude-cowork",
    "codex-cli": "codex-cli",
    "codex_cli_rs": "codex-cli",
    "claude-code-cli": "claude-code-cli",
    "claude-code": "claude-code-cli",
}

_MODEL_KEYS = (
    "gen_ai.request.model",
    "request.model",
    "request_model",
    "model",
)
_CONVERSATION_ID_KEYS = (
    "conversation.id",
    "gen_ai.conversation.id",
    "session.id",
)
_TOOL_NAME_KEYS = ("gen_ai.tool.name", "tool.name", "tool_name", "tool")
_TOOL_ID_KEYS = (
    "gen_ai.tool.call.id",
    "tool_use_id",
    "tool_call_id",
    "call_id",
)
_DURATION_KEYS = ("duration_ms", "event.duration_ms", "interaction.duration_ms")
_EVENT_NAME_KEYS = ("event.name", "event_name", "name")
_SAFE_EVENT_NAMES = frozenset(
    {
        "codex.conversation_starts",
        "codex.user_prompt",
        "codex.api_request",
        "codex.sse_event",
        "codex.websocket_request",
        "codex.websocket_event",
        "codex.tool_decision",
        "codex.tool_result",
        "claude_code.user_prompt",
        "claude_code.assistant_response",
        "claude_code.api_request",
        "claude_code.api_error",
        "claude_code.api_refusal",
        "claude_code.api_retries_exhausted",
        "claude_code.tool_decision",
        "claude_code.tool_result",
        "claude_code.interaction",
        "claude_code.llm_request",
        "claude_code.tool",
        "claude_code.tool.blocked_on_user",
        "claude_code.tool.execution",
        "tool.output",
        "user_prompt",
        "assistant_response",
        "api_request",
        "api_error",
        "api_refusal",
        "api_retries_exhausted",
        "tool_decision",
        "tool_result",
        "response.completed",
        "codex.log",
        "chatgpt.log",
        "claude-code.log",
        "claude-cowork.log",
        "codex-cli.log",
        "claude-code-cli.log",
    }
)
_SAFE_SEVERITY_TEXT = re.compile(
    r"^(?:TRACE|DEBUG|INFO|WARN(?:ING)?|ERROR|FATAL)(?:[1-4])?$",
    re.IGNORECASE,
)
_SAFE_OPERATIONAL_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+@-]{0,255}$")

_USAGE_KEYS = {
    "input_tokens": "gen_ai.usage.input_tokens",
    "input_token_count": "gen_ai.usage.input_tokens",
    "output_tokens": "gen_ai.usage.output_tokens",
    "output_token_count": "gen_ai.usage.output_tokens",
    "cache_read_tokens": "gen_ai.usage.cache_read.input_tokens",
    "cached_token_count": "gen_ai.usage.cache_read.input_tokens",
    "cache_creation_tokens": "gen_ai.usage.cache_creation.input_tokens",
    "cache_write_tokens": "gen_ai.usage.cache_creation.input_tokens",
    "cache_write_token_count": "gen_ai.usage.cache_creation.input_tokens",
    "reasoning_token_count": "gen_ai.usage.reasoning.output_tokens",
    "tool_token_count": "gen_ai.usage.tool.output_tokens",
}


def _is_safe_event_name(value: str) -> bool:
    return value.strip() in _SAFE_EVENT_NAMES


class TransformError(ValueError):
    """Raised for an OTLP request that cannot be routed or normalized."""


class RequestLimitError(TransformError):
    """Raised when a valid OTLP request exceeds a configured processing bound."""


@dataclass(frozen=True)
class TransformResult:
    request: ExportTraceServiceRequest
    input_items: int
    output_spans: int
    diagnostics: dict[str, int] | None = None


def _attribute_map(attributes: Iterable[KeyValue]) -> dict[str, AnyValue]:
    return {attribute.key: attribute.value for attribute in attributes}


def _python_attribute_map(attributes: Iterable[KeyValue]) -> dict[str, Any]:
    return {attribute.key: any_value_to_python(attribute.value) for attribute in attributes}


def _find_text(values: dict[str, Any], keys: Sequence[str]) -> str:
    for key in keys:
        value = values.get(key)
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _find_number(values: dict[str, Any], keys: Sequence[str]) -> float | None:
    for key in keys:
        value = values.get(key)
        if isinstance(value, bool) or value is None:
            continue
        try:
            candidate = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(candidate):
            return candidate
    return None


def _upsert(attributes: Any, key: str, value: Any, *, overwrite: bool = True) -> None:
    matches = [index for index, attribute in enumerate(attributes) if attribute.key == key]
    if matches:
        attribute = attributes[matches[0]]
        for duplicate in reversed(matches[1:]):
            del attributes[duplicate]
        if overwrite:
            attribute.value.CopyFrom(_to_any_value(value))
        return
    pair = attributes.add()
    pair.key = key
    pair.value.CopyFrom(_to_any_value(value))


def _to_any_value(value: Any) -> AnyValue:
    result = AnyValue()
    if isinstance(value, bool):
        result.bool_value = value
    elif isinstance(value, int):
        result.int_value = value
    elif isinstance(value, float):
        result.double_value = value
    elif isinstance(value, str):
        result.string_value = value
    elif isinstance(value, (list, tuple)):
        for item in value:
            result.array_value.values.add().CopyFrom(_to_any_value(item))
    elif isinstance(value, dict):
        for raw_key, item in value.items():
            pair = result.kvlist_value.values.add()
            pair.key = str(raw_key)
            pair.value.CopyFrom(_to_any_value(item))
    elif value is None:
        result.string_value = ""
    else:
        result.string_value = str(value)
    return result


def _replace_attributes(
    destination: Any,
    source: Iterable[KeyValue],
    settings: Settings,
) -> None:
    sanitized = sanitize_attributes(
        list(source),
        capture_content=settings.capture_content,
        maximum=settings.max_content_chars,
        pseudonym_secret=settings.pseudonym_secret,
    )
    del destination[:]
    destination.extend(sanitized)


def _without_reserved_attributes(attributes: Iterable[KeyValue]) -> list[KeyValue]:
    return [
        attribute for attribute in attributes if not attribute.key.lower().startswith("galileo.")
    ]


def _resource_agent(resource: Resource) -> str:
    values = _python_attribute_map(resource.attributes)
    for key in ("allsky.agent", "agent.surface", "service.name"):
        candidate = str(values.get(key, "")).strip().lower()
        if candidate in AGENTS:
            return candidate
        if candidate in _RESOURCE_AGENT_ALIASES:
            return _RESOURCE_AGENT_ALIASES[candidate]
    return ""


def resolve_agent(request: Any, header_value: str) -> str:
    """Resolve one source for the whole inbound batch.

    An explicit X-Allsky-Agent header is authoritative. Without it, all resource
    groups must resolve to the same supported source.
    """

    explicit = header_value.strip().lower()
    if explicit:
        if explicit not in AGENTS:
            raise TransformError(
                f"unsupported X-Allsky-Agent {explicit!r}; expected one of {', '.join(AGENTS)}"
            )
        return explicit

    groups = getattr(request, "resource_spans", None)
    if groups is None:
        groups = getattr(request, "resource_logs", ())
    resolved_groups = [_resource_agent(group.resource) for group in groups]
    if any(not agent for agent in resolved_groups):
        raise TransformError(
            "X-Allsky-Agent is required when any resource group lacks a source identity"
        )
    resolved = set(resolved_groups)
    if len(resolved) == 1:
        return resolved.pop()
    if not resolved:
        raise TransformError(
            "X-Allsky-Agent is required when resource attributes do not identify a source"
        )
    raise TransformError("one OTLP request contains multiple agent sources")


def count_spans(request: ExportTraceServiceRequest) -> int:
    return sum(
        len(scope_spans.spans)
        for resource_spans in request.resource_spans
        for scope_spans in resource_spans.scope_spans
    )


def count_logs(request: ExportLogsServiceRequest) -> int:
    return sum(
        len(scope_logs.log_records)
        for resource_logs in request.resource_logs
        for scope_logs in resource_logs.scope_logs
    )


def _classify(name: str, attributes: dict[str, Any]) -> str:
    existing = str(attributes.get("openinference.span.kind", "")).strip().upper()
    if existing in {"AGENT", "LLM", "TOOL"}:
        return existing

    haystack = " ".join(
        (
            name,
            str(attributes.get("event.name", "")),
            str(attributes.get("span.type", "")),
            str(attributes.get("gen_ai.operation.name", "")),
        )
    ).lower()
    if any(
        token in haystack for token in ("tool", "execute_tool", "permission", "blocked_on_user")
    ):
        return "TOOL"
    if any(
        token in haystack
        for token in (
            "llm",
            "api_request",
            "api_error",
            "assistant_response",
            "chat",
            "completion",
            "model_request",
            "sse_event",
            "websocket_request",
            "websocket_event",
            "api_refusal",
            "api_retries_exhausted",
        )
    ):
        return "LLM"
    return "AGENT"


def _content_string(value: Any, settings: Settings) -> str:
    if not settings.capture_content:
        return CONTENT_DISABLED
    if value is None:
        return ""
    return sanitized_text_value(
        value,
        settings.max_content_chars,
        settings.pseudonym_secret,
    )


def _safe_original_name(name: str, settings: Settings) -> str:
    if _is_safe_event_name(name):
        return name.strip()
    if settings.capture_content:
        return redact_text(name, settings.max_content_chars)
    return CONTENT_DISABLED


def _safe_span_event_name(name: str, settings: Settings) -> str:
    if _is_safe_event_name(name):
        return name.strip()
    if settings.capture_content:
        return redact_text(name, settings.max_content_chars)
    return "allsky.span_event"


def _safe_severity_text(value: str, settings: Settings) -> str:
    if _SAFE_SEVERITY_TEXT.fullmatch(value.strip()):
        return value.strip()
    if settings.capture_content:
        return redact_text(value, 128)
    return CONTENT_DISABLED


def _safe_operational_label(
    value: str,
    settings: Settings,
    *,
    fallback: str,
) -> str:
    if not value:
        return fallback
    sanitized = redact_text(value, 256)
    if settings.capture_content or _SAFE_OPERATIONAL_LABEL.fullmatch(sanitized):
        return sanitized
    return fallback


def _candidate(
    attributes: dict[str, Any],
    keys: Sequence[str],
    fallback: Any,
) -> Any:
    for key in keys:
        value = attributes.get(key)
        if value not in (None, ""):
            return value
    return fallback


def _log_correlation_identity(attributes: dict[str, Any]) -> tuple[str, Any] | None:
    conversation_id = _candidate(attributes, _CONVERSATION_ID_KEYS, None)
    if conversation_id not in (None, ""):
        return "conversation", conversation_id

    prompt_id = attributes.get("prompt.id")
    if prompt_id not in (None, ""):
        return "prompt", prompt_id
    return None


def _required_io(
    span: Span,
    classification: str,
    original_attributes: dict[str, Any],
    settings: Settings,
    *,
    body: Any = None,
    event_name: str = "",
) -> None:
    input_candidate: Any = None
    output_candidate: Any = None

    if classification == "TOOL":
        input_candidate = _candidate(
            original_attributes,
            (
                "gen_ai.tool.call.arguments",
                "tool_parameters",
                "tool_input",
                "input.value",
                "full_command",
                "bash_command",
                "file_path",
            ),
            body if "decision" in event_name else None,
        )
        output_candidate = _candidate(
            original_attributes,
            (
                "gen_ai.tool.call.result",
                "tool_result",
                "result",
                "output.value",
                "_allsky.tool_event_output",
            ),
            body if "result" in event_name else None,
        )
        input_role = output_role = "tool"
    else:
        input_candidate = _candidate(
            original_attributes,
            (
                "gen_ai.input.messages",
                "input.value",
                "llm.input_messages",
                "prompt",
                "user_prompt",
            ),
            body if "user_prompt" in event_name else None,
        )
        output_candidate = _candidate(
            original_attributes,
            (
                "gen_ai.output.messages",
                "output.value",
                "llm.output_messages",
                "response",
                "assistant_response",
            ),
            body if "assistant_response" in event_name else None,
        )
        input_role, output_role = "user", "assistant"

    input_json = message_json(input_role, _content_string(input_candidate, settings))
    output_json = message_json(output_role, _content_string(output_candidate, settings))
    _upsert(span.attributes, "gen_ai.input.messages", input_json)
    _upsert(span.attributes, "gen_ai.output.messages", output_json)
    _upsert(span.attributes, "input.value", input_json)
    _upsert(span.attributes, "input.mime_type", "application/json")
    _upsert(span.attributes, "output.value", output_json)
    _upsert(span.attributes, "output.mime_type", "application/json")
    _upsert(span.attributes, "allsky.content.capture.enabled", settings.capture_content)
    _upsert(span.attributes, "allsky.content.input.present", input_candidate is not None)
    _upsert(span.attributes, "allsky.content.output.present", output_candidate is not None)


def _normalize_semantics(
    span: Span,
    *,
    agent: str,
    original_name: str,
    original_attributes: dict[str, Any],
    settings: Settings,
    body: Any = None,
    event_name: str = "",
) -> None:
    provider, agent_name = _AGENT_METADATA[agent]
    classification = _classify(original_name, original_attributes)
    model = _safe_operational_label(
        _find_text(original_attributes, _MODEL_KEYS),
        settings,
        fallback="",
    )
    tool_name = _safe_operational_label(
        _find_text(original_attributes, _TOOL_NAME_KEYS),
        settings,
        fallback="unknown",
    )
    tool_id = _find_text(original_attributes, _TOOL_ID_KEYS)

    _upsert(span.attributes, "allsky.agent", agent)
    _upsert(
        span.attributes,
        "allsky.original.span.name",
        _safe_original_name(original_name, settings),
    )
    _upsert(span.attributes, "allsky.schema.version", SCHEMA_VERSION)
    _upsert(span.attributes, "gen_ai.provider.name", provider)
    _upsert(span.attributes, "gen_ai.system", provider)
    _upsert(span.attributes, "openinference.span.kind", classification)

    if classification == "LLM":
        span.name = f"chat {model or agent_name}"
        span.kind = SPAN_KIND_CLIENT
        _upsert(span.attributes, "gen_ai.operation.name", "chat")
        if model:
            _upsert(span.attributes, "gen_ai.request.model", model)
            _upsert(span.attributes, "llm.model_name", model)
        _upsert(span.attributes, "llm.system", provider)
    elif classification == "TOOL":
        span.name = f"execute_tool {tool_name}"
        span.kind = SPAN_KIND_INTERNAL
        _upsert(span.attributes, "gen_ai.operation.name", "execute_tool")
        _upsert(span.attributes, "gen_ai.tool.name", tool_name)
        if tool_id:
            _upsert(
                span.attributes,
                "gen_ai.tool.call.id",
                pseudonymize(
                    tool_id,
                    settings.pseudonym_secret,
                    "tool_call",
                ),
            )
    else:
        span.name = f"invoke_agent {agent_name}"
        span.kind = SPAN_KIND_INTERNAL
        _upsert(span.attributes, "gen_ai.operation.name", "invoke_agent")
        _upsert(span.attributes, "gen_ai.agent.name", agent_name)

    for source_key, destination_key in _USAGE_KEYS.items():
        value = original_attributes.get(source_key)
        if isinstance(value, bool) or value is None:
            continue
        try:
            integer = int(value)
        except (TypeError, ValueError, OverflowError):
            continue
        if 0 <= integer <= (1 << 63) - 1:
            _upsert(span.attributes, destination_key, integer, overwrite=False)

    _required_io(
        span,
        classification,
        original_attributes,
        settings,
        body=body,
        event_name=event_name,
    )
    if span.status.code == STATUS_CODE_ERROR:
        error_type = _safe_operational_label(
            _find_text(original_attributes, ("error.type", "error_type")),
            settings,
            fallback="telemetry_error",
        )
        span.status.message = error_type
        _upsert(
            span.attributes,
            "error.type",
            error_type,
        )


def _normalize_resource(
    resource: Resource,
    *,
    agent: str,
    stream: str,
    settings: Settings,
) -> None:
    original = _without_reserved_attributes(resource.attributes)
    _replace_attributes(resource.attributes, original, settings)
    _upsert(resource.attributes, "allsky.agent", agent)
    _upsert(resource.attributes, "galileo.project.name", settings.project)
    _upsert(resource.attributes, "galileo.logstream.name", stream)


def _normalize_scope(scope: Any, *, agent: str, settings: Settings) -> None:
    original_attributes = _without_reserved_attributes(scope.attributes)
    _replace_attributes(scope.attributes, original_attributes, settings)
    scope.name = f"allsky_collector/{agent}"
    scope.version = SCHEMA_VERSION


def _normalize_span_identity_and_time(
    span: Span,
    *,
    agent: str,
    original_name: str,
    index: int,
    now_ns: int,
    pseudonym_secret: str,
) -> None:
    source_timestamp = span.end_time_unix_nano or span.start_time_unix_nano
    timestamp = min(UINT64_MAX - 1, max(1, source_timestamp or now_ns + index))
    if not _valid_identifier(span.trace_id, 16):
        span.trace_id = _derived_identifier(
            length=16,
            domain="trace-normalization/trace-id",
            secret=pseudonym_secret,
            agent=agent,
            event_name=original_name,
            timestamp=source_timestamp,
            index=index,
            body="trace",
        )
    if not _valid_identifier(span.span_id, 8):
        span.span_id = _derived_identifier(
            length=8,
            domain="trace-normalization/span-id",
            secret=pseudonym_secret,
            agent=agent,
            event_name=original_name,
            timestamp=source_timestamp,
            index=index,
            body="span",
        )
    if span.parent_span_id and not _valid_identifier(span.parent_span_id, 8):
        span.parent_span_id = b""
    if not span.start_time_unix_nano and not span.end_time_unix_nano:
        span.start_time_unix_nano = timestamp
        span.end_time_unix_nano = timestamp + 1
    elif not span.start_time_unix_nano:
        if span.end_time_unix_nano <= 1:
            span.start_time_unix_nano = 1
            span.end_time_unix_nano = 2
        else:
            span.start_time_unix_nano = span.end_time_unix_nano - 1
    elif not span.end_time_unix_nano or span.end_time_unix_nano <= span.start_time_unix_nano:
        if span.start_time_unix_nano >= UINT64_MAX:
            span.start_time_unix_nano = UINT64_MAX - 1
            span.end_time_unix_nano = UINT64_MAX
        else:
            span.end_time_unix_nano = span.start_time_unix_nano + 1


def normalize_traces(
    request: ExportTraceServiceRequest,
    *,
    agent: str,
    settings: Settings,
) -> TransformResult:
    result = ExportTraceServiceRequest()
    result.CopyFrom(request)
    result.DiscardUnknownFields()
    stream = settings.routes[agent]
    item_index = 0
    clock = time.time_ns()

    for resource_spans in result.resource_spans:
        resource_spans.schema_url = ""
        _normalize_resource(
            resource_spans.resource,
            agent=agent,
            stream=stream,
            settings=settings,
        )
        for scope_spans in resource_spans.scope_spans:
            scope_spans.schema_url = ""
            _normalize_scope(scope_spans.scope, agent=agent, settings=settings)
            for span in scope_spans.spans:
                original_name = span.name or "unnamed"
                original_attributes = _python_attribute_map(span.attributes)
                semantic_attributes = dict(original_attributes)
                for event in span.events:
                    if event.name == "tool.output":
                        event_payload = _python_attribute_map(event.attributes)
                        if event_payload:
                            semantic_attributes.setdefault(
                                "_allsky.tool_event_output",
                                event_payload,
                            )
                _replace_attributes(
                    span.attributes,
                    _without_reserved_attributes(span.attributes),
                    settings,
                )
                span.trace_state = ""
                if span.status.message:
                    span.status.message = (
                        redact_text(span.status.message, settings.max_content_chars)
                        if settings.capture_content
                        else CONTENT_DISABLED
                    )
                for event in span.events:
                    event.name = _safe_span_event_name(event.name, settings)
                    _replace_attributes(
                        event.attributes,
                        _without_reserved_attributes(event.attributes),
                        settings,
                    )
                for link in span.links:
                    link.trace_state = ""
                    _replace_attributes(
                        link.attributes,
                        _without_reserved_attributes(link.attributes),
                        settings,
                    )
                _normalize_span_identity_and_time(
                    span,
                    agent=agent,
                    original_name=original_name,
                    index=item_index,
                    now_ns=clock,
                    pseudonym_secret=settings.pseudonym_secret,
                )
                _normalize_semantics(
                    span,
                    agent=agent,
                    original_name=original_name,
                    original_attributes=semantic_attributes,
                    settings=settings,
                )
                conversation_id = _candidate(
                    original_attributes,
                    _CONVERSATION_ID_KEYS,
                    None,
                )
                if conversation_id not in (None, ""):
                    _upsert(
                        span.attributes,
                        "gen_ai.conversation.id",
                        pseudonymize(
                            conversation_id,
                            settings.pseudonym_secret,
                            "conversation",
                        ),
                    )
                item_index += 1

    total = count_spans(result)
    return TransformResult(request=result, input_items=total, output_spans=total)


def _event_name(
    attributes: dict[str, Any],
    body: Any,
    agent: str,
    otlp_event_name: str = "",
) -> str:
    if _is_safe_event_name(otlp_event_name):
        return otlp_event_name.strip()
    name = _find_text(attributes, _EVENT_NAME_KEYS)
    if _is_safe_event_name(name):
        return name
    if isinstance(body, str) and _is_safe_event_name(body):
        return body
    return f"{agent}.log"


def _valid_identifier(value: bytes, length: int) -> bool:
    return len(value) == length and any(value)


def _derived_identifier(
    *,
    length: int,
    domain: str,
    secret: str,
    agent: str,
    event_name: str,
    timestamp: int,
    index: int,
    body: Any,
) -> bytes:
    digest = hmac.new(secret.encode("utf-8"), digestmod=hashlib.sha256)

    def update_frame(value: Any) -> None:
        if isinstance(value, bytes):
            encoded = value
        else:
            try:
                encoded = json.dumps(
                    value,
                    allow_nan=False,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                    default=str,
                ).encode("utf-8", errors="replace")
            except (TypeError, ValueError):
                encoded = str(value).encode("utf-8", errors="replace")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)

    for component in (
        SCHEMA_VERSION,
        domain,
        agent,
        event_name,
        timestamp,
        index,
        body,
    ):
        update_frame(component)
    value = digest.digest()[:length]
    return value if any(value) else b"\x01" + value[1:]


def _correlation_identifier(
    *,
    length: int,
    purpose: str,
    identity: tuple[str, Any],
    agent: str,
    secret: str,
) -> bytes:
    namespace, value = identity
    return _derived_identifier(
        length=length,
        domain=f"logs-to-traces/{namespace}-{purpose}",
        secret=secret,
        agent=agent,
        event_name=f"{namespace}-{purpose}",
        timestamp=0,
        index=0,
        body=value,
    )


def logs_to_traces(
    request: ExportLogsServiceRequest,
    *,
    agent: str,
    settings: Settings,
    now_ns: int | None = None,
) -> TransformResult:
    result = ExportTraceServiceRequest()
    stream = settings.routes[agent]
    item_index = 0
    clock = time.time_ns() if now_ns is None else now_ns
    emitted_correlation_roots: set[bytes] = set()
    diagnostics: Counter[str] = Counter()

    for resource_logs in request.resource_logs:
        resource_spans = result.resource_spans.add()
        resource_spans.resource.CopyFrom(resource_logs.resource)
        resource_spans.schema_url = ""
        _normalize_resource(
            resource_spans.resource,
            agent=agent,
            stream=stream,
            settings=settings,
        )

        for scope_logs in resource_logs.scope_logs:
            scope_spans = resource_spans.scope_spans.add()
            scope_spans.scope.CopyFrom(scope_logs.scope)
            scope_spans.schema_url = ""
            _normalize_scope(scope_spans.scope, agent=agent, settings=settings)
            scope_default_identity: tuple[str, Any] | None = None
            if agent in _LOG_PRIMARY_AGENTS:
                identities_by_trace_id: dict[bytes, tuple[str, Any]] = {}
                for candidate_record in scope_logs.log_records:
                    candidate_attributes = _python_attribute_map(candidate_record.attributes)
                    candidate_identity = _log_correlation_identity(candidate_attributes)
                    if candidate_identity is None:
                        continue
                    candidate_trace_id = _correlation_identifier(
                        length=16,
                        purpose="group-trace-id",
                        identity=candidate_identity,
                        secret=settings.pseudonym_secret,
                        agent=agent,
                    )
                    identities_by_trace_id[candidate_trace_id] = candidate_identity
                if len(identities_by_trace_id) == 1:
                    scope_default_identity = next(iter(identities_by_trace_id.values()))

            for log_record in scope_logs.log_records:
                raw_attributes = _python_attribute_map(log_record.attributes)
                raw_body = any_value_to_python(log_record.body)
                event_name = _event_name(
                    raw_attributes,
                    raw_body,
                    agent,
                    log_record.event_name,
                )
                source_timestamp = log_record.time_unix_nano or log_record.observed_time_unix_nano
                timestamp = source_timestamp or clock + item_index
                explicit_correlation_identity = _log_correlation_identity(raw_attributes)
                correlation_identity = explicit_correlation_identity or scope_default_identity
                has_inbound_trace_id = _valid_identifier(log_record.trace_id, 16)
                if (
                    agent in _LOG_PRIMARY_AGENTS
                    and has_inbound_trace_id
                    and correlation_identity is None
                ):
                    diagnostics["logs.suppressed.uncorrelated_trace_id"] += 1
                    item_index += 1
                    continue
                prefer_correlation_identity = correlation_identity is not None and (
                    not has_inbound_trace_id or agent in _LOG_PRIMARY_AGENTS
                )
                if prefer_correlation_identity:
                    diagnostics[f"logs.grouping.{correlation_identity[0]}"] += 1
                    if explicit_correlation_identity is None:
                        diagnostics["logs.grouping.scope_inferred"] += 1
                elif has_inbound_trace_id:
                    diagnostics["logs.grouping.inbound_trace_id"] += 1
                else:
                    diagnostics["logs.grouping.record_fallback"] += 1
                trace_id = (
                    _correlation_identifier(
                        length=16,
                        purpose="group-trace-id",
                        identity=correlation_identity,
                        secret=settings.pseudonym_secret,
                        agent=agent,
                    )
                    if prefer_correlation_identity
                    else log_record.trace_id
                    if has_inbound_trace_id
                    else _derived_identifier(
                        length=16,
                        domain="logs-to-traces/trace-id",
                        secret=settings.pseudonym_secret,
                        agent=agent,
                        event_name=event_name,
                        timestamp=source_timestamp,
                        index=item_index,
                        body=raw_body,
                    )
                )
                span_id = _derived_identifier(
                    length=8,
                    domain="logs-to-traces/span-id",
                    secret=settings.pseudonym_secret,
                    agent=agent,
                    event_name=event_name,
                    timestamp=source_timestamp,
                    index=item_index,
                    body=raw_body,
                )
                correlation_root_span_id = (
                    _correlation_identifier(
                        length=8,
                        purpose="root-span-id",
                        identity=correlation_identity,
                        secret=settings.pseudonym_secret,
                        agent=agent,
                    )
                    if prefer_correlation_identity
                    else b""
                )
                classification = _classify(event_name, raw_attributes)
                is_correlation_root = (
                    bool(correlation_root_span_id)
                    and classification == "AGENT"
                    and correlation_root_span_id not in emitted_correlation_roots
                )
                if is_correlation_root:
                    span_id = correlation_root_span_id
                    emitted_correlation_roots.add(correlation_root_span_id)

                span = scope_spans.spans.add()
                span.trace_id = trace_id
                span.span_id = span_id
                if is_correlation_root:
                    span.parent_span_id = b""
                elif correlation_root_span_id:
                    span.parent_span_id = correlation_root_span_id
                elif (
                    _valid_identifier(log_record.trace_id, 16)
                    and _valid_identifier(log_record.span_id, 8)
                    and log_record.span_id != span_id
                ):
                    span.parent_span_id = log_record.span_id
                span.flags = log_record.flags

                duration_ms = _find_number(raw_attributes, _DURATION_KEYS)
                duration_ns = max(1, int((duration_ms or 0) * 1_000_000))
                span.end_time_unix_nano = max(2, timestamp)
                span.start_time_unix_nano = max(
                    1,
                    span.end_time_unix_nano - duration_ns,
                )

                sanitized = sanitize_attributes(
                    _without_reserved_attributes(log_record.attributes),
                    capture_content=settings.capture_content,
                    maximum=settings.max_content_chars,
                    pseudonym_secret=settings.pseudonym_secret,
                )
                span.attributes.extend(sanitized)
                _upsert(span.attributes, "event.name", event_name)
                _upsert(span.attributes, "allsky.signal", "logs")
                _upsert(span.attributes, "allsky.log.severity.number", log_record.severity_number)
                if log_record.severity_text:
                    _upsert(
                        span.attributes,
                        "allsky.log.severity.text",
                        _safe_severity_text(log_record.severity_text, settings),
                    )
                _upsert(
                    span.attributes,
                    "allsky.log.body",
                    _content_string(raw_body, settings),
                )

                success_value = raw_attributes.get("success")
                explicit_failure = success_value is False or (
                    isinstance(success_value, str)
                    and success_value.strip().lower() in {"false", "0", "failed"}
                )
                status_code = _find_number(
                    raw_attributes,
                    ("http.response.status_code", "status_code"),
                )
                if (
                    log_record.severity_number >= SEVERITY_ERROR
                    or explicit_failure
                    or (status_code is not None and status_code >= 400)
                    or any(
                        token in event_name.lower()
                        for token in ("error", "failed", "retries_exhausted")
                    )
                ):
                    span.status.code = STATUS_CODE_ERROR
                    error_type = _find_text(raw_attributes, ("error.type", "error_type"))
                    safe_error_type = _safe_operational_label(
                        error_type,
                        settings,
                        fallback="telemetry_error",
                    )
                    span.status.message = safe_error_type
                    _upsert(
                        span.attributes,
                        "error.type",
                        safe_error_type,
                    )

                _normalize_semantics(
                    span,
                    agent=agent,
                    original_name=event_name,
                    original_attributes=raw_attributes,
                    settings=settings,
                    body=raw_body,
                    event_name=event_name.lower(),
                )
                conversation_id = _candidate(
                    raw_attributes,
                    _CONVERSATION_ID_KEYS,
                    None,
                )
                if conversation_id not in (None, ""):
                    _upsert(
                        span.attributes,
                        "gen_ai.conversation.id",
                        pseudonymize(
                            conversation_id,
                            settings.pseudonym_secret,
                            "conversation",
                        ),
                    )
                elif correlation_identity is not None and correlation_identity[0] == "prompt":
                    _upsert(
                        span.attributes,
                        "gen_ai.conversation.id",
                        pseudonymize(
                            correlation_identity[1],
                            settings.pseudonym_secret,
                            "prompt",
                        ),
                    )
                item_index += 1

    result.DiscardUnknownFields()
    return TransformResult(
        request=result,
        input_items=item_index,
        output_spans=count_spans(result),
        diagnostics=dict(diagnostics),
    )

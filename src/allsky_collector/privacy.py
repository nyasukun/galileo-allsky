"""Fail-closed content handling for untrusted telemetry attributes."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any

from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue

CONTENT_DISABLED = "[content capture disabled]"
REDACTED = "[REDACTED]"
REDACTED_REASONING = "[REDACTED REASONING]"
OPERATIONAL_METADATA_OMITTED = "[operational metadata omitted]"

_SENSITIVE_KEY = re.compile(
    r"(?:^|[._-])(?:"
    r"api[-_]?key|authorization|proxy[-_]?authorization|"
    r"access[-_]?token|refresh[-_]?token|auth[-_]?token|"
    r"tokens?|secret|client[-_]?secret|password|passwd|cookie|"
    r"credentials?|private[-_]?key"
    r")(?:$|[._-])",
    re.IGNORECASE,
)
_REASONING_KEY = re.compile(
    r"(?:^|[._-])(?:"
    r"reasoning|thinking|thought[-_]?signature|"
    r"chain[-_]?of[-_]?thought|analysis|encrypted[-_]?(?:content|reasoning)"
    r")(?:$|[._-])",
    re.IGNORECASE,
)
_CONTENT_KEY = re.compile(
    r"(?:^|[._-])(?:"
    r"prompt|completion|input|output|content|body|messages?|"
    r"arguments?|parameters?|result|response|command|path|"
    r"file[-_]?content|raw|url|error|exception|stacktrace|slug|cwd"
    r")(?:$|[._-])",
    re.IGNORECASE,
)
_SAFE_METADATA_KEY = re.compile(
    r"^(?:"
    r"(?:gen_ai\.usage\.)?(?:"
    r"input_tokens|output_tokens|total_tokens|"
    r"cache_read\.input_tokens|cache_creation\.input_tokens|"
    r"reasoning\.output_tokens|tool\.output_tokens"
    r")|"
    r"(?:input|output|cached|reasoning|tool)[-_]?(?:tokens?|token[-_]?count)|"
    r"cache[-_]?(?:read|write|creation)[-_]?(?:tokens?|token[-_]?count)|"
    r"(?:prompt|response|input|output|tool_input|tool_result)[-_]?"
    r"(?:length|size|bytes|size_bytes)|"
    r"gen_ai\.response\.(?:id|model|finish_reasons)|"
    r"http\.response\.status_code|"
    r"error(?:\.type|_type|\.code|_code)|"
    r"cost_usd|gen_ai\.usage\.cost"
    r")$",
    re.IGNORECASE,
)
_SAFE_OPERATIONAL_KEY = re.compile(
    r"^(?:"
    r"service\.(?:name|namespace|version)|"
    r"telemetry\.sdk\.(?:name|language|version)|"
    r"deployment\.environment(?:\.name)?|"
    r"cloud\.(?:provider|platform|region|availability_zone)|"
    r"os\.(?:type|name|version)|host\.arch|"
    r"process\.runtime\.(?:name|version|description)|"
    r"agent\.surface|"
    r"event(?:\.name|_name|\.kind|\.domain|\.sequence)|"
    r"span\.type|"
    r"gen_ai\.(?:"
    r"operation\.name|provider\.name|system|request\.model|"
    r"response\.(?:model|finish_reasons)|tool\.(?:name|call\.id)"
    r")|"
    r"openinference\.span\.kind|"
    r"llm\.(?:system|model_name)|"
    r"(?:request|response)[._-]?model|"
    r"model|provider|system|operation|agent_name|"
    r"tool(?:[._-]?name)?|"
    r"auth_mode|originator|sandbox_mode|approval_mode|query_source|speed|"
    r"stop_reason|finish_reasons|decision|source|success|status|status_code|"
    r"attempts?|duration_ms|event\.duration_ms|interaction\.duration_ms|"
    r"exception\.type"
    r")$",
    re.IGNORECASE,
)
_IDENTITY_KEY = re.compile(
    r"^(?:"
    r"session\.id|conversation\.id|gen_ai\.conversation\.id|"
    r"user\.(?:id|email|account_uuid|account_id)|"
    r"organization\.id|host\.id|host\.name|workspace\.host_paths"
    r"|service\.instance\.id|device\.id|"
    r"gen_ai\.tool\.call\.id|tool_use_id|tool_call_id|call_id|prompt\.id"
    r")$",
    re.IGNORECASE,
)
_SAFE_ATTRIBUTE_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,255}$")
_SAFE_OPERATIONAL_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+@=%-]{0,255}$")
_KNOWN_SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "api-key",
        "authorization",
        "proxy_authorization",
        "proxy-authorization",
        "access_token",
        "access-token",
        "refresh_token",
        "refresh-token",
        "auth_token",
        "auth-token",
        "token",
        "password",
        "passwd",
        "cookie",
        "set-cookie",
        "credential",
        "credentials",
        "private_key",
        "private-key",
        "client_secret",
        "client-secret",
    }
)
_KNOWN_REASONING_KEYS = frozenset(
    {
        "reasoning",
        "reasoning.content",
        "thinking",
        "thinking.content",
        "thought_signature",
        "thought-signature",
        "chain_of_thought",
        "chain-of-thought",
        "analysis",
        "encrypted_content",
        "encrypted-content",
        "encrypted_reasoning",
        "encrypted-reasoning",
    }
)
_KNOWN_CONTENT_KEYS = frozenset(
    {
        "prompt",
        "user_prompt",
        "assistant_response",
        "completion",
        "input",
        "output",
        "content",
        "body",
        "message",
        "messages",
        "arguments",
        "parameters",
        "tool_parameters",
        "result",
        "response",
        "command",
        "bash_command",
        "full_command",
        "path",
        "file_path",
        "file_content",
        "raw",
        "url",
        "error",
        "exception",
        "stacktrace",
        "slug",
        "cwd",
        "tool_input",
        "tool_output",
        "tool_result",
        "tool.input",
        "tool.output",
        "input.value",
        "output.value",
        "gen_ai.input.messages",
        "gen_ai.output.messages",
        "gen_ai.tool.call.arguments",
        "gen_ai.tool.call.result",
        "llm.input_messages",
        "llm.output_messages",
        "http.request.body",
        "http.response.body",
        "exception.message",
        "exception.stacktrace",
        "error.message",
        "url.full",
        "url.path",
    }
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_COMMON_SECRET = re.compile(r"(?i)\b(?:sk|rk|pk|ghp|github_pat|xox[baprs])[-_][A-Za-z0-9_-]{8,}")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b")
_CLOUD_ACCESS_KEY = re.compile(r"\b(?:(?:AKIA|ASIA)[A-Z0-9]{16}|AIza[0-9A-Za-z_-]{20,})\b")
_PEM_PRIVATE_KEY = re.compile(
    r"-----BEGIN ([A-Z0-9 ]*PRIVATE KEY)-----.*?-----END \1-----",
    re.IGNORECASE | re.DOTALL,
)
_AUTHORIZATION_HEADER = re.compile(r"(?im)\b(authorization|proxy-authorization)\s*:[^\r\n]*")
_COOKIE_HEADER = re.compile(r"(?im)\b(cookie|set-cookie)\s*:[^\r\n]*")
_DATA_URI = re.compile(r"(?i)data:[^,\s\"']{0,240};base64,[A-Za-z0-9+/=_-]+")
_ASSIGNMENT_SECRET_NAME = (
    r"(?:[A-Za-z0-9_.-]+[-_.])?"
    r"(?:"
    r"api[-_]?key|access[-_]?token|refresh[-_]?token|auth[-_]?token|"
    r"tokens?|password|passwd|client[-_]?secret|cookie|"
    r"private[-_]?key|credentials?|secret"
    r")"
)
_QUOTED_ASSIGNMENT_SECRET = re.compile(
    rf"(?i)[\"']?\b({_ASSIGNMENT_SECRET_NAME})[\"']?\s*[:=]\s*"
    r"([\"'])[^\"'\r\n]{1,4096}\2"
)
_ASSIGNMENT_SECRET = re.compile(
    rf"(?i)\b({_ASSIGNMENT_SECRET_NAME})\s*[:=]\s*"
    r"(?:[\"'])?[^,\s\"'}]+"
)
_REASONING_ASSIGNMENT_NAME = (
    r"reasoning|thinking|thought[-_]?signature|"
    r"chain[-_]?of[-_]?thought|analysis|encrypted[-_]?(?:content|reasoning)"
)
_QUOTED_REASONING = re.compile(
    rf"(?i)[\"']?\b({_REASONING_ASSIGNMENT_NAME})[\"']?\s*[:=]\s*"
    r"([\"']).*?\2"
)
_UNQUOTED_REASONING = re.compile(rf"(?i)\b({_REASONING_ASSIGNMENT_NAME})\s*[:=]\s*[^,\r\n}}]+")


def clip_text(value: str, maximum: int) -> str:
    if len(value) <= maximum:
        return value
    suffix = f"… [truncated; original chars={len(value)}]"
    if len(suffix) >= maximum:
        return suffix[:maximum]
    return value[: maximum - len(suffix)] + suffix


def redact_text(value: str, maximum: int) -> str:
    value = _DATA_URI.sub("[data URI omitted]", value)
    value = _PEM_PRIVATE_KEY.sub("[PRIVATE KEY REDACTED]", value)
    value = _AUTHORIZATION_HEADER.sub(
        lambda match: f"{match.group(1)}: [REDACTED]",
        value,
    )
    value = _COOKIE_HEADER.sub(lambda match: f"{match.group(1)}: [REDACTED]", value)
    value = _BEARER.sub("Bearer [REDACTED]", value)
    value = _JWT.sub(REDACTED, value)
    value = _CLOUD_ACCESS_KEY.sub(REDACTED, value)
    value = _COMMON_SECRET.sub(REDACTED, value)
    value = _QUOTED_ASSIGNMENT_SECRET.sub(
        lambda match: f"{match.group(1)}=[REDACTED]",
        value,
    )
    value = _ASSIGNMENT_SECRET.sub(lambda match: f"{match.group(1)}=[REDACTED]", value)
    value = _QUOTED_REASONING.sub(
        lambda match: f"{match.group(1)}={REDACTED_REASONING}",
        value,
    )
    value = _UNQUOTED_REASONING.sub(
        lambda match: f"{match.group(1)}={REDACTED_REASONING}",
        value,
    )
    return clip_text(value, maximum)


def is_content_key(key: str) -> bool:
    if is_safe_metadata_key(key) or is_safe_operational_key(key):
        return False
    return bool(_CONTENT_KEY.search(key))


def is_reasoning_key(key: str) -> bool:
    return bool(_REASONING_KEY.search(key))


def is_sensitive_key(key: str) -> bool:
    if is_safe_metadata_key(key):
        return False
    return bool(_SENSITIVE_KEY.search(key))


def is_safe_metadata_key(key: str) -> bool:
    return bool(_SAFE_METADATA_KEY.fullmatch(key))


def is_safe_operational_key(key: str) -> bool:
    return bool(_SAFE_OPERATIONAL_KEY.fullmatch(key))


def is_identity_key(key: str) -> bool:
    return bool(_IDENTITY_KEY.fullmatch(key))


def _approved_attribute_key(key: str, *, capture_content: bool) -> str | None:
    if not _SAFE_ATTRIBUTE_KEY.fullmatch(key):
        return None
    canonical = key.lower()
    if is_safe_metadata_key(key):
        return canonical
    if is_identity_key(key):
        return canonical
    if is_sensitive_key(key):
        return canonical if canonical in _KNOWN_SENSITIVE_KEYS else None
    if is_reasoning_key(key):
        return canonical if canonical in _KNOWN_REASONING_KEYS else None
    if is_content_key(key):
        return canonical if canonical in _KNOWN_CONTENT_KEYS else None
    if is_safe_operational_key(key):
        return canonical
    if capture_content:
        return key
    return None


def pseudonymize(value: Any, secret: str, namespace: str) -> str:
    if isinstance(value, str):
        serialized = value
    else:
        try:
            serialized = json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        except (TypeError, ValueError):
            serialized = str(value)
    digest = hmac.new(
        secret.encode("utf-8"),
        f"{namespace}\0{serialized}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return f"allsky:{namespace}:{digest}"


def _identity_namespace(key: str) -> str:
    normalized = key.lower()
    if normalized in {
        "session.id",
        "conversation.id",
        "gen_ai.conversation.id",
    }:
        return "conversation"
    if normalized in {
        "gen_ai.tool.call.id",
        "tool_use_id",
        "tool_call_id",
        "call_id",
    }:
        return "tool_call"
    if normalized == "prompt.id":
        return "prompt"
    return normalized


def any_value_to_python(value: AnyValue) -> Any:
    kind = value.WhichOneof("value")
    if kind is None:
        return None
    if kind == "array_value":
        return [any_value_to_python(item) for item in value.array_value.values]
    if kind == "kvlist_value":
        return {item.key: any_value_to_python(item.value) for item in value.kvlist_value.values}
    if kind == "bytes_value":
        return f"[binary omitted: {len(value.bytes_value)} bytes]"
    return getattr(value, kind)


def _normalize(
    value: Any,
    *,
    capture_content: bool,
    maximum: int,
    pseudonym_secret: str,
    depth: int = 0,
    operational: bool = False,
) -> Any:
    if depth >= 10:
        return "[maximum depth reached]"
    if isinstance(value, float) and not math.isfinite(value):
        return OPERATIONAL_METADATA_OMITTED
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        stripped = value.lstrip()
        if stripped.startswith(("{", "[")):
            try:
                structured = json.loads(value)
            except (json.JSONDecodeError, RecursionError):
                pass
            else:
                if isinstance(structured, (dict, list)):
                    normalized = _normalize(
                        structured,
                        capture_content=capture_content,
                        maximum=maximum,
                        pseudonym_secret=pseudonym_secret,
                        depth=depth + 1,
                        operational=operational,
                    )
                    try:
                        serialized = clip_text(
                            json.dumps(
                                normalized,
                                ensure_ascii=False,
                                separators=(",", ":"),
                                sort_keys=True,
                            ),
                            maximum,
                        )
                        if (
                            operational
                            and not capture_content
                            and not _SAFE_OPERATIONAL_VALUE.fullmatch(serialized)
                        ):
                            return OPERATIONAL_METADATA_OMITTED
                        return serialized
                    except (TypeError, ValueError):
                        return REDACTED
        sanitized = redact_text(value, maximum)
        if operational and not capture_content and not _SAFE_OPERATIONAL_VALUE.fullmatch(sanitized):
            return OPERATIONAL_METADATA_OMITTED
        return sanitized
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"[binary omitted: {len(value)} bytes]"
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for raw_key, item in list(value.items())[:100]:
            key = str(raw_key)
            approved_key = _approved_attribute_key(
                key,
                capture_content=capture_content,
            )
            if approved_key is None:
                continue
            if is_sensitive_key(key):
                normalized[approved_key] = REDACTED
            elif is_safe_metadata_key(key):
                normalized[approved_key] = _normalize(
                    item,
                    capture_content=capture_content,
                    maximum=maximum,
                    pseudonym_secret=pseudonym_secret,
                    depth=depth + 1,
                    operational=True,
                )
            elif is_reasoning_key(key):
                normalized[approved_key] = REDACTED_REASONING
            elif is_identity_key(key):
                normalized[approved_key] = pseudonymize(
                    item,
                    pseudonym_secret,
                    _identity_namespace(key),
                )
            elif is_content_key(key) and not capture_content:
                normalized[approved_key] = CONTENT_DISABLED
            elif capture_content or is_safe_operational_key(key):
                normalized[approved_key] = _normalize(
                    item,
                    capture_content=capture_content,
                    maximum=maximum,
                    pseudonym_secret=pseudonym_secret,
                    depth=depth + 1,
                    operational=is_safe_operational_key(key),
                )
            else:
                continue
        return normalized
    if isinstance(value, Sequence):
        return [
            _normalize(
                item,
                capture_content=capture_content,
                maximum=maximum,
                pseudonym_secret=pseudonym_secret,
                depth=depth + 1,
                operational=operational,
            )
            for item in list(value)[:100]
        ]
    return redact_text(str(value), maximum)


def sanitized_text_value(value: Any, maximum: int, pseudonym_secret: str) -> str:
    """Serialize a Python value after recursive secret and reasoning removal."""

    normalized = _normalize(
        value,
        capture_content=True,
        maximum=maximum,
        pseudonym_secret=pseudonym_secret,
    )
    if isinstance(normalized, str):
        return normalized
    try:
        serialized = json.dumps(
            normalized,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError):
        serialized = REDACTED
    return clip_text(serialized, maximum)


def python_to_any_value(value: Any) -> AnyValue:
    result = AnyValue()
    if value is None:
        result.string_value = ""
    elif isinstance(value, bool):
        result.bool_value = value
    elif isinstance(value, int):
        result.int_value = value
    elif isinstance(value, float):
        result.double_value = value
    elif isinstance(value, str):
        result.string_value = value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            pair = result.kvlist_value.values.add()
            pair.key = str(key)
            pair.value.CopyFrom(python_to_any_value(item))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            result.array_value.values.add().CopyFrom(python_to_any_value(item))
    else:
        result.string_value = str(value)
    return result


def sanitized_any_value(
    key: str,
    value: AnyValue,
    *,
    capture_content: bool,
    maximum: int,
    pseudonym_secret: str,
) -> AnyValue:
    if is_sensitive_key(key):
        return python_to_any_value(REDACTED)
    if is_safe_metadata_key(key):
        raw_value = any_value_to_python(value)
        if key in {"cost_usd", "gen_ai.usage.cost"} and (
            isinstance(raw_value, bool)
            or not isinstance(raw_value, (int, float))
            or not math.isfinite(float(raw_value))
            or raw_value < 0
        ):
            return python_to_any_value(OPERATIONAL_METADATA_OMITTED)
        normalized = _normalize(
            raw_value,
            capture_content=capture_content,
            maximum=maximum,
            pseudonym_secret=pseudonym_secret,
            operational=True,
        )
        return python_to_any_value(normalized)
    if is_reasoning_key(key):
        return python_to_any_value(REDACTED_REASONING)
    if is_identity_key(key):
        return python_to_any_value(
            pseudonymize(
                any_value_to_python(value),
                pseudonym_secret,
                _identity_namespace(key),
            )
        )
    if is_content_key(key) and not capture_content:
        return python_to_any_value(CONTENT_DISABLED)
    normalized = _normalize(
        any_value_to_python(value),
        capture_content=capture_content,
        maximum=maximum,
        pseudonym_secret=pseudonym_secret,
        operational=is_safe_operational_key(key),
    )
    return python_to_any_value(normalized)


def sanitize_attributes(
    attributes: Sequence[KeyValue],
    *,
    capture_content: bool,
    maximum: int,
    pseudonym_secret: str,
) -> list[KeyValue]:
    result: list[KeyValue] = []
    seen: set[str] = set()
    for attribute in attributes:
        key = attribute.key
        approved_key = _approved_attribute_key(
            key,
            capture_content=capture_content,
        )
        if approved_key is None or approved_key in seen:
            continue
        seen.add(approved_key)
        pair = KeyValue(key=approved_key)
        pair.value.CopyFrom(
            sanitized_any_value(
                approved_key,
                attribute.value,
                capture_content=capture_content,
                maximum=maximum,
                pseudonym_secret=pseudonym_secret,
            )
        )
        result.append(pair)
    return result


def message_json(role: str, value: str = CONTENT_DISABLED) -> str:
    return json.dumps([{"role": role, "content": value}], ensure_ascii=False)

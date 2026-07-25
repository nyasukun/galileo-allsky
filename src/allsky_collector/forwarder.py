"""Galileo's direct OTLP/HTTP compatibility adapter."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timezone
from email.message import Message
from email.utils import format_datetime, parsedate_to_datetime

from google.protobuf.message import DecodeError
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)

from .config import Settings


class UpstreamError(RuntimeError):
    """An HTTP or transport failure returned to the OTLP client."""

    def __init__(
        self,
        status: int,
        message: str,
        *,
        retry_after: str = "",
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class UpstreamProtocolError(UpstreamError):
    """A successful upstream response that violates the documented contract."""


UPSTREAM_PROTOCOL_STATUS = 424


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Treat redirects as upstream failures so credentials never change origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True)
class GalileoResult:
    rejected_spans: int = 0
    error_message: str = ""
    partial_success_present: bool = False


def _partial_success_message(rejected_spans: int, upstream_message: object) -> str:
    if rejected_spans:
        return f"Galileo rejected {rejected_spans} span(s)"
    if upstream_message:
        return "Galileo reported a partial-success warning"
    return ""


def _header_content_type(headers: Mapping[str, str] | Message) -> str:
    if isinstance(headers, Message):
        return headers.get_content_type().lower()
    return str(headers.get("Content-Type", "")).split(";", 1)[0].strip().lower()


def _receiver_status(upstream_status: int) -> int:
    """Return a safe status for the Agent-facing receiver response."""

    if 400 <= upstream_status <= 599:
        return upstream_status
    return UPSTREAM_PROTOCOL_STATUS


def _safe_retry_after(value: str) -> str:
    candidate = value.strip()
    if re.fullmatch(r"\d{1,10}", candidate):
        return candidate
    try:
        parsed = parsedate_to_datetime(candidate)
        if parsed.tzinfo is None:
            return ""
        return format_datetime(parsed.astimezone(timezone.utc), usegmt=True)
    except (TypeError, ValueError, OverflowError):
        return ""


def _parse_json_response(body: bytes) -> GalileoResult:
    try:
        payload = json.loads(body or b"{}")
    except json.JSONDecodeError as exc:
        raise UpstreamProtocolError(
            UPSTREAM_PROTOCOL_STATUS,
            "Galileo returned malformed JSON for OTLP success",
        ) from exc
    if not isinstance(payload, dict):
        raise UpstreamProtocolError(
            UPSTREAM_PROTOCOL_STATUS,
            "Galileo returned a non-object JSON OTLP response",
        )
    partial = payload.get("partialSuccess")
    if partial is None:
        partial = payload.get("partial_success")
    if partial is None:
        return GalileoResult()
    if not isinstance(partial, dict):
        raise UpstreamProtocolError(
            UPSTREAM_PROTOCOL_STATUS,
            "Galileo returned an invalid partialSuccess value",
        )
    rejected = partial.get("rejectedSpans", partial.get("rejected_spans", 0))
    message = partial.get("errorMessage", partial.get("error_message", ""))
    if (
        isinstance(rejected, bool)
        or not isinstance(rejected, int)
        or not 0 <= rejected <= (1 << 63) - 1
    ):
        raise UpstreamProtocolError(
            UPSTREAM_PROTOCOL_STATUS,
            "Galileo returned an invalid rejectedSpans value",
        )
    rejected_spans = rejected
    return GalileoResult(
        rejected_spans=rejected_spans,
        error_message=_partial_success_message(rejected_spans, message),
        partial_success_present=True,
    )


def _parse_protobuf_response(body: bytes) -> GalileoResult:
    response = ExportTraceServiceResponse()
    try:
        response.ParseFromString(body)
    except DecodeError as exc:
        raise UpstreamProtocolError(
            UPSTREAM_PROTOCOL_STATUS,
            "Galileo returned malformed protobuf for OTLP success",
        ) from exc
    if not response.HasField("partial_success"):
        return GalileoResult()
    if response.partial_success.rejected_spans < 0:
        raise UpstreamProtocolError(
            UPSTREAM_PROTOCOL_STATUS,
            "Galileo returned a negative rejectedSpans value",
        )
    return GalileoResult(
        rejected_spans=response.partial_success.rejected_spans,
        error_message=_partial_success_message(
            response.partial_success.rejected_spans,
            response.partial_success.error_message,
        ),
        partial_success_present=True,
    )


def parse_galileo_response(
    body: bytes,
    headers: Mapping[str, str] | Message,
) -> GalileoResult:
    """Accept Galileo's documented JSON response and standard OTLP protobuf."""

    if not body:
        return GalileoResult()
    content_type = _header_content_type(headers)
    stripped = body.lstrip()
    if stripped.startswith((b"{", b"[")) or content_type in {
        "application/json",
        "text/json",
    }:
        return _parse_json_response(body)
    if content_type == "application/x-protobuf":
        return _parse_protobuf_response(body)
    return _parse_protobuf_response(body)


class GalileoForwarder:
    """Make exactly one bounded upstream attempt per receiver request.

    The agent's OTLP exporter owns retry. Avoiding nested synchronous retries
    prevents one request from outliving the exporter timeout and racing a
    duplicate retry.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        opener: urllib.request.OpenerDirector | None = None,
    ) -> None:
        self._settings = settings
        self._opener = opener or urllib.request.build_opener(_NoRedirectHandler())

    def export(
        self,
        request: ExportTraceServiceRequest,
        *,
        log_stream: str,
    ) -> GalileoResult:
        payload = request.SerializeToString()
        outbound = urllib.request.Request(
            self._settings.endpoint,
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/x-protobuf",
                "Galileo-API-Key": self._settings.api_key,
                "project": self._settings.project,
                "logstream": log_stream,
                "User-Agent": "galileo-allsky/0.1.0",
            },
        )
        try:
            with self._opener.open(
                outbound,
                timeout=self._settings.forward_timeout_seconds,
            ) as response:
                body = response.read(4 * 1024 * 1024 + 1)
                if len(body) > 4 * 1024 * 1024:
                    raise UpstreamProtocolError(
                        UPSTREAM_PROTOCOL_STATUS,
                        "Galileo OTLP response exceeded 4 MiB",
                    )
                if response.status != 200:
                    raise UpstreamError(
                        _receiver_status(response.status),
                        f"Galileo returned HTTP {response.status}",
                        retry_after=_safe_retry_after(response.headers.get("Retry-After", "")),
                    )
                return parse_galileo_response(body, response.headers)
        except urllib.error.HTTPError as exc:
            exc.read(4097)
            retry_after = (
                _safe_retry_after(exc.headers.get("Retry-After", "")) if exc.headers else ""
            )
            raise UpstreamError(
                _receiver_status(exc.code),
                f"Galileo returned HTTP {exc.code}",
                retry_after=retry_after,
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise UpstreamError(
                503,
                f"Galileo connection failed: {type(reason).__name__}",
            ) from exc

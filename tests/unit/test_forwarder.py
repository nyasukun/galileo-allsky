from __future__ import annotations

import io
import json
import urllib.error
from email.message import Message

import pytest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)

from allsky_collector.config import Settings
from allsky_collector.forwarder import (
    GalileoForwarder,
    UpstreamError,
    UpstreamProtocolError,
    _safe_retry_after,
    parse_galileo_response,
)


def test_parse_documented_galileo_json_partial_success() -> None:
    result = parse_galileo_response(
        json.dumps(
            {
                "partialSuccess": {
                    "rejectedSpans": 2,
                    "errorMessage": "No GenAI patterns detected",
                }
            }
        ).encode(),
        {"Content-Type": "application/json"},
    )

    assert result.partial_success_present is True
    assert result.rejected_spans == 2
    assert result.error_message == "Galileo rejected 2 span(s)"


def test_parse_binary_otlp_partial_success() -> None:
    response = ExportTraceServiceResponse()
    response.partial_success.rejected_spans = 1
    response.partial_success.error_message = "one rejected"

    result = parse_galileo_response(
        response.SerializeToString(),
        {"Content-Type": "application/x-protobuf"},
    )

    assert result.partial_success_present is True
    assert result.rejected_spans == 1
    assert result.error_message == "Galileo rejected 1 span(s)"


def test_empty_success_response_is_full_success() -> None:
    result = parse_galileo_response(b"", {})

    assert result.partial_success_present is False
    assert result.rejected_spans == 0


def test_invalid_success_json_is_protocol_error() -> None:
    with pytest.raises(UpstreamProtocolError, match="malformed JSON"):
        parse_galileo_response(b"{", {"Content-Type": "application/json"})


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b"[]", "non-object"),
        (b'{"partialSuccess":"bad"}', "invalid partialSuccess"),
        (
            b'{"partialSuccess":{"rejectedSpans":"not-a-number"}}',
            "invalid rejectedSpans",
        ),
        (b'{"partialSuccess":{"rejectedSpans":true}}', "invalid rejectedSpans"),
        (b'{"partialSuccess":{"rejectedSpans":1.9}}', "invalid rejectedSpans"),
        (b'{"partialSuccess":{"rejectedSpans":-2}}', "invalid rejectedSpans"),
        (
            b'{"partialSuccess":{"rejectedSpans":9223372036854775808}}',
            "invalid rejectedSpans",
        ),
    ],
)
def test_invalid_success_shapes_are_protocol_errors(
    payload: bytes,
    message: str,
) -> None:
    with pytest.raises(UpstreamProtocolError, match=message):
        parse_galileo_response(payload, {"Content-Type": "application/json"})


def test_json_snake_case_warning_is_preserved() -> None:
    result = parse_galileo_response(
        b'{"partial_success":{"rejected_spans":0,"error_message":"warning"}}',
        {"Content-Type": "application/json"},
    )

    assert result.partial_success_present is True
    assert result.rejected_spans == 0
    assert result.error_message == "Galileo reported a partial-success warning"


def test_binary_negative_rejected_spans_is_protocol_error() -> None:
    response = ExportTraceServiceResponse()
    response.partial_success.rejected_spans = -1

    with pytest.raises(UpstreamProtocolError, match="negative rejectedSpans"):
        parse_galileo_response(
            response.SerializeToString(),
            {"Content-Type": "application/x-protobuf"},
        )


def test_retry_after_http_date_is_normalized_to_gmt() -> None:
    assert _safe_retry_after("Sun, 06 Nov 1994 08:49:37 +0100") == "Sun, 06 Nov 1994 07:49:37 GMT"


class _Response:
    def __init__(
        self,
        *,
        status: int = 200,
        body: bytes = b"{}",
        content_type: str = "application/json",
    ) -> None:
        self.status = status
        self._body = body
        self.headers = Message()
        self.headers["Content-Type"] = content_type

    def read(self, _limit: int) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _Opener:
    def __init__(self, outcome) -> None:
        self.outcome = outcome
        self.request = None
        self.timeout = None

    def open(self, request, *, timeout: float):
        self.request = request
        self.timeout = timeout
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def _settings() -> Settings:
    return Settings(
        api_key="secret-key",
        project="project",
        endpoint="http://127.0.0.1:9999/exact",
        allow_insecure_upstream=True,
    )


def test_forwarder_sets_trusted_headers_and_exact_endpoint() -> None:
    opener = _Opener(_Response())
    forwarder = GalileoForwarder(_settings(), opener=opener)

    result = forwarder.export(ExportTraceServiceRequest(), log_stream="stream")

    assert result.rejected_spans == 0
    assert opener.request.full_url == "http://127.0.0.1:9999/exact"
    assert opener.request.get_header("Galileo-api-key") == "secret-key"
    assert opener.request.get_header("Project") == "project"
    assert opener.request.get_header("Logstream") == "stream"
    assert opener.timeout == 5.0


def test_forwarder_converts_http_error_and_retry_after() -> None:
    headers = Message()
    headers["Content-Type"] = "application/json"
    headers["Retry-After"] = "3"
    error = urllib.error.HTTPError(
        "http://stub",
        503,
        "unavailable",
        headers,
        io.BytesIO(b'{"detail":"try later"}'),
    )
    forwarder = GalileoForwarder(_settings(), opener=_Opener(error))

    with pytest.raises(UpstreamError) as caught:
        forwarder.export(ExportTraceServiceRequest(), log_stream="stream")

    assert caught.value.status == 503
    assert caught.value.retry_after == "3"
    assert str(caught.value) == "Galileo returned HTTP 503"


def test_forwarder_converts_connection_failure() -> None:
    forwarder = GalileoForwarder(
        _settings(),
        opener=_Opener(urllib.error.URLError("offline")),
    )

    with pytest.raises(UpstreamError) as caught:
        forwarder.export(ExportTraceServiceRequest(), log_stream="stream")

    assert caught.value.status == 503
    assert "connection failed" in str(caught.value)


def test_forwarder_rejects_oversized_success_response() -> None:
    forwarder = GalileoForwarder(
        _settings(),
        opener=_Opener(_Response(body=b"x" * (4 * 1024 * 1024 + 1))),
    )

    with pytest.raises(UpstreamProtocolError, match="exceeded 4 MiB") as caught:
        forwarder.export(ExportTraceServiceRequest(), log_stream="stream")

    assert caught.value.status == 424


def test_forwarder_rejects_unexpected_non_200_response_object() -> None:
    forwarder = GalileoForwarder(
        _settings(),
        opener=_Opener(_Response(status=201, body=b'{"message":"unexpected"}')),
    )

    with pytest.raises(UpstreamError) as caught:
        forwarder.export(ExportTraceServiceRequest(), log_stream="stream")

    assert caught.value.status == 424
    assert str(caught.value) == "Galileo returned HTTP 201"


@pytest.mark.parametrize(
    ("upstream_status", "receiver_status"),
    [
        (302, 424),
        (401, 401),
        (429, 429),
        (500, 500),
        (504, 504),
    ],
)
def test_forwarder_maps_upstream_http_failures_safely(
    upstream_status: int,
    receiver_status: int,
) -> None:
    headers = Message()
    headers["Content-Type"] = "application/json"
    error = urllib.error.HTTPError(
        "http://stub",
        upstream_status,
        "failure",
        headers,
        io.BytesIO(b'{"detail":"stub failure"}'),
    )
    forwarder = GalileoForwarder(_settings(), opener=_Opener(error))

    with pytest.raises(UpstreamError) as caught:
        forwarder.export(ExportTraceServiceRequest(), log_stream="stream")

    assert caught.value.status == receiver_status
    assert f"HTTP {upstream_status}" in str(caught.value)


def test_default_forwarder_disables_redirect_following() -> None:
    forwarder = GalileoForwarder(_settings())

    assert any(
        type(handler).__name__ == "_NoRedirectHandler" for handler in forwarder._opener.handlers
    )


def test_invalid_retry_after_and_error_detail_are_not_reflected() -> None:
    headers = Message()
    headers["Content-Type"] = "application/json"
    headers["Retry-After"] = "not-a-valid-value"
    error = urllib.error.HTTPError(
        "http://stub",
        503,
        "failure",
        headers,
        io.BytesIO(b'{"detail":"Bearer COLLECTOR-SECRET-CANARY"}'),
    )
    forwarder = GalileoForwarder(_settings(), opener=_Opener(error))

    with pytest.raises(UpstreamError) as caught:
        forwarder.export(ExportTraceServiceRequest(), log_stream="stream")

    assert caught.value.retry_after == ""
    assert "COLLECTOR-SECRET-CANARY" not in str(caught.value)

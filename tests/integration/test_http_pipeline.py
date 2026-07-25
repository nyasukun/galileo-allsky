from __future__ import annotations

import gzip
import http.client
import json
import threading
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from conftest import add_attribute, attributes_dict
from google.rpc.status_pb2 import Status
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
    ExportLogsServiceRequest,
    ExportLogsServiceResponse,
)
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)

from allsky_collector.config import AGENTS, Settings
from allsky_collector.privacy import python_to_any_value
from allsky_collector.server import CollectorApplication, make_server


@dataclass
class StubResponse:
    status: int = 200
    body: bytes = b"{}"
    content_type: str = "application/json"
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class CapturedRequest:
    path: str
    headers: dict[str, str]
    body: bytes


class GalileoStubState:
    def __init__(self) -> None:
        self.responses: deque[StubResponse] = deque()
        self.requests: list[CapturedRequest] = []
        self.lock = threading.Lock()

    def enqueue(self, response: StubResponse) -> None:
        with self.lock:
            self.responses.append(response)


class GalileoStubServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, state: GalileoStubState) -> None:
        self.state = state
        super().__init__(("127.0.0.1", 0), GalileoStubHandler)


class GalileoStubHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        state: GalileoStubState = self.server.state  # type: ignore[attr-defined]
        with state.lock:
            state.requests.append(
                CapturedRequest(
                    path=self.path,
                    headers={key.lower(): value for key, value in self.headers.items()},
                    body=body,
                )
            )
            response = state.responses.popleft() if state.responses else StubResponse()
        self.send_response(response.status)
        self.send_header("Content-Type", response.content_type)
        self.send_header("Content-Length", str(len(response.body)))
        for name, value in response.headers.items():
            self.send_header(name, value)
        self.end_headers()
        if response.body:
            self.wfile.write(response.body)


@pytest.fixture
def galileo_stub() -> Iterator[tuple[GalileoStubState, GalileoStubServer]]:
    state = GalileoStubState()
    server = GalileoStubServer(state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state, server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@contextmanager
def running_collector(settings: Settings) -> Iterator[tuple[str, int]]:
    server = make_server(CollectorApplication(settings), port=0)
    assert server.server_address[0] == "127.0.0.1"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def collector_settings(
    stub: GalileoStubServer,
    **overrides: object,
) -> Settings:
    values: dict[str, object] = {
        "api_key": "galileo-secret",
        "project": "allsky-project",
        "endpoint": f"http://127.0.0.1:{stub.server_address[1]}/custom/otel",
        "allow_insecure_upstream": True,
        "pseudonym_secret": "pseudonym-secret",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def one_log(
    *,
    event_name: str = "codex.user_prompt",
    body: str = "",
) -> ExportLogsServiceRequest:
    request = ExportLogsServiceRequest()
    resource_logs = request.resource_logs.add()
    add_attribute(resource_logs.resource.attributes, "service.name", "codex_cli_rs")
    scope = resource_logs.scope_logs.add()
    record = scope.log_records.add()
    record.time_unix_nano = 1_000_000
    record.event_name = event_name
    if body:
        record.body.CopyFrom(python_to_any_value(body))
    add_attribute(record.attributes, "conversation.id", "raw-conversation")
    add_attribute(record.attributes, "user.email", "person@example.com")
    add_attribute(record.attributes, "prompt", "Bearer should-not-leak")
    return request


def post(
    address: tuple[str, int],
    path: str,
    body: bytes,
    *,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection(*address, timeout=3)
    request_headers = {"Content-Type": "application/x-protobuf"}
    if headers:
        request_headers.update(headers)
    connection.request("POST", path, body=body, headers=request_headers)
    response = connection.getresponse()
    payload = response.read()
    response_headers = {key.lower(): value for key, value in response.getheaders()}
    connection.close()
    return response.status, response_headers, payload


def post_chunked(
    address: tuple[str, int],
    path: str,
    chunks: list[bytes],
    *,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection(*address, timeout=3)
    connection.putrequest("POST", path)
    connection.putheader("Content-Type", "application/x-protobuf")
    connection.putheader("Transfer-Encoding", "chunked")
    if headers:
        for name, value in headers.items():
            connection.putheader(name, value)
    connection.endheaders()
    for chunk in chunks:
        connection.send(f"{len(chunk):X}\r\n".encode() + chunk + b"\r\n")
    connection.send(b"0\r\n\r\n")
    response = connection.getresponse()
    payload = response.read()
    response_headers = {key.lower(): value for key, value in response.getheaders()}
    connection.close()
    return response.status, response_headers, payload


def test_logs_pipeline_uses_stub_and_returns_standard_binary_success(
    galileo_stub: tuple[GalileoStubState, GalileoStubServer],
) -> None:
    state, stub = galileo_stub
    settings = collector_settings(stub)
    inbound = one_log()
    scope = inbound.resource_logs[0].scope_logs[0]
    for timestamp, event_name in (
        (2_000_000, "codex.sse_event"),
        (3_000_000, "codex.tool_result"),
    ):
        record = scope.log_records.add()
        record.time_unix_nano = timestamp
        record.event_name = event_name
        add_attribute(record.attributes, "conversation.id", "raw-conversation")

    with running_collector(settings) as address:
        status, headers, body = post(
            address,
            "/v1/logs",
            inbound.SerializeToString(),
            headers={
                "X-Allsky-Agent": "codex-cli",
                "Galileo-API-Key": "spoofed",
                "project": "spoofed-project",
                "logstream": "spoofed-stream",
            },
        )

    response = ExportLogsServiceResponse()
    response.ParseFromString(body)
    assert status == 200
    assert headers["content-type"] == "application/x-protobuf"
    assert not response.HasField("partial_success")
    assert len(state.requests) == 1
    captured = state.requests[0]
    assert captured.path == "/custom/otel"
    assert captured.headers["galileo-api-key"] == "galileo-secret"
    assert captured.headers["project"] == "allsky-project"
    assert captured.headers["logstream"] == "codex-cli"
    assert captured.headers["content-type"] == "application/x-protobuf"

    forwarded = ExportTraceServiceRequest()
    forwarded.ParseFromString(captured.body)
    spans = forwarded.resource_spans[0].scope_spans[0].spans
    span = spans[0]
    attributes = attributes_dict(span.attributes)
    assert span.name == "invoke_agent Codex CLI"
    assert attributes["openinference.span.kind"] == "AGENT"
    assert len(spans) == 3
    assert len({item.trace_id for item in spans}) == 1
    assert span.parent_span_id == b""
    assert {item.parent_span_id for item in spans[1:]} == {span.span_id}
    assert b"spoofed" not in captured.body
    assert b"person@example.com" not in captured.body
    assert b"raw-conversation" not in captured.body
    assert b"should-not-leak" not in captured.body


def test_separate_correlated_batches_use_distinct_traces_and_keep_prompt(
    galileo_stub: tuple[GalileoStubState, GalileoStubServer],
) -> None:
    state, stub = galileo_stub
    settings = collector_settings(stub, capture_content=True)
    conversation_start = one_log(event_name="codex.conversation_starts")
    user_prompt = one_log(event_name="codex.user_prompt")
    prompt_record = user_prompt.resource_logs[0].scope_logs[0].log_records[0]
    for attribute in prompt_record.attributes:
        if attribute.key == "prompt":
            attribute.value.string_value = "visible prompt"

    with running_collector(settings) as address:
        statuses = [
            post(
                address,
                "/v1/logs",
                inbound.SerializeToString(),
                headers={"X-Allsky-Agent": "codex"},
            )[0]
            for inbound in (conversation_start, user_prompt)
        ]

    assert statuses == [200, 200]
    assert len(state.requests) == 2
    forwarded = []
    for captured in state.requests:
        request = ExportTraceServiceRequest()
        request.ParseFromString(captured.body)
        forwarded.append(request.resource_spans[0].scope_spans[0].spans[0])

    start_span, prompt_span = forwarded
    start_attributes = attributes_dict(start_span.attributes)
    prompt_attributes = attributes_dict(prompt_span.attributes)
    assert start_span.trace_id != prompt_span.trace_id
    assert start_span.parent_span_id == prompt_span.parent_span_id == b""
    assert start_attributes["gen_ai.conversation.id"] == prompt_attributes["gen_ai.conversation.id"]
    assert json.loads(prompt_attributes["gen_ai.input.messages"])[0]["content"] == "visible prompt"


def test_all_six_allowlisted_routes_reach_only_the_stub(
    galileo_stub: tuple[GalileoStubState, GalileoStubServer],
) -> None:
    state, stub = galileo_stub

    with running_collector(collector_settings(stub)) as address:
        statuses = [
            post(
                address,
                "/v1/logs",
                one_log().SerializeToString(),
                headers={"X-Allsky-Agent": agent},
            )[0]
            for agent in AGENTS
        ]

    assert statuses == [200] * len(AGENTS)
    assert [request.headers["logstream"] for request in state.requests] == list(AGENTS)


def test_galileo_json_partial_success_is_translated_to_logs_partial(
    galileo_stub: tuple[GalileoStubState, GalileoStubServer],
) -> None:
    state, stub = galileo_stub
    state.enqueue(
        StubResponse(
            body=json.dumps(
                {
                    "partialSuccess": {
                        "rejectedSpans": 1,
                        "errorMessage": "stub rejected one",
                    }
                }
            ).encode()
        )
    )

    with running_collector(collector_settings(stub)) as address:
        status, _, body = post(
            address,
            "/v1/logs",
            one_log().SerializeToString(),
            headers={"X-Allsky-Agent": "codex-cli"},
        )

    response = ExportLogsServiceResponse()
    response.ParseFromString(body)
    assert status == 200
    assert response.partial_success.rejected_log_records == 1
    assert response.partial_success.error_message == "Galileo rejected 1 span(s)"
    assert b"stub rejected one" not in body
    assert len(state.requests) == 1


def test_gzip_request_is_accepted(
    galileo_stub: tuple[GalileoStubState, GalileoStubServer],
) -> None:
    state, stub = galileo_stub
    payload = gzip.compress(one_log().SerializeToString())

    with running_collector(collector_settings(stub)) as address:
        status, _, _ = post(
            address,
            "/v1/logs",
            payload,
            headers={
                "Content-Encoding": "gzip",
                "X-Allsky-Agent": "codex-cli",
            },
        )

    assert status == 200
    assert len(state.requests) == 1


def test_chunked_request_from_older_claude_exporter_is_accepted(
    galileo_stub: tuple[GalileoStubState, GalileoStubServer],
) -> None:
    state, stub = galileo_stub
    payload = one_log().SerializeToString()
    midpoint = len(payload) // 2

    with running_collector(collector_settings(stub)) as address:
        status, _, _ = post_chunked(
            address,
            "/v1/logs",
            [payload[:midpoint], payload[midpoint:]],
            headers={"X-Allsky-Agent": "claude-code-cli"},
        )

    assert status == 200
    assert len(state.requests) == 1


def test_trace_pipeline_returns_trace_response_and_preserves_ids(
    galileo_stub: tuple[GalileoStubState, GalileoStubServer],
) -> None:
    state, stub = galileo_stub
    request = ExportTraceServiceRequest()
    scope = request.resource_spans.add().scope_spans.add()
    span = scope.spans.add()
    span.trace_id = b"\x11" * 16
    span.span_id = b"\x22" * 8
    span.name = "claude_code.llm_request"
    span.start_time_unix_nano = 100
    span.end_time_unix_nano = 200
    add_attribute(span.attributes, "gen_ai.request.model", "claude-test")

    with running_collector(collector_settings(stub)) as address:
        status, _, body = post(
            address,
            "/v1/traces",
            request.SerializeToString(),
            headers={"X-Allsky-Agent": "claude-code-cli"},
        )

    response = ExportTraceServiceResponse()
    response.ParseFromString(body)
    assert status == 200
    assert not response.HasField("partial_success")
    forwarded = ExportTraceServiceRequest()
    forwarded.ParseFromString(state.requests[0].body)
    output = forwarded.resource_spans[0].scope_spans[0].spans[0]
    assert output.trace_id == span.trace_id
    assert output.span_id == span.span_id
    assert output.name == "chat claude-test"


def test_codex_native_traces_are_acked_without_forwarding(
    galileo_stub: tuple[GalileoStubState, GalileoStubServer],
) -> None:
    state, stub = galileo_stub
    request = ExportTraceServiceRequest()
    span = request.resource_spans.add().scope_spans.add().spans.add()
    span.trace_id = b"\x11" * 16
    span.span_id = b"\x22" * 8
    span.name = "codex.internal"

    with running_collector(collector_settings(stub)) as address:
        status, _, body = post(
            address,
            "/v1/traces",
            request.SerializeToString(),
            headers={"X-Allsky-Agent": "codex"},
        )

    response = ExportTraceServiceResponse()
    response.ParseFromString(body)
    assert status == 200
    assert not response.HasField("partial_success")
    assert state.requests == []


def test_uncorrelated_codex_log_trace_is_acked_without_forwarding(
    galileo_stub: tuple[GalileoStubState, GalileoStubServer],
) -> None:
    state, stub = galileo_stub
    request = one_log(event_name="codex.sse_event")
    record = request.resource_logs[0].scope_logs[0].log_records[0]
    del record.attributes[:]
    record.trace_id = b"\xaa" * 16

    with running_collector(collector_settings(stub)) as address:
        status, _, body = post(
            address,
            "/v1/logs",
            request.SerializeToString(),
            headers={"X-Allsky-Agent": "codex"},
        )

    response = ExportLogsServiceResponse()
    response.ParseFromString(body)
    assert status == 200
    assert not response.HasField("partial_success")
    assert state.requests == []


def test_empty_envelope_succeeds_without_agent_or_upstream(
    galileo_stub: tuple[GalileoStubState, GalileoStubServer],
) -> None:
    state, stub = galileo_stub

    with running_collector(collector_settings(stub)) as address:
        status, _, body = post(
            address,
            "/v1/logs",
            ExportLogsServiceRequest().SerializeToString(),
        )

    response = ExportLogsServiceResponse()
    response.ParseFromString(body)
    assert status == 200
    assert not response.HasField("partial_success")
    assert state.requests == []


@pytest.mark.parametrize(
    ("content_type", "payload", "expected"),
    [
        ("text/plain", b"bad", 415),
        ("application/x-protobuf", b"\x80", 400),
    ],
)
def test_invalid_input_is_rejected_without_upstream(
    galileo_stub: tuple[GalileoStubState, GalileoStubServer],
    content_type: str,
    payload: bytes,
    expected: int,
) -> None:
    state, stub = galileo_stub

    with running_collector(collector_settings(stub)) as address:
        status, headers, body = post(
            address,
            "/v1/logs",
            payload,
            headers={
                "Content-Type": content_type,
                "X-Allsky-Agent": "codex-cli",
            },
        )

    error = Status()
    error.ParseFromString(body)
    assert status == expected
    assert headers["content-type"] == "application/x-protobuf"
    assert headers["connection"] == "close"
    assert error.message
    assert state.requests == []


def test_unknown_agent_is_rejected_without_upstream(
    galileo_stub: tuple[GalileoStubState, GalileoStubServer],
) -> None:
    state, stub = galileo_stub

    with running_collector(collector_settings(stub)) as address:
        status, _, _ = post(
            address,
            "/v1/logs",
            one_log().SerializeToString(),
            headers={"X-Allsky-Agent": "attacker-stream"},
        )

    assert status == 400
    assert state.requests == []


def test_expanded_gzip_limit_is_enforced(
    galileo_stub: tuple[GalileoStubState, GalileoStubServer],
) -> None:
    state, stub = galileo_stub
    payload = gzip.compress(one_log(body="A" * 5000).SerializeToString())
    settings = collector_settings(stub, max_request_bytes=1024)

    with running_collector(settings) as address:
        status, _, _ = post(
            address,
            "/v1/logs",
            payload,
            headers={
                "Content-Encoding": "gzip",
                "X-Allsky-Agent": "codex-cli",
            },
        )

    assert status == 413
    assert state.requests == []


def test_item_count_limit_is_enforced_before_forwarding(
    galileo_stub: tuple[GalileoStubState, GalileoStubServer],
) -> None:
    state, stub = galileo_stub
    request = one_log()
    request.resource_logs[0].scope_logs[0].log_records.add().event_name = "codex.sse_event"

    with running_collector(collector_settings(stub, max_items_per_request=1)) as address:
        status, _, body = post(
            address,
            "/v1/logs",
            request.SerializeToString(),
            headers={"X-Allsky-Agent": "codex-cli"},
        )

    error = Status()
    error.ParseFromString(body)
    assert status == 413
    assert error.code == 8
    assert state.requests == []


def test_normalized_output_limit_is_enforced_before_forwarding(
    galileo_stub: tuple[GalileoStubState, GalileoStubServer],
) -> None:
    state, stub = galileo_stub

    with running_collector(collector_settings(stub, max_output_bytes=1024)) as address:
        status, _, body = post(
            address,
            "/v1/logs",
            one_log().SerializeToString(),
            headers={"X-Allsky-Agent": "codex-cli"},
        )

    error = Status()
    error.ParseFromString(body)
    assert status == 413
    assert error.code == 8
    assert state.requests == []


def test_retryable_upstream_status_is_propagated_once_with_retry_after(
    galileo_stub: tuple[GalileoStubState, GalileoStubServer],
) -> None:
    state, stub = galileo_stub
    state.enqueue(
        StubResponse(
            status=503,
            body=b'{"detail":"temporarily unavailable"}',
            headers={"Retry-After": "2"},
        )
    )

    with running_collector(collector_settings(stub)) as address:
        status, headers, body = post(
            address,
            "/v1/logs",
            one_log().SerializeToString(),
            headers={"X-Allsky-Agent": "codex-cli"},
        )

    error = Status()
    error.ParseFromString(body)
    assert status == 503
    assert headers["retry-after"] == "2"
    assert error.code == 14
    assert len(state.requests) == 1


def test_authorization_header_is_ignored_on_loopback(
    galileo_stub: tuple[GalileoStubState, GalileoStubServer],
) -> None:
    state, stub = galileo_stub

    with running_collector(collector_settings(stub)) as address:
        status, _, _ = post(
            address,
            "/v1/logs",
            one_log().SerializeToString(),
            headers={
                "X-Allsky-Agent": "codex-cli",
                "Authorization": "Bearer arbitrary-agent-value",
            },
        )

    assert status == 200
    assert len(state.requests) == 1
    assert "authorization" not in state.requests[0].headers


def test_health_and_status_never_include_galileo_credentials(
    galileo_stub: tuple[GalileoStubState, GalileoStubServer],
) -> None:
    _, stub = galileo_stub

    with running_collector(collector_settings(stub)) as address:
        connection = http.client.HTTPConnection(*address, timeout=3)
        connection.request("GET", "/status")
        response = connection.getresponse()
        body = response.read()
        connection.close()

    assert response.status == 200
    assert b"galileo-secret" not in body
    assert b"receiver_auth" not in body


def test_health_and_unknown_get_endpoints(
    galileo_stub: tuple[GalileoStubState, GalileoStubServer],
) -> None:
    _, stub = galileo_stub

    with running_collector(collector_settings(stub)) as address:
        connection = http.client.HTTPConnection(*address, timeout=3)
        connection.request("GET", "/healthz")
        healthy = connection.getresponse()
        health_body = healthy.read()
        connection.request("GET", "/missing")
        missing = connection.getresponse()
        missing_body = missing.read()
        connection.close()

    error = Status()
    error.ParseFromString(missing_body)
    assert healthy.status == 200
    assert json.loads(health_body) == {"status": "ok"}
    assert missing.status == 404
    assert error.message == "not found"


@pytest.mark.parametrize(
    ("encoding", "payload", "expected"),
    [
        ("br", b"not-brotli", 415),
        ("gzip", b"not-gzip", 400),
    ],
)
def test_invalid_content_encoding_is_rejected(
    galileo_stub: tuple[GalileoStubState, GalileoStubServer],
    encoding: str,
    payload: bytes,
    expected: int,
) -> None:
    state, stub = galileo_stub

    with running_collector(collector_settings(stub)) as address:
        status, _, _ = post(
            address,
            "/v1/logs",
            payload,
            headers={
                "Content-Encoding": encoding,
                "X-Allsky-Agent": "codex-cli",
            },
        )

    assert status == expected
    assert state.requests == []

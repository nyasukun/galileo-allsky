"""OTLP/HTTP receiver and health endpoints."""

from __future__ import annotations

import gzip
import io
import json
import logging
import threading
import time
from collections import Counter
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, BinaryIO
from urllib.parse import urlsplit

from google.protobuf.message import DecodeError
from google.rpc.status_pb2 import Status
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
    ExportLogsServiceRequest,
    ExportLogsServiceResponse,
)
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)

from .aggregator import ConversationAggregator, ReleasedTurn, sweeper
from .config import LISTEN_HOST, Settings
from .forwarder import GalileoForwarder, UpstreamError
from .transform import (
    RequestLimitError,
    TransformError,
    count_logs,
    count_spans,
    logs_to_traces,
    normalize_traces,
    resolve_agent,
)

logger = logging.getLogger(__name__)

GRPC_INVALID_ARGUMENT = 3
GRPC_RESOURCE_EXHAUSTED = 8
GRPC_INTERNAL = 13
GRPC_UNAVAILABLE = 14
GRPC_UNAUTHENTICATED = 16

_LOG_PRIMARY_AGENTS = frozenset({"codex", "codex-cli"})


@dataclass(frozen=True)
class ProcessedResponse:
    body: bytes
    input_items: int
    output_spans: int
    rejected_items: int


class CollectorStats:
    """Small in-memory diagnostics with no telemetry content."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._started_at = time.time()
        self._counters: Counter[str] = Counter()
        self._last_success_at: float | None = None
        self._last_error_at: float | None = None
        self._last_error_type = ""

    def increment(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._counters[name] += amount

    def record_diagnostics(self, agent: str, diagnostics: dict[str, int] | None) -> None:
        """Record each transform diagnostic globally and per source.

        Comparing one agent's trace shape against another needs the per-agent
        split; the unscoped names stay for existing operational checks.
        """

        if not diagnostics:
            return
        with self._lock:
            for name, amount in diagnostics.items():
                self._counters[name] += amount
                self._counters[f"agent.{agent}.{name}"] += amount

    def success(self) -> None:
        with self._lock:
            self._last_success_at = time.time()

    def error(self, error_type: str) -> None:
        with self._lock:
            self._last_error_at = time.time()
            self._last_error_type = error_type
            self._counters["errors"] += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "status": "ready",
                "uptime_seconds": max(0, int(time.time() - self._started_at)),
                "counters": dict(self._counters),
                "last_success_at": self._last_success_at,
                "last_error_at": self._last_error_at,
                "last_error_type": self._last_error_type,
            }


class CollectorApplication:
    def __init__(
        self,
        settings: Settings,
        *,
        forwarder: GalileoForwarder | Any | None = None,
        aggregator: ConversationAggregator | None = None,
    ) -> None:
        self.settings = settings
        self.forwarder = forwarder or GalileoForwarder(settings)
        self.stats = CollectorStats()
        if aggregator is not None:
            self.aggregator: ConversationAggregator | None = aggregator
        elif settings.aggregate_turns:
            self.aggregator = ConversationAggregator(
                idle_seconds=settings.turn_idle_seconds,
                max_turn_records=settings.max_turn_records,
                max_turn_bytes=settings.max_output_bytes,
                max_total_records=settings.max_buffered_records,
            )
        else:
            self.aggregator = None

    def deliver(self, turn: ReleasedTurn) -> None:
        """Transform and forward one released turn.

        The agent's request already returned, so a failure here can only be
        recorded. Nothing retries it: the collector has no durable spool.
        """

        self.stats.increment("turns_released")
        self.stats.increment(f"turns_released.{turn.reason}")
        self.stats.increment(f"agent.{turn.agent}.turns_released")
        try:
            transformed = logs_to_traces(
                turn.request,
                agent=turn.agent,
                settings=self.settings,
            )
            if transformed.output_spans == 0:
                self.stats.record_diagnostics(turn.agent, transformed.diagnostics)
                return
            if transformed.request.ByteSize() > self.settings.max_output_bytes:
                self.stats.error("turn_too_large")
                self.stats.increment("turns_dropped")
                return
            upstream = self.forwarder.export(
                transformed.request,
                log_stream=self.settings.routes[turn.agent],
            )
            rejected = max(0, upstream.rejected_spans)
            self.stats.increment("logs_received", turn.records)
            self.stats.increment("spans_forwarded", transformed.output_spans - rejected)
            self.stats.increment(
                f"agent.{turn.agent}.spans_forwarded",
                transformed.output_spans - rejected,
            )
            self.stats.increment("items_rejected", rejected)
            self.stats.record_diagnostics(turn.agent, transformed.diagnostics)
            self.stats.success()
        except UpstreamError as exc:
            logger.warning("released turn was not delivered: HTTP %s", exc.status)
            self.stats.error("deferred_upstream")
            self.stats.increment("turns_dropped")
        except Exception:
            logger.exception("released turn failed to transform")
            self.stats.error("deferred_internal")
            self.stats.increment("turns_dropped")

    def process(
        self,
        *,
        signal: str,
        payload: bytes,
        agent_header: str,
    ) -> ProcessedResponse:
        if signal == "traces":
            inbound: Any = ExportTraceServiceRequest()
        elif signal == "logs":
            inbound = ExportLogsServiceRequest()
        else:
            raise ValueError(f"unsupported signal: {signal}")

        try:
            inbound.ParseFromString(payload)
        except DecodeError as exc:
            raise TransformError(f"malformed OTLP {signal} protobuf") from exc

        input_items = count_spans(inbound) if signal == "traces" else count_logs(inbound)
        if input_items > self.settings.max_items_per_request:
            raise RequestLimitError("OTLP item count exceeds ALLSKY_MAX_ITEMS_PER_REQUEST")
        if input_items == 0:
            response: Any = (
                ExportTraceServiceResponse() if signal == "traces" else ExportLogsServiceResponse()
            )
            self.stats.increment("empty_requests")
            return ProcessedResponse(
                body=response.SerializeToString(),
                input_items=0,
                output_spans=0,
                rejected_items=0,
            )

        agent = resolve_agent(inbound, agent_header)
        if signal == "traces" and agent in _LOG_PRIMARY_AGENTS:
            outbound_response = ExportTraceServiceResponse()
            self.stats.increment("requests")
            self.stats.increment("traces_received", input_items)
            self.stats.increment("traces_suppressed", input_items)
            self.stats.increment(f"agent.{agent}.requests")
            self.stats.increment(f"agent.{agent}.traces_suppressed", input_items)
            self.stats.success()
            return ProcessedResponse(
                body=outbound_response.SerializeToString(),
                input_items=input_items,
                output_spans=0,
                rejected_items=0,
            )

        if signal == "logs" and agent in _LOG_PRIMARY_AGENTS and self.aggregator is not None:
            # Codex exports about one record per request. Hold the turn so its
            # spans reach Galileo together, in the single request a trace gets.
            for released in self.aggregator.add(agent, inbound):
                self.deliver(released)
            self.stats.increment("requests")
            self.stats.increment(f"agent.{agent}.requests")
            self.stats.increment("logs_buffered", input_items)
            return ProcessedResponse(
                body=ExportLogsServiceResponse().SerializeToString(),
                input_items=input_items,
                output_spans=0,
                rejected_items=0,
            )

        transformed = (
            normalize_traces(inbound, agent=agent, settings=self.settings)
            if signal == "traces"
            else logs_to_traces(inbound, agent=agent, settings=self.settings)
        )
        if transformed.output_spans == 0:
            outbound_response = (
                ExportTraceServiceResponse() if signal == "traces" else ExportLogsServiceResponse()
            )
            self.stats.increment("requests")
            self.stats.increment(f"{signal}_received", input_items)
            self.stats.increment(f"agent.{agent}.requests")
            self.stats.record_diagnostics(agent, transformed.diagnostics)
            self.stats.success()
            return ProcessedResponse(
                body=outbound_response.SerializeToString(),
                input_items=input_items,
                output_spans=0,
                rejected_items=0,
            )
        if transformed.request.ByteSize() > self.settings.max_output_bytes:
            raise RequestLimitError("normalized trace batch exceeds ALLSKY_MAX_OUTPUT_BYTES")
        upstream = self.forwarder.export(
            transformed.request,
            log_stream=self.settings.routes[agent],
        )
        rejected = min(input_items, max(0, upstream.rejected_spans))

        if signal == "traces":
            outbound_response: Any = ExportTraceServiceResponse()
            if upstream.partial_success_present:
                outbound_response.partial_success.rejected_spans = rejected
                outbound_response.partial_success.error_message = upstream.error_message
        else:
            outbound_response = ExportLogsServiceResponse()
            if upstream.partial_success_present:
                outbound_response.partial_success.rejected_log_records = rejected
                outbound_response.partial_success.error_message = upstream.error_message

        self.stats.increment("requests")
        self.stats.increment(f"{signal}_received", input_items)
        self.stats.increment("spans_forwarded", transformed.output_spans - rejected)
        self.stats.increment("items_rejected", rejected)
        self.stats.increment(f"agent.{agent}.requests")
        self.stats.increment(f"agent.{agent}.spans_forwarded", transformed.output_spans - rejected)
        self.stats.record_diagnostics(agent, transformed.diagnostics)
        self.stats.success()
        return ProcessedResponse(
            body=outbound_response.SerializeToString(),
            input_items=input_items,
            output_spans=transformed.output_spans,
            rejected_items=rejected,
        )


class CollectorHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        application: CollectorApplication,
        *,
        port: int,
    ) -> None:
        self.application = application
        self._stop_sweeper = threading.Event()
        self._sweeper: threading.Thread | None = None
        super().__init__((LISTEN_HOST, port), CollectorRequestHandler)

    def server_activate(self) -> None:
        super().server_activate()
        aggregator = self.application.aggregator
        if aggregator is None:
            return
        # Not a daemon: shutdown has to drain held turns before the process exits.
        self._sweeper = threading.Thread(
            target=sweeper(
                aggregator,
                self.application.deliver,
                interval_seconds=min(5.0, self.application.settings.turn_idle_seconds),
                stop=self._stop_sweeper,
            ),
            name="allsky-turn-sweeper",
        )
        self._sweeper.start()

    def server_close(self) -> None:
        self._stop_sweeper.set()
        if self._sweeper is not None:
            self._sweeper.join(timeout=30)
            self._sweeper = None
        super().server_close()


def _decompress_gzip(payload: bytes, maximum: int) -> bytes:
    with gzip.GzipFile(fileobj=io.BytesIO(payload), mode="rb") as compressed:
        expanded = compressed.read(maximum + 1)
    if len(expanded) > maximum:
        raise OverflowError("expanded request exceeds configured limit")
    return expanded


def _read_chunked(stream: BinaryIO, maximum: int) -> bytes:
    payload = bytearray()
    while True:
        size_line = stream.readline(8193)
        if not size_line or len(size_line) > 8192 or not size_line.endswith(b"\r\n"):
            raise ValueError("malformed chunk size line")
        size_text = size_line[:-2].split(b";", 1)[0].strip()
        if not size_text or any(
            character not in b"0123456789abcdefABCDEF" for character in size_text
        ):
            raise ValueError("malformed chunk size")
        size = int(size_text, 16)
        if size == 0:
            trailer_bytes = 0
            while True:
                trailer = stream.readline(8193)
                trailer_bytes += len(trailer)
                if (
                    not trailer
                    or len(trailer) > 8192
                    or trailer_bytes > 8192
                    or not trailer.endswith(b"\r\n")
                ):
                    raise ValueError("malformed chunk trailer")
                if trailer == b"\r\n":
                    return bytes(payload)
        if len(payload) + size > maximum:
            raise OverflowError("chunked request exceeds configured limit")
        chunk = stream.read(size)
        if len(chunk) != size or stream.read(2) != b"\r\n":
            raise ValueError("incomplete chunked request")
        payload.extend(chunk)


class CollectorRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "galileo-allsky/0.1"
    sys_version = ""

    @property
    def application(self) -> CollectorApplication:
        return self.server.application  # type: ignore[attr-defined,no-any-return]

    def log_message(self, format: str, *args: object) -> None:
        logger.debug("HTTP request handled for %s", self.client_address[0])

    def _send_bytes(
        self,
        status: int,
        body: bytes,
        *,
        content_type: str,
        extra_headers: dict[str, str] | None = None,
        close_connection: bool = False,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if close_connection:
            self.send_header("Connection", "close")
            self.close_connection = True
        if extra_headers:
            for name, value in extra_headers.items():
                if value:
                    self.send_header(name, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _send_status(
        self,
        http_status: int,
        grpc_code: int,
        message: str,
        *,
        retry_after: str = "",
        close_connection: bool = True,
    ) -> None:
        body = Status(code=grpc_code, message=message[:1024]).SerializeToString()
        headers = {"Retry-After": retry_after} if retry_after else None
        self._send_bytes(
            http_status,
            body,
            content_type="application/x-protobuf",
            extra_headers=headers,
            close_connection=close_connection,
        )

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        self._send_bytes(status, body, content_type="application/json")

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/healthz":
            self._json(200, {"status": "ok"})
        elif path in {"/readyz", "/status"}:
            self._json(200, self.application.stats.snapshot())
        else:
            self._send_status(404, GRPC_INVALID_ARGUMENT, "not found")

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        signal = {
            "/v1/traces": "traces",
            "/v1/logs": "logs",
        }.get(path)
        if signal is None:
            self._send_status(404, GRPC_INVALID_ARGUMENT, "unknown OTLP endpoint")
            return

        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/x-protobuf":
            self.application.stats.error("unsupported_media_type")
            self._send_status(
                415,
                GRPC_INVALID_ARGUMENT,
                "Content-Type must be application/x-protobuf",
            )
            return

        maximum = self.application.settings.max_request_bytes
        raw_length = self.headers.get("Content-Length")
        transfer_encoding = self.headers.get("Transfer-Encoding", "").strip().lower()
        if transfer_encoding:
            if raw_length is not None:
                self._send_status(
                    400,
                    GRPC_INVALID_ARGUMENT,
                    "Content-Length and Transfer-Encoding cannot be combined",
                )
                return
            if transfer_encoding != "chunked":
                self._send_status(
                    415,
                    GRPC_INVALID_ARGUMENT,
                    "Transfer-Encoding must be chunked or omitted",
                )
                return
            try:
                payload = _read_chunked(self.rfile, maximum)
            except ValueError:
                self._send_status(400, GRPC_INVALID_ARGUMENT, "malformed chunked request")
                return
            except OverflowError:
                self.application.stats.error("request_too_large")
                self._send_status(
                    413,
                    GRPC_RESOURCE_EXHAUSTED,
                    "chunked OTLP request exceeds configured limit",
                )
                return
        else:
            if raw_length is None:
                self._send_status(
                    411,
                    GRPC_INVALID_ARGUMENT,
                    "Content-Length or chunked Transfer-Encoding is required",
                )
                return
            try:
                content_length = int(raw_length)
            except ValueError:
                self._send_status(400, GRPC_INVALID_ARGUMENT, "invalid Content-Length")
                return
            if content_length < 0 or content_length > maximum:
                self.application.stats.error("request_too_large")
                self._send_status(
                    413,
                    GRPC_RESOURCE_EXHAUSTED,
                    "OTLP request exceeds configured limit",
                )
                return
            payload = self.rfile.read(content_length)
            if len(payload) != content_length:
                self._send_status(400, GRPC_INVALID_ARGUMENT, "incomplete request body")
                return

        content_encoding = self.headers.get("Content-Encoding", "").strip().lower()
        if content_encoding:
            if content_encoding != "gzip":
                self._send_status(
                    415,
                    GRPC_INVALID_ARGUMENT,
                    "Content-Encoding must be gzip or omitted",
                )
                return
            try:
                payload = _decompress_gzip(payload, maximum)
            except (gzip.BadGzipFile, EOFError, OSError):
                self._send_status(400, GRPC_INVALID_ARGUMENT, "malformed gzip request")
                return
            except OverflowError:
                self.application.stats.error("expanded_request_too_large")
                self._send_status(
                    413,
                    GRPC_RESOURCE_EXHAUSTED,
                    "expanded OTLP request exceeds configured limit",
                )
                return

        try:
            response = self.application.process(
                signal=signal,
                payload=payload,
                agent_header=self.headers.get("X-Allsky-Agent", ""),
            )
        except RequestLimitError as exc:
            self.application.stats.error("processing_limit")
            self._send_status(413, GRPC_RESOURCE_EXHAUSTED, str(exc))
            return
        except TransformError as exc:
            self.application.stats.error("invalid_otlp")
            self._send_status(400, GRPC_INVALID_ARGUMENT, str(exc))
            return
        except UpstreamError as exc:
            self.application.stats.error("upstream")
            grpc_code = (
                GRPC_UNAUTHENTICATED
                if exc.status in {401, 403}
                else GRPC_UNAVAILABLE
                if exc.status in {429, 502, 503, 504}
                else GRPC_INTERNAL
            )
            self._send_status(
                exc.status,
                grpc_code,
                str(exc),
                retry_after=exc.retry_after,
            )
            return
        except Exception:
            logger.exception("collector request failed")
            self.application.stats.error("internal")
            self._send_status(500, GRPC_INTERNAL, "collector internal error")
            return

        self._send_bytes(
            200,
            response.body,
            content_type="application/x-protobuf",
        )


def make_server(
    application: CollectorApplication,
    *,
    port: int | None = None,
) -> CollectorHTTPServer:
    settings = application.settings
    return CollectorHTTPServer(
        application,
        port=settings.port if port is None else port,
    )

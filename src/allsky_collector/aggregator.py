"""Group log records by conversation so one trace ships in one OTLP request.

Galileo's OTLP endpoint treats a trace as single-shot and immutable: a later
request that reuses a trace ID is rejected with HTTP 422, and one that adds an
orphan child answers 200 while silently discarding the span. Every span of a
trace therefore has to arrive together.

Agents do not cooperate with that. Codex exports roughly one log record per
OTLP request, so each record used to become its own single-span trace. This
module holds records keyed by conversation and releases a whole turn at once,
which also lets a record that carries no conversation ID of its own inherit the
identity of the turn it arrived in.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
    ExportLogsServiceRequest,
)
from opentelemetry.proto.logs.v1.logs_pb2 import LogRecord, ResourceLogs, ScopeLogs
from opentelemetry.proto.resource.v1.resource_pb2 import Resource

from .privacy import any_value_to_python
from .transform import (
    correlation_identity_of,
    event_name_of,
    python_attribute_map,
)

# A turn starts here, so anything still buffered for the conversation belongs to
# the previous turn and is released before the new one begins.
TURN_START_EVENTS = frozenset(
    {
        "codex.conversation_starts",
        "codex.user_prompt",
        "claude_code.user_prompt",
        "user_prompt",
    }
)


@dataclass
class _Turn:
    """One conversation turn held until a trigger releases it."""

    agent: str
    resource: Resource
    scope: Any
    records: list[LogRecord] = field(default_factory=list)
    first_seen_ns: int = 0
    last_seen_ns: int = 0
    bytes_held: int = 0

    def add(self, record: LogRecord, now_ns: int) -> None:
        if not self.records:
            self.first_seen_ns = now_ns
        self.last_seen_ns = now_ns
        self.bytes_held += record.ByteSize()
        self.records.append(record)

    def to_request(self) -> ExportLogsServiceRequest:
        request = ExportLogsServiceRequest()
        resource_logs = request.resource_logs.add()
        resource_logs.resource.CopyFrom(self.resource)
        scope_logs = resource_logs.scope_logs.add()
        scope_logs.scope.CopyFrom(self.scope)
        for record in self.records:
            scope_logs.log_records.add().CopyFrom(record)
        return request


@dataclass(frozen=True)
class ReleasedTurn:
    agent: str
    request: ExportLogsServiceRequest
    reason: str
    records: int


class ConversationAggregator:
    """Hold log records per conversation and release complete turns.

    All public methods are safe to call from the receiver's request threads and
    from the background sweeper at the same time.
    """

    def __init__(
        self,
        *,
        idle_seconds: float,
        max_turn_records: int,
        max_turn_bytes: int,
        max_total_records: int,
        clock: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        self._idle_ns = int(idle_seconds * 1_000_000_000)
        self._max_turn_records = max_turn_records
        self._max_turn_bytes = max_turn_bytes
        self._max_total_records = max_total_records
        self._clock = clock
        self._lock = threading.Lock()
        self._turns: dict[tuple[str, str], _Turn] = {}
        self._recent_identity: dict[str, tuple[str, Any]] = {}
        self._held_records = 0

    def held_records(self) -> int:
        with self._lock:
            return self._held_records

    def add(self, agent: str, request: ExportLogsServiceRequest) -> list[ReleasedTurn]:
        """Buffer every record in the request, returning turns ready to ship."""

        released: list[ReleasedTurn] = []
        now = self._clock()
        with self._lock:
            for resource_logs in request.resource_logs:
                for scope_logs in resource_logs.scope_logs:
                    scope_identity = _single_scope_identity(scope_logs.log_records)
                    for record in scope_logs.log_records:
                        released.extend(
                            self._add_locked(
                                agent,
                                resource_logs,
                                scope_logs,
                                record,
                                scope_identity,
                                now,
                            )
                        )
            released.extend(self._release_over_capacity_locked())
        return released

    def due(self) -> list[ReleasedTurn]:
        """Release turns that have gone quiet for longer than the idle window."""

        now = self._clock()
        with self._lock:
            expired = [
                key for key, turn in self._turns.items() if now - turn.last_seen_ns >= self._idle_ns
            ]
            return [self._release_locked(key, "idle") for key in expired]

    def drain(self) -> list[ReleasedTurn]:
        """Release everything, for shutdown."""

        with self._lock:
            return [self._release_locked(key, "drain") for key in list(self._turns)]

    def _add_locked(
        self,
        agent: str,
        resource_logs: ResourceLogs,
        scope_logs: ScopeLogs,
        record: LogRecord,
        scope_identity: tuple[str, Any] | None,
        now: int,
    ) -> list[ReleasedTurn]:
        attributes = python_attribute_map(record.attributes)
        identity = (
            correlation_identity_of(attributes)
            or scope_identity
            or self._recent_identity.get(agent)
        )
        key = (agent, _identity_key(identity))
        if identity is not None:
            self._recent_identity[agent] = identity

        released: list[ReleasedTurn] = []
        event_name = event_name_of(
            attributes, any_value_to_python(record.body), agent, record.event_name
        )
        existing = self._turns.get(key)
        if existing is not None and event_name in TURN_START_EVENTS:
            released.append(self._release_locked(key, "turn_start"))
            existing = None

        if existing is None:
            existing = _Turn(agent=agent, resource=resource_logs.resource, scope=scope_logs.scope)
            self._turns[key] = existing

        existing.add(record, now)
        self._held_records += 1

        if (
            len(existing.records) >= self._max_turn_records
            or existing.bytes_held >= self._max_turn_bytes
        ):
            released.append(self._release_locked(key, "turn_full"))
        return released

    def _release_over_capacity_locked(self) -> list[ReleasedTurn]:
        released: list[ReleasedTurn] = []
        while self._held_records > self._max_total_records and self._turns:
            oldest = min(self._turns, key=lambda key: self._turns[key].first_seen_ns)
            released.append(self._release_locked(oldest, "capacity"))
        return released

    def _release_locked(self, key: tuple[str, str], reason: str) -> ReleasedTurn:
        turn = self._turns.pop(key)
        self._held_records -= len(turn.records)
        return ReleasedTurn(
            agent=turn.agent,
            request=turn.to_request(),
            reason=reason,
            records=len(turn.records),
        )


def _identity_key(identity: tuple[str, Any] | None) -> str:
    if identity is None:
        return "\x00unidentified"
    namespace, value = identity
    return f"{namespace}\x00{value}"


def _single_scope_identity(records: Sequence[LogRecord]) -> tuple[str, Any] | None:
    """Return the one conversation identity in this scope, when unambiguous."""

    found: set[tuple[str, Any]] = set()
    for record in records:
        identity = correlation_identity_of(python_attribute_map(record.attributes))
        if identity is not None:
            found.add((identity[0], str(identity[1])))
        if len(found) > 1:
            return None
    return next(iter(found)) if len(found) == 1 else None


def sweeper(
    aggregator: ConversationAggregator,
    deliver: Callable[[ReleasedTurn], None],
    *,
    interval_seconds: float,
    stop: threading.Event,
) -> Callable[[], None]:
    """Return a loop that releases idle turns until `stop` is set."""

    def run() -> None:
        while not stop.wait(interval_seconds):
            for turn in aggregator.due():
                deliver(turn)
        for turn in aggregator.drain():
            deliver(turn)

    return run


def iter_records(request: ExportLogsServiceRequest) -> Iterable[LogRecord]:
    for resource_logs in request.resource_logs:
        for scope_logs in resource_logs.scope_logs:
            yield from scope_logs.log_records

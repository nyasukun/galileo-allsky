from __future__ import annotations

import threading

from conftest import add_attribute
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
    ExportLogsServiceRequest,
)

from allsky_collector.aggregator import (
    ConversationAggregator,
    ReleasedTurn,
    iter_records,
    sweeper,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_000_000_000

    def __call__(self) -> int:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += int(seconds * 1_000_000_000)


def make_aggregator(clock: FakeClock, **overrides: object) -> ConversationAggregator:
    settings: dict[str, object] = {
        "idle_seconds": 10.0,
        "max_turn_records": 500,
        "max_turn_bytes": 4 * 1024 * 1024,
        "max_total_records": 10_000,
    }
    settings.update(overrides)
    return ConversationAggregator(clock=clock, **settings)  # type: ignore[arg-type]


def one_request(*records: tuple[str, str | None]) -> ExportLogsServiceRequest:
    request = ExportLogsServiceRequest()
    scope_logs = request.resource_logs.add().scope_logs.add()
    for event_name, conversation in records:
        record = scope_logs.log_records.add()
        record.event_name = event_name
        if conversation is not None:
            add_attribute(record.attributes, "conversation.id", conversation)
    return request


def event_names(turn: ReleasedTurn) -> list[str]:
    return [record.event_name for record in iter_records(turn.request)]


def test_records_from_separate_requests_join_one_turn() -> None:
    clock = FakeClock()
    aggregator = make_aggregator(clock)

    assert aggregator.add("codex", one_request(("codex.user_prompt", "c1"))) == []
    assert aggregator.add("codex", one_request(("codex.sse_event", "c1"))) == []
    assert aggregator.add("codex", one_request(("codex.tool_result", "c1"))) == []
    assert aggregator.held_records() == 3

    clock.advance(11)
    released = aggregator.due()

    assert len(released) == 1
    assert released[0].reason == "idle"
    assert event_names(released[0]) == [
        "codex.user_prompt",
        "codex.sse_event",
        "codex.tool_result",
    ]
    assert aggregator.held_records() == 0


def test_a_new_user_prompt_closes_the_previous_turn() -> None:
    clock = FakeClock()
    aggregator = make_aggregator(clock)

    aggregator.add("codex", one_request(("codex.user_prompt", "c1")))
    aggregator.add("codex", one_request(("codex.sse_event", "c1")))
    released = aggregator.add("codex", one_request(("codex.user_prompt", "c1")))

    assert len(released) == 1
    assert released[0].reason == "turn_start"
    assert event_names(released[0]) == ["codex.user_prompt", "codex.sse_event"]
    assert aggregator.held_records() == 1


def test_a_record_without_a_conversation_id_joins_the_turn_in_flight() -> None:
    """Codex sends tool results in their own request, carrying no conversation ID."""

    clock = FakeClock()
    aggregator = make_aggregator(clock)

    aggregator.add("codex", one_request(("codex.user_prompt", "c1")))
    aggregator.add("codex", one_request(("codex.tool_result", None)))

    clock.advance(11)
    released = aggregator.due()

    assert len(released) == 1
    assert event_names(released[0]) == ["codex.user_prompt", "codex.tool_result"]


def test_conversations_and_agents_stay_separate() -> None:
    clock = FakeClock()
    aggregator = make_aggregator(clock)

    aggregator.add("codex", one_request(("codex.user_prompt", "c1")))
    aggregator.add("codex", one_request(("codex.user_prompt", "c2")))
    aggregator.add("claude-code", one_request(("claude_code.user_prompt", "c1")))

    clock.advance(11)
    released = aggregator.due()

    assert len(released) == 3
    assert {turn.agent for turn in released} == {"codex", "claude-code"}
    assert all(turn.records == 1 for turn in released)


def test_a_turn_is_released_once_it_reaches_the_record_cap() -> None:
    clock = FakeClock()
    aggregator = make_aggregator(clock, max_turn_records=3)

    aggregator.add("codex", one_request(("codex.user_prompt", "c1")))
    aggregator.add("codex", one_request(("codex.sse_event", "c1")))
    released = aggregator.add("codex", one_request(("codex.sse_event", "c1")))

    assert len(released) == 1
    assert released[0].reason == "turn_full"
    assert released[0].records == 3
    assert aggregator.held_records() == 0


def test_the_oldest_turn_is_released_when_total_capacity_is_exceeded() -> None:
    clock = FakeClock()
    aggregator = make_aggregator(clock, max_total_records=2)

    aggregator.add("codex", one_request(("codex.user_prompt", "old")))
    clock.advance(1)
    aggregator.add("codex", one_request(("codex.user_prompt", "new")))
    clock.advance(1)
    released = aggregator.add("codex", one_request(("codex.sse_event", "third")))

    assert [turn.reason for turn in released] == ["capacity"]
    assert event_names(released[0]) == ["codex.user_prompt"]
    assert aggregator.held_records() == 2


def test_drain_releases_everything() -> None:
    clock = FakeClock()
    aggregator = make_aggregator(clock)

    aggregator.add("codex", one_request(("codex.user_prompt", "c1")))
    aggregator.add("codex", one_request(("codex.user_prompt", "c2")))

    released = aggregator.drain()

    assert len(released) == 2
    assert {turn.reason for turn in released} == {"drain"}
    assert aggregator.held_records() == 0


def test_the_sweeper_delivers_idle_turns_then_drains_on_stop() -> None:
    clock = FakeClock()
    aggregator = make_aggregator(clock)
    delivered: list[ReleasedTurn] = []
    stop = threading.Event()

    aggregator.add("codex", one_request(("codex.user_prompt", "c1")))
    clock.advance(11)

    run = sweeper(aggregator, delivered.append, interval_seconds=0.01, stop=stop)
    thread = threading.Thread(target=run)
    thread.start()
    try:
        for _ in range(200):
            if delivered:
                break
            threading.Event().wait(0.01)
    finally:
        stop.set()
        thread.join(timeout=5)

    assert [turn.reason for turn in delivered] == ["idle"]
    assert aggregator.held_records() == 0

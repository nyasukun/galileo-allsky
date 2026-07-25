from __future__ import annotations

import io

import pytest

from allsky_collector.server import CollectorStats, _read_chunked


def test_last_error_type_survives_a_later_success() -> None:
    stats = CollectorStats()

    stats.error("upstream")
    stats.success()

    snapshot = stats.snapshot()
    assert snapshot["last_error_type"] == "upstream"
    assert snapshot["last_error_at"] is not None
    assert snapshot["last_success_at"] is not None


def test_diagnostics_are_counted_globally_and_per_agent() -> None:
    stats = CollectorStats()

    stats.record_diagnostics("codex", {"logs.event.codex.sse_event": 4})
    stats.record_diagnostics("claude-code", {"logs.event.codex.sse_event": 1})
    stats.record_diagnostics("codex", None)
    stats.record_diagnostics("codex", {})

    counters = stats.snapshot()["counters"]
    assert counters["logs.event.codex.sse_event"] == 5
    assert counters["agent.codex.logs.event.codex.sse_event"] == 4
    assert counters["agent.claude-code.logs.event.codex.sse_event"] == 1


def test_chunked_reader_decodes_extensions_and_trailers_within_bound() -> None:
    payload = _read_chunked(
        io.BytesIO(b"4;extension=yes\r\ntest\r\n3\r\n123\r\n0\r\nX-Test: ok\r\n\r\n"),
        7,
    )

    assert payload == b"test123"


@pytest.mark.parametrize(
    "wire",
    [
        b"not-hex\r\n",
        b"4\r\nabc",
        b"0\r\nmissing-terminator",
    ],
)
def test_chunked_reader_rejects_malformed_framing(wire: bytes) -> None:
    with pytest.raises(ValueError):
        _read_chunked(io.BytesIO(wire), 1024)


def test_chunked_reader_rejects_payload_over_limit() -> None:
    with pytest.raises(OverflowError):
        _read_chunked(io.BytesIO(b"5\r\n12345\r\n0\r\n\r\n"), 4)

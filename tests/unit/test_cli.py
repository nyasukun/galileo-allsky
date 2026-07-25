from __future__ import annotations

import json
import logging
from pathlib import Path

from allsky_collector import cli


def _env_file(path: Path) -> None:
    path.write_text(
        "\n".join(
            (
                "GALILEO_API_KEY=test-key",
                "GALILEO_PROJECT=test-project",
                "ALLSKY_PSEUDONYM_SECRET=test-pseudonym",
            )
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)


def test_check_config_prints_secret_free_summary(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    path = tmp_path / "collector.env"
    _env_file(path)
    for name in (
        "GALILEO_API_KEY",
        "GALILEO_PROJECT",
        "ALLSKY_PSEUDONYM_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)

    exit_code = cli.run(["--env-file", str(path), "--check-config"])

    captured = capsys.readouterr()
    summary = json.loads(captured.out)
    assert exit_code == 0
    assert summary["project"] == "test-project"
    assert "test-key" not in captured.out
    assert "test-pseudonym" not in captured.out


def test_invalid_configuration_returns_two(monkeypatch, capsys) -> None:
    monkeypatch.delenv("GALILEO_API_KEY", raising=False)
    monkeypatch.delenv("GALILEO_PROJECT", raising=False)

    exit_code = cli.run(["--check-config"])

    assert exit_code == 2
    assert "configuration error" in capsys.readouterr().err


def test_run_starts_and_closes_server(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "collector.env"
    _env_file(path)
    for name in (
        "GALILEO_API_KEY",
        "GALILEO_PROJECT",
        "ALLSKY_PSEUDONYM_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)

    calls: list[object] = []

    class FakeServer:
        server_address = ("127.0.0.1", 4318)

        def serve_forever(self, *, poll_interval: float) -> None:
            calls.append(("serve", poll_interval))

        def shutdown(self) -> None:
            calls.append("shutdown")

        def server_close(self) -> None:
            calls.append("close")

    monkeypatch.setattr(cli, "make_server", lambda _application: FakeServer())
    monkeypatch.setattr(cli.signal, "signal", lambda *_args: None)

    exit_code = cli.run(["--env-file", str(path), "--log-level", "warning"])

    assert exit_code == 0
    assert calls == [("serve", 0.25), "close"]


def test_env_file_permission_warning(tmp_path: Path, caplog) -> None:
    path = tmp_path / "collector.env"
    _env_file(path)
    path.chmod(0o644)

    with caplog.at_level(logging.WARNING):
        cli._warn_env_file_permissions(str(path))

    assert "chmod 600" in caplog.text

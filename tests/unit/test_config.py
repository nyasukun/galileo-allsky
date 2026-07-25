from __future__ import annotations

import os

import pytest

from allsky_collector.config import (
    AGENTS,
    ConfigurationError,
    Settings,
    load_env_file,
)


def test_settings_resolve_routes_and_hide_secrets() -> None:
    settings = Settings.from_environ(
        {
            "GALILEO_API_KEY": "secret",
            "GALILEO_PROJECT": "project",
            "GALILEO_CODEX_LOG_STREAM": "custom-codex",
            "ALLSKY_PSEUDONYM_SECRET": "different-secret",
        }
    )

    assert settings.routes["codex"] == "custom-codex"
    assert set(settings.routes) == set(AGENTS)
    summary = settings.public_summary()
    assert summary["dedicated_pseudonym_secret"] is True
    assert summary["max_items_per_request"] == 10_000
    assert summary["max_output_bytes"] == 16 * 1024 * 1024
    assert summary["listen"] == "127.0.0.1:4318"
    assert "receiver_auth" not in summary
    assert "different-secret" not in repr(summary)
    assert "'secret'" not in repr(summary)


def test_api_key_is_default_pseudonym_secret() -> None:
    settings = Settings(api_key="key", project="project")

    assert settings.pseudonym_secret == "key"
    assert settings.public_summary()["dedicated_pseudonym_secret"] is False


@pytest.mark.parametrize("host", ["0.0.0.0", "::1", "localhost", "collector.local"])
def test_environment_cannot_override_fixed_loopback(host: str) -> None:
    with pytest.raises(ConfigurationError, match=r"fixed to 127\.0\.0\.1"):
        Settings.from_environ(
            {
                "GALILEO_API_KEY": "key",
                "GALILEO_PROJECT": "project",
                "ALLSKY_LISTEN_HOST": host,
            }
        )


def test_legacy_loopback_host_value_is_accepted() -> None:
    settings = Settings.from_environ(
        {
            "GALILEO_API_KEY": "key",
            "GALILEO_PROJECT": "project",
            "ALLSKY_LISTEN_HOST": "127.0.0.1",
        }
    )

    assert settings.public_summary()["listen"] == "127.0.0.1:4318"


def test_receiver_token_is_rejected_as_obsolete() -> None:
    with pytest.raises(ConfigurationError, match="not supported"):
        Settings.from_environ(
            {
                "GALILEO_API_KEY": "key",
                "GALILEO_PROJECT": "project",
                "ALLSKY_RECEIVER_TOKEN": "obsolete-token",
            }
        )


def test_insecure_upstream_requires_explicit_opt_in() -> None:
    with pytest.raises(ConfigurationError, match="https"):
        Settings(
            api_key="key",
            project="project",
            endpoint="http://127.0.0.1:9999/otel",
        )


def test_insecure_upstream_is_limited_to_loopback() -> None:
    with pytest.raises(ConfigurationError, match="loopback"):
        Settings(
            api_key="key",
            project="project",
            endpoint="http://remote.example/otel",
            allow_insecure_upstream=True,
        )


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://[::1",
        "https://example.test:99999/otel",
    ],
)
def test_malformed_endpoint_is_a_configuration_error(endpoint: str) -> None:
    with pytest.raises(ConfigurationError, match="valid URL"):
        Settings(api_key="key", project="project", endpoint=endpoint)


def test_exact_endpoint_path_including_trailing_slash_is_preserved() -> None:
    endpoint = "https://example.test/custom/otel/"

    assert Settings(api_key="key", project="project", endpoint=endpoint).endpoint == endpoint


def test_env_file_does_not_override_process_values(tmp_path) -> None:
    path = tmp_path / "collector.env"
    path.write_text(
        """
        # comment
        export GALILEO_API_KEY='from-file'
        GALILEO_PROJECT="project-name"
        ALLSKY_CAPTURE_CONTENT=true
        """,
        encoding="utf-8",
    )
    environ = {"GALILEO_API_KEY": "from-process"}

    load_env_file(path, environ)

    assert environ == {
        "GALILEO_API_KEY": "from-process",
        "GALILEO_PROJECT": "project-name",
        "ALLSKY_CAPTURE_CONTENT": "true",
    }


def test_env_file_rejects_shell_syntax_without_evaluating_it(tmp_path) -> None:
    path = tmp_path / "collector.env"
    path.write_text("not an assignment\n", encoding="utf-8")

    with pytest.raises(ConfigurationError, match="expected NAME=value"):
        load_env_file(path, {})


def test_environment_boolean_is_strict(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GALILEO_API_KEY", "key")
    monkeypatch.setenv("GALILEO_PROJECT", "project")
    monkeypatch.setenv("ALLSKY_CAPTURE_CONTENT", "sometimes")

    with pytest.raises(ConfigurationError, match="must be one of"):
        Settings.from_environ(os.environ)


@pytest.mark.parametrize(
    ("values", "message"),
    [
        (
            {"GALILEO_API_KEY": "key", "GALILEO_PROJECT": ""},
            "GALILEO_PROJECT",
        ),
        (
            {
                "GALILEO_API_KEY": "key",
                "GALILEO_PROJECT": "project",
                "ALLSKY_LISTEN_PORT": "abc",
            },
            "must be an integer",
        ),
        (
            {
                "GALILEO_API_KEY": "key",
                "GALILEO_PROJECT": "project",
                "ALLSKY_FORWARD_TIMEOUT_SECONDS": "500",
            },
            "between",
        ),
        (
            {
                "GALILEO_API_KEY": "key",
                "GALILEO_PROJECT": "project",
                "GALILEO_OTLP_TRACES_ENDPOINT": "https://example.test/otel?secret=x",
            },
            "query or fragment",
        ),
        (
            {
                "GALILEO_API_KEY": "key",
                "GALILEO_PROJECT": "project",
                "GALILEO_OTLP_TRACES_ENDPOINT": "https://user:pass@example.test/otel",
            },
            "user information",
        ),
        (
            {
                "GALILEO_API_KEY": "key",
                "GALILEO_PROJECT": "project",
                "ALLSKY_MAX_ITEMS_PER_REQUEST": "0",
            },
            "between",
        ),
    ],
)
def test_invalid_environment_values_are_rejected(
    values: dict[str, str],
    message: str,
) -> None:
    with pytest.raises(ConfigurationError, match=message):
        Settings.from_environ(values)


def test_missing_route_is_rejected() -> None:
    with pytest.raises(ConfigurationError, match="missing Galileo"):
        Settings(
            api_key="key",
            project="project",
            routes={"codex": "codex"},
        )


def test_header_values_reject_control_characters() -> None:
    with pytest.raises(ConfigurationError, match="control characters"):
        Settings(api_key="key\r\nInjected: yes", project="project")


def test_outbound_header_values_reject_unencodable_unicode() -> None:
    with pytest.raises(ConfigurationError, match="Latin-1"):
        Settings(api_key="key", project="日本語プロジェクト")


def test_env_file_rejects_invalid_name_and_unterminated_quote(tmp_path) -> None:
    invalid_name = tmp_path / "invalid-name.env"
    invalid_name.write_text("BAD-NAME=value\n", encoding="utf-8")
    unterminated = tmp_path / "unterminated.env"
    unterminated.write_text("VALUE='oops\n", encoding="utf-8")

    with pytest.raises(ConfigurationError, match="invalid variable name"):
        load_env_file(invalid_name, {})
    with pytest.raises(ConfigurationError, match="unterminated"):
        load_env_file(unterminated, {})

"""Validated collector configuration."""

from __future__ import annotations

import ipaddress
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from urllib.parse import urlsplit


class ConfigurationError(ValueError):
    """Raised when the collector cannot start safely."""


AGENTS = (
    "codex",
    "chatgpt",
    "claude-code",
    "claude-cowork",
    "codex-cli",
    "claude-code-cli",
)
LISTEN_HOST = "127.0.0.1"

AGENT_ENV_NAMES = {
    "codex": "GALILEO_CODEX_LOG_STREAM",
    "chatgpt": "GALILEO_CHATGPT_LOG_STREAM",
    "claude-code": "GALILEO_CLAUDE_CODE_LOG_STREAM",
    "claude-cowork": "GALILEO_CLAUDE_COWORK_LOG_STREAM",
    "codex-cli": "GALILEO_CODEX_CLI_LOG_STREAM",
    "claude-code-cli": "GALILEO_CLAUDE_CODE_CLI_LOG_STREAM",
}

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off"})
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _text(environ: Mapping[str, str], name: str, default: str = "") -> str:
    value = environ.get(name)
    return default if value is None or not value.strip() else value.strip()


def _boolean(environ: Mapping[str, str], name: str, default: bool) -> bool:
    raw = environ.get(name)
    if raw is None or not raw.strip():
        return default
    normalized = raw.strip().lower()
    if normalized in _TRUTHY:
        return True
    if normalized in _FALSY:
        return False
    raise ConfigurationError(f"{name} must be one of: true, false, 1, 0, yes, no")


def _integer(
    environ: Mapping[str, str],
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    raw = environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{name} must be between {minimum} and {maximum}")
    return value


def _floating(
    environ: Mapping[str, str],
    name: str,
    default: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    raw = environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number") from exc
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{name} must be between {minimum} and {maximum}")
    return value


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _validate_endpoint(endpoint: str, *, allow_insecure: bool) -> str:
    try:
        parsed = urlsplit(endpoint)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ConfigurationError("GALILEO_OTLP_TRACES_ENDPOINT is not a valid URL") from exc
    if parsed.scheme not in {"http", "https"} or not hostname:
        raise ConfigurationError("GALILEO_OTLP_TRACES_ENDPOINT must be an absolute http(s) URL")
    if port is not None and not 1 <= port <= 65_535:
        raise ConfigurationError("GALILEO_OTLP_TRACES_ENDPOINT has an invalid port")
    if parsed.query or parsed.fragment:
        raise ConfigurationError(
            "GALILEO_OTLP_TRACES_ENDPOINT must not contain a query or fragment"
        )
    if parsed.username is not None or parsed.password is not None:
        raise ConfigurationError("GALILEO_OTLP_TRACES_ENDPOINT must not contain user information")
    if parsed.scheme != "https":
        if not allow_insecure:
            raise ConfigurationError(
                "GALILEO_OTLP_TRACES_ENDPOINT must use https; "
                "set ALLSKY_ALLOW_INSECURE_UPSTREAM=true only for a trusted local stub"
            )
        if not _is_loopback(hostname):
            raise ConfigurationError(
                "an insecure GALILEO_OTLP_TRACES_ENDPOINT must use a loopback host"
            )
    return endpoint


def _validate_header_value(name: str, value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ConfigurationError(f"{name} must not be empty")
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        raise ConfigurationError(f"{name} must not contain control characters")
    try:
        normalized.encode("latin-1")
    except UnicodeEncodeError as exc:
        raise ConfigurationError(
            f"{name} must contain only HTTP header-compatible Latin-1 characters"
        ) from exc
    return normalized


@dataclass(frozen=True)
class Settings:
    """Immutable settings resolved before the receiver starts."""

    api_key: str
    project: str
    endpoint: str = "https://api.galileo.ai/otel/v1/traces"
    routes: Mapping[str, str] = field(
        default_factory=lambda: MappingProxyType({agent: agent for agent in AGENTS})
    )
    port: int = 4318
    pseudonym_secret: str = ""
    capture_content: bool = False
    max_content_chars: int = 12_000
    max_request_bytes: int = 8 * 1024 * 1024
    max_items_per_request: int = 10_000
    max_output_bytes: int = 16 * 1024 * 1024
    forward_timeout_seconds: float = 5.0
    allow_insecure_upstream: bool = False
    forward_unidentified_logs: bool = False
    aggregate_turns: bool = True
    turn_idle_seconds: float = 12.0
    max_turn_records: int = 2_000
    max_buffered_records: int = 50_000

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "api_key",
            _validate_header_value("GALILEO_API_KEY", self.api_key),
        )
        object.__setattr__(
            self,
            "project",
            _validate_header_value("GALILEO_PROJECT", self.project),
        )
        if not 1 <= self.port <= 65_535:
            raise ConfigurationError("ALLSKY_LISTEN_PORT must be between 1 and 65535")
        if not self.pseudonym_secret:
            object.__setattr__(self, "pseudonym_secret", self.api_key)
        if not 256 <= self.max_content_chars <= 1_000_000:
            raise ConfigurationError("ALLSKY_MAX_CONTENT_CHARS must be between 256 and 1000000")
        if not 1_024 <= self.max_request_bytes <= 256 * 1024 * 1024:
            raise ConfigurationError("ALLSKY_MAX_REQUEST_BYTES must be between 1024 and 268435456")
        if not 1 <= self.max_items_per_request <= 1_000_000:
            raise ConfigurationError("ALLSKY_MAX_ITEMS_PER_REQUEST must be between 1 and 1000000")
        if not 1_024 <= self.max_output_bytes <= 512 * 1024 * 1024:
            raise ConfigurationError("ALLSKY_MAX_OUTPUT_BYTES must be between 1024 and 536870912")
        if not 0.1 <= self.forward_timeout_seconds <= 300:
            raise ConfigurationError("ALLSKY_FORWARD_TIMEOUT_SECONDS must be between 0.1 and 300")
        if not 1 <= self.turn_idle_seconds <= 600:
            raise ConfigurationError("ALLSKY_TURN_IDLE_SECONDS must be between 1 and 600")
        if not 1 <= self.max_turn_records <= 100_000:
            raise ConfigurationError("ALLSKY_MAX_TURN_RECORDS must be between 1 and 100000")
        if self.max_buffered_records < self.max_turn_records:
            raise ConfigurationError(
                "ALLSKY_MAX_BUFFERED_RECORDS must be at least ALLSKY_MAX_TURN_RECORDS"
            )
        if self.max_buffered_records > 1_000_000:
            raise ConfigurationError("ALLSKY_MAX_BUFFERED_RECORDS must not exceed 1000000")
        normalized_routes = {
            agent: _validate_header_value(AGENT_ENV_NAMES[agent], stream)
            for agent, stream in self.routes.items()
            if agent in AGENTS and stream.strip()
        }
        missing = sorted(set(AGENTS) - set(normalized_routes))
        if missing:
            raise ConfigurationError(f"missing Galileo Log stream routes: {', '.join(missing)}")
        object.__setattr__(self, "routes", MappingProxyType(normalized_routes))
        object.__setattr__(
            self,
            "endpoint",
            _validate_endpoint(self.endpoint.strip(), allow_insecure=self.allow_insecure_upstream),
        )

    @classmethod
    def from_environ(cls, environ: Mapping[str, str] | None = None) -> Settings:
        source = os.environ if environ is None else environ
        configured_host = _text(source, "ALLSKY_LISTEN_HOST", LISTEN_HOST)
        if configured_host != LISTEN_HOST:
            raise ConfigurationError(
                f"collector listen address is fixed to {LISTEN_HOST}; remove ALLSKY_LISTEN_HOST"
            )
        if _text(source, "ALLSKY_RECEIVER_TOKEN"):
            raise ConfigurationError(
                "ALLSKY_RECEIVER_TOKEN is not supported by the loopback-only collector"
            )
        allow_insecure = _boolean(source, "ALLSKY_ALLOW_INSECURE_UPSTREAM", False)
        routes = {
            agent: _text(source, env_name, agent) for agent, env_name in AGENT_ENV_NAMES.items()
        }
        return cls(
            api_key=_text(source, "GALILEO_API_KEY"),
            project=_text(source, "GALILEO_PROJECT"),
            endpoint=_validate_endpoint(
                _text(
                    source,
                    "GALILEO_OTLP_TRACES_ENDPOINT",
                    "https://api.galileo.ai/otel/v1/traces",
                ),
                allow_insecure=allow_insecure,
            ),
            routes=routes,
            port=_integer(source, "ALLSKY_LISTEN_PORT", 4318, minimum=1, maximum=65_535),
            pseudonym_secret=_text(source, "ALLSKY_PSEUDONYM_SECRET"),
            capture_content=_boolean(source, "ALLSKY_CAPTURE_CONTENT", False),
            max_content_chars=_integer(
                source,
                "ALLSKY_MAX_CONTENT_CHARS",
                12_000,
                minimum=256,
                maximum=1_000_000,
            ),
            max_request_bytes=_integer(
                source,
                "ALLSKY_MAX_REQUEST_BYTES",
                8 * 1024 * 1024,
                minimum=1_024,
                maximum=256 * 1024 * 1024,
            ),
            max_items_per_request=_integer(
                source,
                "ALLSKY_MAX_ITEMS_PER_REQUEST",
                10_000,
                minimum=1,
                maximum=1_000_000,
            ),
            max_output_bytes=_integer(
                source,
                "ALLSKY_MAX_OUTPUT_BYTES",
                16 * 1024 * 1024,
                minimum=1_024,
                maximum=512 * 1024 * 1024,
            ),
            forward_timeout_seconds=_floating(
                source,
                "ALLSKY_FORWARD_TIMEOUT_SECONDS",
                5.0,
                minimum=0.1,
                maximum=300,
            ),
            allow_insecure_upstream=allow_insecure,
            forward_unidentified_logs=_boolean(
                source,
                "ALLSKY_FORWARD_UNIDENTIFIED_LOGS",
                False,
            ),
            aggregate_turns=_boolean(source, "ALLSKY_AGGREGATE_TURNS", True),
            turn_idle_seconds=_floating(
                source,
                "ALLSKY_TURN_IDLE_SECONDS",
                12.0,
                minimum=1,
                maximum=600,
            ),
            max_turn_records=_integer(
                source,
                "ALLSKY_MAX_TURN_RECORDS",
                2_000,
                minimum=1,
                maximum=100_000,
            ),
            max_buffered_records=_integer(
                source,
                "ALLSKY_MAX_BUFFERED_RECORDS",
                50_000,
                minimum=1,
                maximum=1_000_000,
            ),
        )

    def public_summary(self) -> dict[str, object]:
        """Return diagnostic configuration without credentials."""

        return {
            "listen": f"{LISTEN_HOST}:{self.port}",
            "endpoint": self.endpoint,
            "project": self.project,
            "routes": dict(self.routes),
            "capture_content": self.capture_content,
            "dedicated_pseudonym_secret": self.pseudonym_secret != self.api_key,
            "max_request_bytes": self.max_request_bytes,
            "max_items_per_request": self.max_items_per_request,
            "max_output_bytes": self.max_output_bytes,
            "forward_timeout_seconds": self.forward_timeout_seconds,
            "forward_unidentified_logs": self.forward_unidentified_logs,
            "aggregate_turns": self.aggregate_turns,
            "turn_idle_seconds": self.turn_idle_seconds,
            "max_turn_records": self.max_turn_records,
            "max_buffered_records": self.max_buffered_records,
        }


def load_env_file(path: str | os.PathLike[str], environ: dict[str, str] | None = None) -> None:
    """Load a small dotenv subset without evaluating shell syntax.

    Existing environment variables win. Values may be unquoted, single quoted,
    or double quoted; interpolation and command substitution are intentionally
    unsupported.
    """

    target = os.environ if environ is None else environ
    env_path = Path(path).expanduser()
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ConfigurationError(f"cannot read env file {env_path}: {exc}") from exc

    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ConfigurationError(f"{env_path}:{line_number}: expected NAME=value")
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not _ENV_NAME.fullmatch(name):
            raise ConfigurationError(f"{env_path}:{line_number}: invalid variable name")
        if value[:1] in {"'", '"'}:
            quote = value[0]
            if len(value) < 2 or value[-1] != quote:
                raise ConfigurationError(f"{env_path}:{line_number}: unterminated quoted value")
            value = value[1:-1]
            if quote == '"':
                value = (
                    value.replace(r"\\", "\\")
                    .replace(r"\n", "\n")
                    .replace(r"\t", "\t")
                    .replace(r"\"", '"')
                )
        target.setdefault(name, value)

#!/usr/bin/env python3
"""Detect and record changes to this project's resolved Python dependencies."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_CANONICAL_NAME_SEPARATOR = re.compile(r"[-_.]+")
_DISTRIBUTION_NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+!-]*$")
_PROJECT_DISTRIBUTION = "galileo-allsky"


class BaselineError(ValueError):
    """Raised when a dependency baseline or pip report is invalid."""


@dataclass(frozen=True, slots=True)
class Baseline:
    python: str
    resolved: dict[str, str]


def _canonical_name(value: Any) -> str:
    return _CANONICAL_NAME_SEPARATOR.sub("-", str(value).strip()).lower()


def _validated_version(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not _VERSION_PATTERN.fullmatch(value):
        raise BaselineError(f"{label} is not a safe package version")
    return value


def _validated_resolved(value: Any, *, label: str) -> dict[str, str]:
    if not isinstance(value, dict) or not value:
        raise BaselineError(f"{label} must be a non-empty JSON object")

    resolved: dict[str, str] = {}
    for raw_name, raw_version in value.items():
        if not isinstance(raw_name, str):
            raise BaselineError(f"{label} distribution names must be strings")
        name = _canonical_name(raw_name)
        if raw_name != name or not _DISTRIBUTION_NAME_PATTERN.fullmatch(name):
            raise BaselineError(f"{label} contains unsafe distribution {raw_name!r}")
        resolved[name] = _validated_version(raw_version, label=f"{label} version for {name}")
    return dict(sorted(resolved.items()))


def load_baseline(path: Path) -> Baseline:
    """Load the committed resolution baseline."""

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        python = data["python"]
        resolved = data["resolved"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise BaselineError(f"cannot load dependency baseline {path}: {exc}") from exc

    if not isinstance(python, str) or not re.fullmatch(r"3\.\d+", python):
        raise BaselineError("baseline python must use the major.minor form")
    return Baseline(python=python, resolved=_validated_resolved(resolved, label="baseline"))


def resolve_dependencies(repository: Path) -> dict[str, str]:
    """Resolve runtime and dev dependencies with pip without installing them."""

    with tempfile.TemporaryDirectory(prefix="galileo-allsky-pip-report-") as temporary:
        report_path = Path(temporary) / "report.json"
        command = [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-input",
            "--dry-run",
            "--ignore-installed",
            "--report",
            str(report_path),
            "-e",
            ".[dev]",
        ]
        try:
            subprocess.run(
                command,
                cwd=repository,
                check=True,
                capture_output=True,
                text=True,
            )
            payload = json.loads(report_path.read_text(encoding="utf-8"))
        except subprocess.CalledProcessError as exc:
            details = (exc.stderr or exc.stdout or str(exc)).strip()
            raise BaselineError(f"pip could not resolve project dependencies: {details}") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise BaselineError(f"cannot read pip resolution report: {exc}") from exc

    installs = payload.get("install") if isinstance(payload, dict) else None
    if not isinstance(installs, list):
        raise BaselineError("pip resolution report must contain an install list")

    resolved: dict[str, str] = {}
    for item in installs:
        if not isinstance(item, dict) or not isinstance(item.get("metadata"), dict):
            raise BaselineError("pip resolution report contains invalid package metadata")
        metadata = item["metadata"]
        try:
            name = _canonical_name(metadata["name"])
            version = _validated_version(metadata["version"], label=f"resolved version for {name}")
        except KeyError as exc:
            raise BaselineError(f"pip resolution report is missing {exc.args[0]!r}") from exc
        if not _DISTRIBUTION_NAME_PATTERN.fullmatch(name):
            raise BaselineError(f"pip resolved unsafe distribution {name!r}")
        if name == _PROJECT_DISTRIBUTION:
            continue
        previous = resolved.get(name)
        if previous is not None and previous != version:
            raise BaselineError(f"pip resolved conflicting versions for {name}")
        resolved[name] = version

    return _validated_resolved(resolved, label="resolved dependency closure")


def _compact_json(value: dict[str, str]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _write_github_outputs(outputs: dict[str, str]) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT", "").strip()
    if not output_path:
        return
    with Path(output_path).open("a", encoding="utf-8") as output_file:
        for name, value in outputs.items():
            output_file.write(f"{name}={value}\n")


def write_baseline(path: Path, baseline: Baseline, resolved: dict[str, str]) -> None:
    """Record a resolution only after its E2E test has completed successfully."""

    data = {"python": baseline.python, "resolved": _validated_resolved(resolved, label="tested")}
    path.write_text(f"{json.dumps(data, indent=2, sort_keys=True)}\n", encoding="utf-8")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--baseline",
        type=Path,
        default=Path(".github/dependency-baseline.json"),
    )
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--resolved-json", help="successfully tested dependency closure")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        baseline = load_baseline(args.baseline)
        if args.write:
            if not args.resolved_json:
                raise BaselineError("--write requires --resolved-json")
            try:
                resolved_payload = json.loads(args.resolved_json)
            except json.JSONDecodeError as exc:
                raise BaselineError(f"--resolved-json is invalid JSON: {exc}") from exc
            resolved = _validated_resolved(resolved_payload, label="tested")
            write_baseline(args.baseline, baseline, resolved)
            print(f"Recorded tested dependency closure: {_compact_json(resolved)}")
            return 0

        latest = resolve_dependencies(args.repository.resolve())
        outputs = {
            "updated": str(latest != baseline.resolved).lower(),
            "baseline_resolved_json": _compact_json(baseline.resolved),
            "resolved_json": _compact_json(latest),
        }
        _write_github_outputs(outputs)
        print(
            "Dependency check: "
            f"{_compact_json(baseline.resolved)} -> {_compact_json(latest)}; "
            f"updated={outputs['updated']}"
        )
        return 0
    except BaselineError as exc:
        print(f"dependency check failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

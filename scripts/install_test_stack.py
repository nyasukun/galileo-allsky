#!/usr/bin/env python3
"""Install the exact dependency closure selected by the daily dependency watcher."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from importlib import metadata
from pathlib import Path
from typing import Any

_CANONICAL_NAME_SEPARATOR = re.compile(r"[-_.]+")
_DISTRIBUTION_NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+!-]*$")


def _canonical_name(value: Any) -> str:
    return _CANONICAL_NAME_SEPARATOR.sub("-", str(value).strip()).lower()


def parse_resolved_json(value: str) -> dict[str, str]:
    """Validate a dependency closure emitted by check_dependency_updates.py."""

    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"resolved JSON is invalid: {exc}") from exc
    if not isinstance(payload, dict) or not payload:
        raise ValueError("resolved JSON must be a non-empty object")

    resolved: dict[str, str] = {}
    for raw_name, raw_version in payload.items():
        if not isinstance(raw_name, str):
            raise ValueError("resolved distribution names must be strings")
        name = _canonical_name(raw_name)
        if raw_name != name or not _DISTRIBUTION_NAME_PATTERN.fullmatch(name):
            raise ValueError(f"resolved JSON contains unsafe distribution {raw_name!r}")
        if not isinstance(raw_version, str) or not _VERSION_PATTERN.fullmatch(raw_version):
            raise ValueError(f"resolved version for {name} contains unsafe characters")
        resolved[name] = raw_version
    return dict(sorted(resolved.items()))


def _verify_installed_versions(resolved: dict[str, str]) -> None:
    for name, expected in resolved.items():
        try:
            actual = metadata.version(name)
        except metadata.PackageNotFoundError as exc:
            raise RuntimeError(f"resolved distribution {name} was not installed") from exc
        if actual != expected:
            raise RuntimeError(f"installed {name}={actual} does not match {expected}")


def install(repository: Path, resolved: dict[str, str]) -> None:
    """Install the local package and every selected distribution without re-resolving."""

    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-input",
            "--no-deps",
            "-e",
            str(repository),
        ],
        check=True,
    )
    with tempfile.TemporaryDirectory(prefix="galileo-allsky-requirements-") as temporary:
        requirements = Path(temporary) / "requirements.txt"
        requirements.write_text(
            "".join(f"{name}=={version}\n" for name, version in resolved.items()),
            encoding="utf-8",
        )
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-input",
                "--no-deps",
                "-r",
                str(requirements),
            ],
            check=True,
        )
    _verify_installed_versions(resolved)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resolved-json", required=True)
    parser.add_argument("--repository", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    install(args.repository.resolve(), parse_resolved_json(args.resolved_json))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Compare the latest hermes-galileo README commit with its tracked baseline."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")


class BaselineError(ValueError):
    """Raised when the README baseline or upstream response is invalid."""


@dataclass(frozen=True, slots=True)
class Baseline:
    repository: str
    path: str
    sha: str


def _validated_sha(value: Any, *, label: str) -> str:
    if not isinstance(value, str):
        raise BaselineError(f"{label} must be a string")
    sha = value.strip().lower()
    if not _SHA_PATTERN.fullmatch(sha):
        raise BaselineError(f"{label} must be a 40-character Git SHA")
    return sha


def load_baseline(path: Path) -> Baseline:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        repository = data["repository"]
        readme_path = data["path"]
        sha = data["sha"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise BaselineError(f"cannot load README baseline {path}: {exc}") from exc
    if not isinstance(repository, str) or not _REPOSITORY_PATTERN.fullmatch(repository):
        raise BaselineError("baseline repository must use the owner/repository form")
    if readme_path != "README.md":
        raise BaselineError("baseline path must be README.md")
    return Baseline(
        repository=repository, path=readme_path, sha=_validated_sha(sha, label="baseline SHA")
    )


def _get_json(url: str, *, token: str = "", attempts: int = 3) -> Any:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "galileo-allsky-readme-watch"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.loads(response.read())
        except (
            OSError,
            urllib.error.HTTPError,
            urllib.error.URLError,
            json.JSONDecodeError,
        ) as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(2**attempt)
    raise BaselineError(f"failed to query {url}: {last_error}")


def fetch_latest_sha(baseline: Baseline, *, github_token: str = "") -> str:
    query = urllib.parse.urlencode({"path": baseline.path, "per_page": "1"})
    payload = _get_json(
        f"https://api.github.com/repos/{baseline.repository}/commits?{query}",
        token=github_token,
    )
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
        raise BaselineError("GitHub did not return a README commit")
    return _validated_sha(payload[0].get("sha"), label="latest README SHA")


def _write_github_outputs(outputs: dict[str, str]) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT", "").strip()
    if not output_path:
        return
    with Path(output_path).open("a", encoding="utf-8") as output_file:
        for name, value in outputs.items():
            output_file.write(f"{name}={value}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--baseline",
        type=Path,
        default=Path(".github/hermes-galileo-readme-baseline.json"),
    )
    args = parser.parse_args(argv)
    try:
        baseline = load_baseline(args.baseline)
        latest = fetch_latest_sha(baseline, github_token=os.environ.get("GITHUB_TOKEN", ""))
        outputs = {
            "changed": str(latest != baseline.sha).lower(),
            "baseline_sha": baseline.sha,
            "latest_sha": latest,
        }
        _write_github_outputs(outputs)
        print(f"Hermes README check: {baseline.sha} -> {latest}; changed={outputs['changed']}")
        return 0
    except BaselineError as exc:
        print(f"Hermes README check failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

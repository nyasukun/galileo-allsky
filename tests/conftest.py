from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import pytest
from opentelemetry.proto.common.v1.common_pb2 import KeyValue

from allsky_collector.config import Settings
from allsky_collector.privacy import any_value_to_python, python_to_any_value


@pytest.fixture
def settings() -> Settings:
    return Settings(
        api_key="test-galileo-api-key",
        project="allsky-test",
        pseudonym_secret="test-pseudonym-secret",
    )


def add_attribute(attributes: Any, key: str, value: Any) -> KeyValue:
    if hasattr(attributes, "add"):
        pair = attributes.add()
    else:
        pair = KeyValue()
        attributes.append(pair)
    pair.key = key
    pair.value.CopyFrom(python_to_any_value(value))
    return pair


def attributes_dict(attributes: Iterable[KeyValue]) -> dict[str, Any]:
    return {attribute.key: any_value_to_python(attribute.value) for attribute in attributes}

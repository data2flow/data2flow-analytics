"""결과·ChartSpec·템플릿 manifest·이벤트 JSON Schema(2020-12).

정본은 data2flow-contracts/schemas/analytics/로 옮길 예정이다(ANA test-plan "스키마"). 그때까지 생산자(analytics)가 이 파일로 검증한다.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

DIR = Path(__file__).parent


@lru_cache
def _registry() -> Registry:
    resources = []
    for name in ("chartspec.v1.json", "result.v1.json", "template-manifest.v1.json", "events/analytics-events.v1.json"):
        schema = json.loads((DIR / name).read_text(encoding="utf-8"))
        res = Resource.from_contents(schema)
        resources.append((schema["$id"], res))
        resources.append((name.split("/")[-1], res))
    return Registry().with_resources(resources)


def load(name: str) -> dict:
    return json.loads((DIR / name).read_text(encoding="utf-8"))


@lru_cache
def result_validator() -> Draft202012Validator:
    return Draft202012Validator(load("result.v1.json"), registry=_registry())


@lru_cache
def chartspec_validator() -> Draft202012Validator:
    return Draft202012Validator(load("chartspec.v1.json"), registry=_registry())


@lru_cache
def manifest_validator() -> Draft202012Validator:
    return Draft202012Validator(load("template-manifest.v1.json"), registry=_registry())


def event_validator(event_type: str) -> Draft202012Validator:
    schema = load("events/analytics-events.v1.json")
    ref = schema["x-payloadTypes"][event_type]
    return Draft202012Validator({**schema["$defs"][ref], "$defs": schema["$defs"]}, registry=_registry())

from __future__ import annotations

import json
from pathlib import Path

import jsonschema

from mewcode.run_result import RUN_RESULT_SCHEMA_VERSION, RunResult


def test_run_result_json_and_jsonl_are_machine_readable() -> None:
    result = RunResult(
        status="succeeded",
        result="完成",
        provider="local",
        task_id="task-1",
        events=[{"type": "tool", "name": "ReadFile"}],
    )
    payload = json.loads(result.to_json())
    assert payload["schema_version"] == 1
    assert payload["status"] == "succeeded"
    lines = [json.loads(line) for line in result.to_jsonl().splitlines()]
    assert lines[0]["type"] == "tool"
    assert lines[-1]["type"] == "run_result"
    schema = json.loads(
        (Path(__file__).parents[1] / "schemas" / "run-result.schema.json").read_text(
            encoding="utf-8"
        )
    )
    jsonschema.validate(payload, schema)
    assert schema["$defs"]["schemaVersion"]["const"] == RUN_RESULT_SCHEMA_VERSION
    for record in lines:
        jsonschema.validate(record, schema)


def test_run_result_jsonl_normalises_untyped_and_reserved_events() -> None:
    schema = json.loads(
        (Path(__file__).parents[1] / "schemas" / "run-result.schema.json").read_text(
            encoding="utf-8"
        )
    )
    result = RunResult(
        status="succeeded",
        events=[{}, {"type": "run_result", "detail": "not a terminal record"}],
    )

    records = [json.loads(line) for line in result.to_jsonl().splitlines()]

    assert [record["type"] for record in records] == ["event", "event", "run_result"]
    assert records[1]["event_type"] == "run_result"
    for record in records:
        jsonschema.validate(record, schema)

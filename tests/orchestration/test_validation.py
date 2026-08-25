from __future__ import annotations

import json
import sys

from mewcode.orchestration import (
    AcceptanceCriterion,
    AgentRole,
    ArtifactContract,
    DiagnosticSeverity,
    TaskGraph,
    TaskNode,
    validate_dag_file,
    validate_task_graph,
)


def acceptance() -> tuple[AcceptanceCriterion, ...]:
    return (
        AcceptanceCriterion(
            criterion_id="host-pass",
            description="deterministic validation fixture",
            verifier_argv=(sys.executable, "-c", "raise SystemExit(0)"),
        ),
    )


def write_graph(path, graph: TaskGraph) -> None:
    path.write_text(graph.model_dump_json(indent=2), encoding="utf-8")


def test_validate_file_is_offline_side_effect_free_and_returns_preview(
    tmp_path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    graph_path = workspace / "graph.json"
    graph = TaskGraph(
        nodes=(
            TaskNode(
                node_id="a",
                role=AgentRole.IMPLEMENTER,
                objective="write a",
                predicted_write_set=("src/**",),
                token_budget=20,
                acceptance_criteria=acceptance(),
            ),
            TaskNode(
                node_id="b",
                role=AgentRole.IMPLEMENTER,
                objective="write b",
                predicted_write_set=("src/**",),
                token_budget=20,
                acceptance_criteria=acceptance(),
            ),
        )
    )
    write_graph(graph_path, graph)
    provider_calls = 0

    def provider_bomb(*args, **kwargs):
        nonlocal provider_calls
        provider_calls += 1
        raise AssertionError("validate-only must not construct a Provider")

    monkeypatch.setattr("mewcode.client.create_client", provider_bomb)
    before = {
        path.relative_to(workspace).as_posix(): path.read_bytes()
        for path in workspace.rglob("*")
        if path.is_file()
    }

    report = validate_dag_file(graph_path, max_concurrency=2)

    after = {
        path.relative_to(workspace).as_posix(): path.read_bytes()
        for path in workspace.rglob("*")
        if path.is_file()
    }
    assert provider_calls == 0
    assert before == after
    assert not (workspace / ".mewcode").exists()
    assert report.valid is True
    assert report.preview is not None
    # Conflicting write leases are placed into separate static dispatch waves.
    assert report.preview.dispatch_waves == (("a",), ("b",))
    assert report.preview.potential_write_conflicts == (("a", "b"),)
    assert report.preview.token_enforcement == (
        "reservation_and_completion_reconciliation"
    )
    assert report.preview.in_flight_token_limit is False
    assert report.machine_readable()["preview"]["nodes"][0]["role"] == "implementer"


def test_validate_file_reports_schema_json_path(tmp_path) -> None:
    graph_path = tmp_path / "bad.json"
    graph_path.write_text(
        json.dumps(
            {
                "nodes": [
                    {
                        "node_id": "bad-role",
                        "role": "wizard",
                        "objective": "invalid role",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    report = validate_dag_file(graph_path)

    assert report.valid is False
    assert report.preview is None
    role_error = next(
        item for item in report.diagnostics if item.path == "$.nodes[0].role"
    )
    assert role_error.severity is DiagnosticSeverity.ERROR
    assert role_error.code.startswith("dag.schema.")


def test_validate_file_reports_cycle_as_structured_graph_diagnostic(tmp_path) -> None:
    graph_path = tmp_path / "cycle.json"
    graph_path.write_text(
        json.dumps(
            {
                "nodes": [
                    {
                        "node_id": "a",
                        "role": "explorer",
                        "objective": "a",
                        "depends_on": ["b"],
                    },
                    {
                        "node_id": "b",
                        "role": "explorer",
                        "objective": "b",
                        "depends_on": ["a"],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    report = validate_dag_file(graph_path)

    assert report.valid is False
    assert report.diagnostics[0].path == "$"
    assert "cycle" in report.diagnostics[0].message


def test_validate_reports_artifact_acceptance_conflict_and_budget_semantics() -> None:
    graph = TaskGraph(
        nodes=(
            TaskNode(
                node_id="producer",
                role=AgentRole.IMPLEMENTER,
                objective="produce patch",
                predicted_write_set=("src/**",),
                token_budget=15,
                # Deliberately no production acceptance criterion.
                artifact_contract=ArtifactContract(required_outputs=("patch",)),
            ),
            TaskNode(
                node_id="consumer",
                role=AgentRole.IMPLEMENTER,
                objective="consume inventory",
                depends_on=("producer",),
                predicted_write_set=("tests/**",),
                token_budget=15,
                acceptance_criteria=acceptance(),
                artifact_contract=ArtifactContract(required_inputs=("inventory",)),
            ),
        )
    )

    from mewcode.orchestration import ScheduleBudget

    report = validate_task_graph(
        graph,
        budget=ScheduleBudget(total_tokens=20, wall_time_seconds=1),
    )

    codes = {item.code for item in report.diagnostics}
    assert report.valid is False
    assert "dag.acceptance.blocking_required" in codes
    assert "dag.artifact.missing_dependency_output" in codes
    assert "dag.budget.reservation_pressure" in codes
    assert "dag.budget.estimated_critical_path_exceeds_wall_time" in codes
    pressure = next(
        item
        for item in report.diagnostics
        if item.code == "dag.budget.reservation_pressure"
    )
    # Reservation pressure is not mislabeled as guaranteed exhaustion because
    # completed nodes can return unused reservation to the run.
    assert pressure.severity is DiagnosticSeverity.WARNING
    assert "actual usage" in (pressure.remediation or "").lower()


def test_validate_file_turns_invalid_budget_and_concurrency_into_diagnostics(
    tmp_path,
) -> None:
    graph_path = tmp_path / "graph.json"
    write_graph(
        graph_path,
        TaskGraph(
            nodes=(
                TaskNode(
                    node_id="read",
                    role=AgentRole.EXPLORER,
                    objective="read",
                    acceptance_criteria=acceptance(),
                ),
            )
        ),
    )

    bad_budget = validate_dag_file(graph_path, total_tokens=0)
    bad_concurrency = validate_dag_file(graph_path, max_concurrency=0)

    assert bad_budget.valid is False
    assert bad_budget.diagnostics[0].code.startswith("dag.budget.")
    assert bad_budget.diagnostics[0].path == "$.budget.total_tokens"
    assert bad_concurrency.valid is False
    assert bad_concurrency.diagnostics[0].code == "dag.schedule.invalid"

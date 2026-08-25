from __future__ import annotations

import pytest
from pydantic import ValidationError

from mewcode.orchestration import (
    AcceptanceCriterion,
    AgentRole,
    ArtifactContract,
    TaskGraph,
    TaskNode,
    normalize_write_scope,
    write_scopes_conflict,
)


def node(node_id: str, **changes: object) -> TaskNode:
    values: dict[str, object] = {
        "node_id": node_id,
        "role": AgentRole.EXPLORER,
        "objective": f"run {node_id}",
    }
    values.update(changes)
    return TaskNode(**values)


def test_typed_node_exposes_acceptance_artifact_and_budget_contracts() -> None:
    task = node(
        "implement-api",
        role=AgentRole.IMPLEMENTER,
        acceptance_criteria=(
            AcceptanceCriterion(
                criterion_id="tests-pass",
                description="target tests pass",
                verifier_argv=("python", "-m", "pytest", "-q"),
            ),
        ),
        artifact_contract=ArtifactContract(required_outputs=("patch", "receipt")),
        token_budget=2500,
        timeout_seconds=30,
        predicted_write_set=(r"src\api\**",),
    )

    assert task.acceptance_criteria[0].blocking is True
    assert task.artifact_contract.required_outputs == ("patch", "receipt")
    assert task.predicted_write_set == ("src/api/**",)


def test_blocking_acceptance_requires_deterministic_argv() -> None:
    with pytest.raises(ValidationError, match="requires verifier_argv"):
        AcceptanceCriterion(criterion_id="claim", description="model says done")


@pytest.mark.parametrize("role", [AgentRole.EXPLORER, AgentRole.VERIFIER])
def test_read_only_roles_cannot_claim_a_write_set(role: AgentRole) -> None:
    with pytest.raises(ValidationError, match="must have an empty write-set"):
        node("readonly", role=role, predicted_write_set=("src/**",))


def test_write_scopes_are_workspace_relative_and_overlap_conservatively() -> None:
    assert normalize_write_scope(r".\src\api\**") == "src/api/**"
    assert write_scopes_conflict(("src/**",), ("src/api/client.py",))
    assert write_scopes_conflict(("src/api/**",), ("src/api/client.py",))
    assert not write_scopes_conflict(("src/api/**",), ("tests/api/**",))
    assert not write_scopes_conflict(("src/api",), ("src/api/client.py",))
    with pytest.raises(ValueError, match="workspace-relative"):
        normalize_write_scope("C:/outside/file.py")
    with pytest.raises(ValueError, match="traverse"):
        normalize_write_scope("../outside.py")


def test_graph_rejects_missing_dependency_and_cycle() -> None:
    with pytest.raises(ValidationError, match="unknown dependencies: missing"):
        TaskGraph(nodes=(node("a", depends_on=("missing",)),))

    with pytest.raises(ValidationError, match="contains a cycle"):
        TaskGraph(
            nodes=(
                node("a", depends_on=("b",)),
                node("b", depends_on=("a",)),
            )
        )


def test_critical_path_weights_include_longest_descendant_chain() -> None:
    graph = TaskGraph(
        nodes=(
            node("root", estimated_duration_seconds=2),
            node("short", depends_on=("root",), estimated_duration_seconds=1),
            node("long", depends_on=("root",), estimated_duration_seconds=4),
            node("tail", depends_on=("long",), estimated_duration_seconds=3),
        )
    )

    weights = graph.critical_path_weights()
    assert weights == {"tail": 3, "long": 7, "short": 1, "root": 9}
    assert graph.critical_path_seconds == 9

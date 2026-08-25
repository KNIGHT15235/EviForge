"""Side-effect-free validation and scheduling previews for typed DAG files.

The functions in this module deliberately depend only on the typed
orchestration models.  In particular, they do not construct Providers,
runtime stores, artifact stores, or workspace trackers.  This makes them safe
to use from ``dag validate`` / ``dag plan`` commands before any execution
composition starts.
"""

from __future__ import annotations

import json
import os
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .graph import TaskGraph
from .models import AgentRole, ScheduleBudget, TaskNode, write_scopes_conflict


class DiagnosticSeverity(StrEnum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class DAGDiagnostic(BaseModel):
    """One stable, machine-readable validation finding."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    severity: DiagnosticSeverity
    message: str
    path: str = "$"
    node_id: str | None = None
    remediation: str | None = None


class DAGNodePreview(BaseModel):
    """Static facts used by the scheduler for one node."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: str
    role: AgentRole
    depends_on: tuple[str, ...] = ()
    critical_path_weight_seconds: float = Field(ge=0.0)
    token_reservation: int = Field(ge=1)
    timeout_seconds: float = Field(gt=0.0)
    predicted_write_set: tuple[str, ...] = ()


class DAGSchedulePreview(BaseModel):
    """A deterministic preview, not a promise about wall-clock completion.

    ``dispatch_waves`` models dependency, concurrency and declared write-set
    constraints assuming every selected node completes before the next wave.
    Runtime duration, actual usage, failures and artifact contents can change
    the real schedule.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    node_count: int = Field(ge=1)
    max_concurrency: int = Field(ge=1)
    dispatch_waves: tuple[tuple[str, ...], ...]
    priority_order: tuple[str, ...]
    nodes: tuple[DAGNodePreview, ...]
    potential_write_conflicts: tuple[tuple[str, str], ...] = ()
    critical_path_seconds: float = Field(ge=0.0)
    configured_token_reservations: int = Field(ge=1)
    total_token_budget: int = Field(ge=1)
    configured_timeout_seconds: float = Field(gt=0.0)
    wall_time_budget_seconds: float = Field(gt=0.0)
    token_enforcement: str = "reservation_and_completion_reconciliation"
    in_flight_token_limit: bool = False


class DAGValidationReport(BaseModel):
    """Serializable result of a pure DAG validation pass."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "1.0"
    valid: bool
    source: str | None = None
    diagnostics: tuple[DAGDiagnostic, ...] = ()
    preview: DAGSchedulePreview | None = None

    def machine_readable(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def resolve_schedule_budget(
    graph: TaskGraph,
    *,
    total_tokens: int | None = None,
    wall_time_seconds: float | None = None,
) -> ScheduleBudget:
    """Resolve CLI overrides using the same defaults as DAG execution."""

    configured_tokens = sum(node.token_budget for node in graph.nodes)
    configured_wall = sum(node.timeout_seconds for node in graph.nodes)
    return ScheduleBudget(
        total_tokens=configured_tokens if total_tokens is None else total_tokens,
        wall_time_seconds=(
            configured_wall if wall_time_seconds is None else wall_time_seconds
        ),
    )


def _json_path(location: Iterable[str | int]) -> str:
    path = "$"
    for part in location:
        if isinstance(part, int):
            path += f"[{part}]"
        elif part.isidentifier():
            path += f".{part}"
        else:
            path += f"[{json.dumps(part, ensure_ascii=False)}]"
    return path


def _schema_diagnostics(exc: ValidationError) -> tuple[DAGDiagnostic, ...]:
    findings: list[DAGDiagnostic] = []
    for error in exc.errors(include_url=False, include_context=False):
        location = tuple(error.get("loc", ()))
        node_id = None
        if len(location) >= 2 and location[0] == "nodes" and isinstance(location[1], int):
            node_id = f"index:{location[1]}"
        findings.append(
            DAGDiagnostic(
                code=f"dag.schema.{error.get('type', 'invalid')}",
                severity=DiagnosticSeverity.ERROR,
                message=str(error.get("msg", "invalid TaskGraph")),
                path=_json_path(location),
                node_id=node_id,
                remediation="Correct the value at the reported JSON path and validate again.",
            )
        )
    return tuple(findings)


def _dispatch_waves(
    graph: TaskGraph, *, max_concurrency: int
) -> tuple[tuple[str, ...], ...]:
    """Build a deterministic, duration-agnostic scheduler preview."""

    weights = graph.critical_path_weights()
    remaining = set(graph.by_id)
    completed: set[str] = set()
    waves: list[tuple[str, ...]] = []
    while remaining:
        ready = [
            graph.by_id[node_id]
            for node_id in remaining
            if set(graph.by_id[node_id].depends_on) <= completed
        ]
        ready.sort(key=lambda node: (-weights[node.node_id], node.node_id))
        selected: list[TaskNode] = []
        for candidate in ready:
            if len(selected) >= max_concurrency:
                break
            if any(
                write_scopes_conflict(
                    candidate.predicted_write_set, chosen.predicted_write_set
                )
                for chosen in selected
            ):
                continue
            selected.append(candidate)
        # A valid TaskGraph always has at least one ready node.  Keep this
        # guard so the preview cannot loop forever if called with a future
        # graph implementation that weakens that invariant.
        if not selected:
            break
        wave = tuple(node.node_id for node in selected)
        waves.append(wave)
        completed.update(wave)
        remaining.difference_update(wave)
    return tuple(waves)


def _unordered_write_conflicts(graph: TaskGraph) -> tuple[tuple[str, str], ...]:
    conflicts: list[tuple[str, str]] = []
    nodes = list(graph.nodes)
    descendants = {node.node_id: graph.descendants(node.node_id) for node in nodes}
    for index, left in enumerate(nodes):
        for right in nodes[index + 1 :]:
            ordered = (
                right.node_id in descendants[left.node_id]
                or left.node_id in descendants[right.node_id]
            )
            if not ordered and write_scopes_conflict(
                left.predicted_write_set, right.predicted_write_set
            ):
                conflicts.append(tuple(sorted((left.node_id, right.node_id))))
    return tuple(sorted(set(conflicts)))


def validate_task_graph(
    graph: TaskGraph,
    *,
    budget: ScheduleBudget | None = None,
    max_concurrency: int = 4,
    source: str | None = None,
) -> DAGValidationReport:
    """Validate execution contracts and return a static scheduling preview.

    This function performs no I/O.  The graph has already passed Pydantic's
    structural checks for ids, roles, cycles and write-scope safety.
    """

    if max_concurrency < 1:
        raise ValueError("max_concurrency must be at least one")
    resolved_budget = budget or resolve_schedule_budget(graph)
    diagnostics: list[DAGDiagnostic] = []
    by_id = graph.by_id

    for index, node in enumerate(graph.nodes):
        blocking = [
            criterion
            for criterion in node.acceptance_criteria
            if criterion.blocking
            and criterion.verifier == "deterministic"
            and criterion.verifier_argv
        ]
        if not blocking:
            diagnostics.append(
                DAGDiagnostic(
                    code="dag.acceptance.blocking_required",
                    severity=DiagnosticSeverity.ERROR,
                    message=(
                        "production nodes require at least one blocking deterministic "
                        "acceptance criterion"
                    ),
                    path=f"$.nodes[{index}].acceptance_criteria",
                    node_id=node.node_id,
                    remediation=(
                        "Add a blocking deterministic criterion with a non-empty "
                        "verifier_argv."
                    ),
                )
            )

        dependency_outputs: dict[str, list[str]] = {}
        for dependency_id in node.depends_on:
            dependency = by_id[dependency_id]
            for output_name in dependency.artifact_contract.required_outputs:
                dependency_outputs.setdefault(output_name, []).append(dependency_id)
        for input_index, input_name in enumerate(
            node.artifact_contract.required_inputs
        ):
            suppliers = dependency_outputs.get(input_name, [])
            if not suppliers:
                diagnostics.append(
                    DAGDiagnostic(
                        code="dag.artifact.missing_dependency_output",
                        severity=DiagnosticSeverity.ERROR,
                        message=(
                            f"required input artifact {input_name!r} is not declared "
                            "by a direct dependency"
                        ),
                        path=(
                            f"$.nodes[{index}].artifact_contract.required_inputs"
                            f"[{input_index}]"
                        ),
                        node_id=node.node_id,
                        remediation=(
                            "Declare the artifact as a required output of a direct "
                            "dependency, or update depends_on."
                        ),
                    )
                )
            elif len(suppliers) > 1:
                diagnostics.append(
                    DAGDiagnostic(
                        code="dag.artifact.ambiguous_dependency_output",
                        severity=DiagnosticSeverity.WARNING,
                        message=(
                            f"required input artifact {input_name!r} is declared by "
                            f"multiple dependencies: {', '.join(sorted(suppliers))}"
                        ),
                        path=(
                            f"$.nodes[{index}].artifact_contract.required_inputs"
                            f"[{input_index}]"
                        ),
                        node_id=node.node_id,
                        remediation="Use unique artifact names when producer identity matters.",
                    )
                )

        if node.token_budget > resolved_budget.total_tokens:
            diagnostics.append(
                DAGDiagnostic(
                    code="dag.budget.node_reservation_exceeds_total",
                    severity=DiagnosticSeverity.WARNING,
                    message=(
                        f"node reserves {node.token_budget} tokens but the run budget "
                        f"is {resolved_budget.total_tokens}; it cannot be dispatched"
                    ),
                    path=f"$.nodes[{index}].token_budget",
                    node_id=node.node_id,
                    remediation="Raise the run budget or lower this node reservation.",
                )
            )

    configured_tokens = sum(node.token_budget for node in graph.nodes)
    if configured_tokens > resolved_budget.total_tokens:
        diagnostics.append(
            DAGDiagnostic(
                code="dag.budget.reservation_pressure",
                severity=DiagnosticSeverity.WARNING,
                message=(
                    f"declared node reservations total {configured_tokens} tokens, above "
                    f"the run budget {resolved_budget.total_tokens}; later dispatches "
                    "depend on unused reservations returned by completed nodes"
                ),
                path="$.budget.total_tokens",
                remediation=(
                    "Review per-node reservations. Actual usage is reconciled only when "
                    "a node completes, and one in-flight request can overshoot."
                ),
            )
        )

    if graph.critical_path_seconds > resolved_budget.wall_time_seconds:
        diagnostics.append(
            DAGDiagnostic(
                code="dag.budget.estimated_critical_path_exceeds_wall_time",
                severity=DiagnosticSeverity.WARNING,
                message=(
                    f"estimated critical path is {graph.critical_path_seconds:g}s, above "
                    f"the wall-time budget {resolved_budget.wall_time_seconds:g}s"
                ),
                path="$.budget.wall_time_seconds",
                remediation=(
                    "Increase wall-time or reduce estimates; estimates are planning hints, "
                    "not runtime guarantees."
                ),
            )
        )

    conflicts = _unordered_write_conflicts(graph)
    for left, right in conflicts:
        diagnostics.append(
            DAGDiagnostic(
                code="dag.write_set.concurrent_conflict",
                severity=DiagnosticSeverity.INFO,
                message=(
                    f"nodes {left!r} and {right!r} have overlapping write scopes and "
                    "will be serialized if ready together"
                ),
                path="$.nodes",
                remediation="Split write scopes if safe parallel execution is desired.",
            )
        )

    weights = graph.critical_path_weights()
    priority = tuple(
        node.node_id
        for node in sorted(
            graph.nodes, key=lambda item: (-weights[item.node_id], item.node_id)
        )
    )
    preview = DAGSchedulePreview(
        node_count=len(graph.nodes),
        max_concurrency=max_concurrency,
        dispatch_waves=_dispatch_waves(graph, max_concurrency=max_concurrency),
        priority_order=priority,
        nodes=tuple(
            DAGNodePreview(
                node_id=node.node_id,
                role=node.role,
                depends_on=node.depends_on,
                critical_path_weight_seconds=weights[node.node_id],
                token_reservation=node.token_budget,
                timeout_seconds=node.timeout_seconds,
                predicted_write_set=node.predicted_write_set,
            )
            for node in graph.nodes
        ),
        potential_write_conflicts=conflicts,
        critical_path_seconds=graph.critical_path_seconds,
        configured_token_reservations=configured_tokens,
        total_token_budget=resolved_budget.total_tokens,
        configured_timeout_seconds=sum(node.timeout_seconds for node in graph.nodes),
        wall_time_budget_seconds=resolved_budget.wall_time_seconds,
    )
    valid = not any(
        diagnostic.severity is DiagnosticSeverity.ERROR for diagnostic in diagnostics
    )
    return DAGValidationReport(
        valid=valid,
        source=source,
        diagnostics=tuple(diagnostics),
        preview=preview,
    )


def validate_dag_file(
    path: str | os.PathLike[str],
    *,
    max_concurrency: int = 4,
    total_tokens: int | None = None,
    wall_time_seconds: float | None = None,
) -> DAGValidationReport:
    """Read and validate a graph without Providers or workspace mutation."""

    source = Path(path).expanduser()
    source_label = str(source.resolve(strict=False))
    try:
        payload = source.read_text(encoding="utf-8")
    except OSError as exc:
        return DAGValidationReport(
            valid=False,
            source=source_label,
            diagnostics=(
                DAGDiagnostic(
                    code="dag.file.unreadable",
                    severity=DiagnosticSeverity.ERROR,
                    message=f"{type(exc).__name__}: {exc}",
                    remediation="Check that the DAG file exists and is readable.",
                ),
            ),
        )
    try:
        raw = json.loads(payload)
    except json.JSONDecodeError as exc:
        return DAGValidationReport(
            valid=False,
            source=source_label,
            diagnostics=(
                DAGDiagnostic(
                    code="dag.json.invalid",
                    severity=DiagnosticSeverity.ERROR,
                    message=f"{exc.msg} at line {exc.lineno}, column {exc.colno}",
                    path="$",
                    remediation="Correct the JSON syntax and validate again.",
                ),
            ),
        )
    try:
        graph = TaskGraph.model_validate(raw)
    except ValidationError as exc:
        return DAGValidationReport(
            valid=False,
            source=source_label,
            diagnostics=_schema_diagnostics(exc),
        )
    try:
        budget = resolve_schedule_budget(
            graph,
            total_tokens=total_tokens,
            wall_time_seconds=wall_time_seconds,
        )
    except ValidationError as exc:
        budget_findings: list[DAGDiagnostic] = []
        for diagnostic in _schema_diagnostics(exc):
            suffix = diagnostic.path.removeprefix("$")
            budget_findings.append(
                diagnostic.model_copy(
                    update={
                        "code": diagnostic.code.replace(
                            "dag.schema", "dag.budget"
                        ),
                        "path": f"$.budget{suffix}",
                        "remediation": (
                            "Use a positive total_tokens integer and a positive "
                            "wall_time_seconds value."
                        ),
                    }
                )
            )
        return DAGValidationReport(
            valid=False,
            source=source_label,
            diagnostics=tuple(budget_findings),
        )
    try:
        return validate_task_graph(
            graph,
            budget=budget,
            max_concurrency=max_concurrency,
            source=source_label,
        )
    except ValueError as exc:
        return DAGValidationReport(
            valid=False,
            source=source_label,
            diagnostics=(
                DAGDiagnostic(
                    code="dag.schedule.invalid",
                    severity=DiagnosticSeverity.ERROR,
                    message=str(exc),
                    path="$.max_concurrency",
                    remediation="Use max_concurrency >= 1.",
                ),
            ),
        )


__all__ = [
    "DAGDiagnostic",
    "DAGNodePreview",
    "DAGSchedulePreview",
    "DAGValidationReport",
    "DiagnosticSeverity",
    "resolve_schedule_budget",
    "validate_dag_file",
    "validate_task_graph",
]

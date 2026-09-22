"""Versioned, strict contracts for offline graphs and node results."""
from __future__ import annotations

from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

Role = Literal["explorer", "implementer", "verifier", "integrator"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class InputRef(Contract):
    node_id: str
    role: Role


class CommandSpec(Contract):
    """Exact, operator-declared argv invocation; not an OS sandbox."""
    argv: list[str] = Field(min_length=1)
    timeout: int = Field(default=120, ge=1, le=600)

    @field_validator("argv")
    @classmethod
    def valid_argv(cls, argv: list[str]) -> list[str]:
        if not argv[0] or any("\x00" in arg for arg in argv):
            raise ValueError("argv requires a nonempty executable and no NUL bytes")
        return argv


class NodeSpec(Contract):
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
    role: Role
    goal: str = Field(min_length=1)
    depends_on: list[str] = Field(default_factory=list)
    inputs: list[InputRef] = Field(default_factory=list)
    read_set: list[str] = Field(default_factory=lambda: ["."])
    write_set: list[str] = Field(default_factory=list)
    commands: list[CommandSpec] = Field(default_factory=list)
    max_iterations: int = Field(default=25, ge=1, le=200)
    timeout: int = Field(default=300, ge=1, le=3600)

    @field_validator("read_set", "write_set")
    @classmethod
    def safe_scopes(cls, values: list[str]) -> list[str]:
        from pathlib import PurePosixPath, PureWindowsPath
        for value in values:
            posix = PurePosixPath(value.replace("\\", "/"))
            windows = PureWindowsPath(value)
            if (not value or posix.is_absolute() or windows.drive or
                    ".." in posix.parts or any(c in value for c in "*?[]\x00")):
                raise ValueError("Scopes must be relative literal file or directory paths")
            if posix.parts and posix.parts[0].lower() in {".git", ".eviforge"}:
                raise ValueError("Runtime and Git metadata are reserved")
        return values

    @property
    def writable(self) -> bool:
        return bool(self.write_set or self.commands)


class GraphSpec(Contract):
    schema_version: Literal["1.0"] = "1.0"
    name: str = Field(default="workflow", min_length=1)
    max_concurrency: int = Field(default=4, ge=1, le=32)
    nodes: list[NodeSpec] = Field(min_length=1)


class ArtifactRef(Contract):
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int = Field(ge=0)
    path: str
    run_id: str
    node_id: str
    attempt: int = Field(ge=1)


class ExplorerOutput(Contract):
    role: Literal["explorer"]
    summary: str
    findings: list[str]
    evidence: list[ArtifactRef] = Field(default_factory=list)


class ImplementerOutput(Contract):
    role: Literal["implementer"]
    summary: str
    changes: list[str]
    unresolved: list[str] = Field(default_factory=list)
    evidence: list[ArtifactRef] = Field(default_factory=list)


class CheckResult(Contract):
    name: str
    passed: bool
    details: str


class VerifierOutput(Contract):
    role: Literal["verifier"]
    summary: str
    implementation_refs: list[str] = Field(min_length=1)
    verdict: Literal["pass", "fail"]
    checks: list[CheckResult] = Field(min_length=1)
    evidence: list[ArtifactRef] = Field(default_factory=list)


class IntegratorOutput(Contract):
    role: Literal["integrator"]
    summary: str
    implementation_refs: list[str] = Field(min_length=1)
    verification_refs: list[str] = Field(min_length=1)
    changes: list[str] = Field(default_factory=list)
    evidence: list[ArtifactRef] = Field(default_factory=list)


RoleOutput = Annotated[ExplorerOutput | ImplementerOutput | VerifierOutput | IntegratorOutput,
                       Field(discriminator="role")]
OUTPUT_ADAPTER = TypeAdapter(RoleOutput)
OUTPUT_MODELS = {"explorer": ExplorerOutput, "implementer": ImplementerOutput,
                 "verifier": VerifierOutput, "integrator": IntegratorOutput}


class ExplorerInput(Contract):
    role: Literal["explorer"]
    goal: str
    prior_findings: dict[str, ExplorerOutput] = Field(default_factory=dict)


class ImplementerInput(Contract):
    role: Literal["implementer"]
    goal: str
    explorations: dict[str, ExplorerOutput] = Field(default_factory=dict)
    prior_implementations: dict[str, ImplementerOutput] = Field(default_factory=dict)
    verification_feedback: dict[str, VerifierOutput] = Field(default_factory=dict)


class VerifierInput(Contract):
    role: Literal["verifier"]
    goal: str
    implementations: dict[str, ImplementerOutput] = Field(min_length=1)
    explorations: dict[str, ExplorerOutput] = Field(default_factory=dict)


class IntegratorInput(Contract):
    role: Literal["integrator"]
    goal: str
    implementations: dict[str, ImplementerOutput] = Field(min_length=1)
    verifications: dict[str, VerifierOutput] = Field(min_length=1)


RoleInput = Annotated[ExplorerInput | ImplementerInput | VerifierInput | IntegratorInput,
                      Field(discriminator="role")]
INPUT_ADAPTER = TypeAdapter(RoleInput)
INPUT_MODELS = {"explorer": ExplorerInput, "implementer": ImplementerInput,
                "verifier": VerifierInput, "integrator": IntegratorInput}
INPUT_ROLE_FIELDS = {"explorer": {"explorer": "prior_findings"},
    "implementer": {"explorer": "explorations", "implementer": "prior_implementations", "verifier": "verification_feedback"},
    "verifier": {"implementer": "implementations", "explorer": "explorations"},
    "integrator": {"implementer": "implementations", "verifier": "verifications"}}


def build_role_input(node: NodeSpec, inputs: dict[str, RoleOutput]) -> RoleInput:
    data = {"role": node.role, "goal": node.goal}
    for source, output in inputs.items():
        field = INPUT_ROLE_FIELDS[node.role][output.role]
        data.setdefault(field, {})[source] = output
    return INPUT_MODELS[node.role].model_validate(data)


class NodeResult(Contract):
    status: Literal["completed", "failed", "ambiguous", "blocked", "cancelled", "pending"]
    output: RoleOutput | None = None
    error: str = ""


class DAGRunResult(Contract):
    schema_version: Literal["1.0"] = "1.0"
    run_id: str
    status: Literal["success", "failed", "ambiguous", "cancelled"]
    nodes: dict[str, NodeResult]

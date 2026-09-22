"""A DAG node runs the existing Agent and the existing file/command tools."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from pydantic import BaseModel
from eviforge.agent import Agent
from eviforge.agents.tool_filter import clone_agent_registry
from eviforge.conversation import ConversationManager
from eviforge.dag.artifacts import ArtifactStore
from eviforge.dag.capabilities import (NodePermissionChecker, ROLE_PROMPTS, ROLE_TOOLS,
                                       ScopedTool, scoped_path)
from eviforge.dag.graph import DAGError
from eviforge.dag.models import (Contract, NodeSpec, OUTPUT_MODELS, RoleOutput,
                                 VerifierOutput, IntegratorOutput, build_role_input)
from eviforge.tools import ToolRegistry
from eviforge.tools.base import Tool, ToolResult


@dataclass
class NodeContext:
    node: NodeSpec
    root: Path
    run_id: str
    attempt: int
    inputs: dict[str, RoleOutput]
    journal: Any
    artifacts: ArtifactStore
    guard: Callable[[], None]
    submitted: RoleOutput | None = None
    writes: set[str] = field(default_factory=set)
    commands: list[dict] = field(default_factory=list)
    reads: int = 0
    mutation_error: bool = False
    pending_effects: int = 0
    audit_error: bool = False

    def event(self, kind: str, data: dict) -> None:
        try:
            self.journal.append_event(self.node.id, self.attempt, kind, data)
        except BaseException:
            # The Agent can convert tool exceptions into model-visible errors.
            # Preserve the audit failure independently of that recoverable text.
            self.audit_error = True
            raise

    def record_artifact(self, ref: Any) -> None:
        try:
            self.journal.artifact(ref)
        except BaseException:
            self.audit_error = True
            raise

    def validate_output(self, output: RoleOutput) -> None:
        if self.audit_error or self.pending_effects:
            raise DAGError("Execution or audit persistence is incomplete; effects require inspection")
        if self.mutation_error:
            raise DAGError("A file mutation failed; its effects require inspection")
        if output.role != self.node.role:
            raise DAGError("Output role mismatch")
        for ref in output.evidence:
            if (ref.run_id != self.run_id or ref.node_id != self.node.id or
                    ref.attempt != self.attempt or not self.journal.has_artifact(ref)):
                raise DAGError("Evidence producer or ownership mismatch")
            self.artifacts.verify(ref)
        changes = getattr(output, "changes", [])
        normalized = {scoped_path(p, self.node.write_set, self.root, write=True).relative_to(self.root).as_posix()
                      for p in changes}
        if normalized != self.writes:
            raise DAGError("Reported changes do not match actual file writes")
        evidence = {ref.path: ref for ref in output.evidence}
        for path in self.writes:
            if path not in evidence:
                raise DAGError("Every written file requires captured evidence")
            if hashlib.sha256((self.root / path).read_bytes()).hexdigest() != evidence[path].sha256:
                raise DAGError("File changed after evidence capture")
        if isinstance(output, VerifierOutput):
            if not self.commands and not self.reads:
                raise DAGError("Verifier requires actual inspection or command execution")
            expected = {key for key, value in self.inputs.items() if value.role == "implementer"}
            if set(output.implementation_refs) != expected:
                raise DAGError("Verifier must cover every supplied implementation")
            if output.verdict == "pass" and (not all(c.passed for c in output.checks) or
                                               any(not c["success"] for c in self.commands)):
                raise DAGError("Passing verification contradicts check evidence")
            if any(not any(c["argv"] == spec.argv and c["timeout"] == spec.timeout
                           for c in self.commands) for spec in self.node.commands):
                raise DAGError("Declared verification commands were not all executed")
        if isinstance(output, IntegratorOutput):
            impl = {key for key, value in self.inputs.items() if value.role == "implementer"}
            verifiers = {key: value for key, value in self.inputs.items() if value.role == "verifier"}
            if set(output.implementation_refs) != impl or set(output.verification_refs) != set(verifiers):
                raise DAGError("Integrator references differ from supplied inputs")
            covered = set()
            for verifier in verifiers.values():
                if verifier.verdict != "pass":
                    raise DAGError("Cannot integrate failed verification")
                covered.update(verifier.implementation_refs)
            if not impl <= covered:
                raise DAGError("Implementations lack passing verification")


class CaptureParams(Contract):
    file_path: str


class CaptureArtifact(Tool):
    name = "CaptureArtifact"
    description = "Capture an allowed file as immutable SHA-256 evidence; use the exact returned reference in SubmitNodeResult."
    params_model = CaptureParams
    category = "read"

    def __init__(self, context: NodeContext):
        self.context = context

    async def execute(self, params: CaptureParams) -> ToolResult:
        ctx = self.context
        ctx.guard()
        if ctx.submitted is not None:
            raise DAGError("Evidence cannot be captured after final submission")
        path = scoped_path(params.file_path, ctx.node.read_set + ctx.node.write_set, ctx.root)
        ref = ctx.artifacts.capture(path, relative_path=path.relative_to(ctx.root).as_posix(),
                                    run_id=ctx.run_id, node_id=ctx.node.id, attempt=ctx.attempt)
        ctx.record_artifact(ref)
        return ToolResult(output=ref.model_dump_json())


class SubmitNodeResult(Tool):
    name = "SubmitNodeResult"
    description = "Submit the final typed node result exactly once, after all work and evidence capture. Then finish without further tool calls."
    category = "read"

    def __init__(self, context: NodeContext):
        self.context = context
        self.params_model = OUTPUT_MODELS[context.node.role]

    async def execute(self, params: BaseModel) -> ToolResult:
        ctx = self.context
        ctx.guard()
        if ctx.submitted is not None:
            raise DAGError("Node result was already submitted")
        # Receipt hashes are computed by the executor, never invented by a model.
        receipts = [command["evidence"] for command in ctx.commands]
        if receipts:
            params = params.model_copy(update={"evidence": list(params.evidence) + receipts})
        ctx.validate_output(params)
        ctx.event("result_submitted", params.model_dump())
        ctx.submitted = params
        return ToolResult(output="Typed node result accepted; finish now.")


class AgentNodeRunner:
    def __init__(self, parent: Agent):
        self.parent = parent

    async def run(self, node: NodeSpec, context: NodeContext) -> RoleOutput:
        parent = self.parent
        typed_input = build_role_input(node, context.inputs)
        tools = [tool for tool in parent.registry.list_tools()
                 if tool.name in ROLE_TOOLS[node.role] and parent.registry.is_enabled(tool.name)
                 and (tool.name != "Bash" or node.commands)]
        registry = ToolRegistry()
        for tool in clone_agent_registry(parent.registry, tools).list_tools():
            registry.register(ScopedTool(tool, context))
        registry.register(CaptureArtifact(context))
        registry.register(SubmitNodeResult(context))
        child = Agent(client=parent.client, registry=registry, protocol=parent.protocol,
                      work_dir=str(context.root), max_iterations=node.max_iterations,
                      context_window=parent.context_window,
                      permission_checker=NodePermissionChecker(parent, node, context.root, context.guard),
                      query_source="dag", system_prompt_override=ROLE_PROMPTS[node.role])
        child.parent_id = parent.agent_id
        child.trace_id = parent.trace_id or context.run_id
        parent.bind_child(child)
        child.memory_manager = None
        child.skill_loader = None
        child.instructions_content = ""
        child.hook_engine = None  # Arbitrary hook side effects are not DAG capabilities.
        child.notification_fn = None
        child.runtime = None  # Do not inject dynamic memory/skills outside the hashed node input.
        context.event("node_agent_created", {"agent_id": child.agent_id, "parent_id": child.parent_id,
                       "trace_id": child.trace_id, "role": node.role, "depends_on": node.depends_on})
        prompt = json.dumps({"node_id": node.id, "role": node.role, "goal": node.goal,
                             "read_set": node.read_set, "write_set": node.write_set,
                             "commands": [c.model_dump() for c in node.commands],
                             "inputs": typed_input.model_dump(),
                             "completion": "Capture evidence, call SubmitNodeResult, then finish."},
                            ensure_ascii=False, sort_keys=True)
        try:
            await child.run_to_completion(prompt, ConversationManager(),
                                          event_callback=lambda event: context.event("agent_event", event))
            if child.last_run_status != "success":
                raise DAGError(child.last_run_error or "Agent did not complete successfully")
            if context.submitted is None:
                raise DAGError("Agent completed without a typed result submission")
            context.validate_output(context.submitted)
            return context.submitted
        finally:
            await child.cancel_background()

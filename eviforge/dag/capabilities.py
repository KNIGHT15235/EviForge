"""Narrowed tools preserve their names and the Agent's final approval gate."""
from __future__ import annotations

import hashlib
import inspect
import json
from pathlib import Path
from typing import Any
from eviforge.dag.graph import DAGError, DriftError, canonical_hash, within
from eviforge.dag.models import NodeSpec, OUTPUT_MODELS, INPUT_MODELS
from eviforge.permissions import Decision
from eviforge.tools.base import Tool, ToolResult

READ_TOOLS = {"ReadFile", "Glob", "Grep"}
ROLE_TOOLS = {"explorer": READ_TOOLS, "implementer": READ_TOOLS | {"WriteFile", "EditFile"},
              "verifier": READ_TOOLS | {"Bash"}, "integrator": READ_TOOLS | {"WriteFile", "EditFile"}}
ROLE_PROMPTS = {
    "explorer": "Explore the supplied objective and report concrete findings with file evidence. Do not modify files.",
    "implementer": "Implement only the declared objective and write scopes. Read existing files before editing. Capture changed files as evidence.",
    "verifier": "Verify the supplied implementations using file inspection and only the exact approved argv invocations. Report failures honestly.",
    "integrator": "Integrate the supplied implementations only after their Verifier outputs pass. Restrict edits to declared write scopes and capture evidence.",
}


def capability_hash(parent: Any, nodes: list[NodeSpec]) -> str:
    checker = parent.permission_checker
    rules = getattr(checker, "rule_engine", None)
    tiers = rules._load_tiers() if rules else []
    inherited = getattr(rules, "_inherited_denials", ())
    tools = []
    for tool in parent.registry.list_tools():
        if tool.name not in set().union(*ROLE_TOOLS.values()):
            continue
        source = inspect.getsourcefile(type(tool))
        tools.append({"schema": tool.get_schema(), "enabled": parent.registry.is_enabled(tool.name),
                      "implementation": hashlib.sha256(Path(source).read_bytes()).hexdigest() if source else type(tool).__qualname__})
    return canonical_hash({"version": "1.0", "roles": ROLE_PROMPTS,
        "outputs": {role: model.model_json_schema() for role, model in OUTPUT_MODELS.items()},
        "inputs": {role: model.model_json_schema() for role, model in INPUT_MODELS.items()},
        "tools": sorted(tools, key=lambda t: t["schema"]["name"]),
        "mode": str(parent.permission_mode), "checker_mode": str(getattr(checker, "mode", None)),
        "work_dir": str(Path(parent.work_dir).resolve()),
        "rules": [[vars(r) for r in tier] for tier in tiers],
        "inherited_denials": [vars(r) for r in inherited],
        "sandbox_roots": [str(p) for p in getattr(getattr(checker, "sandbox", None), "_allowed_roots", [])],
        "nodes": [n.model_dump() for n in sorted(nodes, key=lambda n: n.id)],
        "adapter": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()})


def scoped_path(raw: str, scopes: list[str], root: Path, *, write: bool = False) -> Path:
    path = Path(raw)
    path = (root / path).resolve() if not path.is_absolute() else path.resolve()
    # Lock conflicts may conservatively case-fold; authorization must use the
    # actual filesystem path semantics (case-folding would escape Linux roots).
    if not path.is_relative_to(root):
        raise DAGError("DAG path escapes the workspace")
    if not any(path.is_relative_to((root / s).resolve()) for s in scopes):
        raise DAGError("DAG path is outside declared capabilities")
    if write and any(within(path, root / reserved) for reserved in (".git", ".eviforge")):
        raise DAGError("DAG cannot write runtime or Git metadata")
    return path


class NodePermissionChecker:
    def __init__(self, parent: Any, node: NodeSpec, root: Path, guard: Any):
        self.parent = parent
        self.node = node
        self.root = root
        self.guard = guard
        self.mode = parent.permission_mode
        self.rule_engine = getattr(parent.permission_checker, "rule_engine", None)
        self.sandbox = getattr(parent.permission_checker, "sandbox", None)
        self.plan_file_path = ""

    def check(self, tool: Tool, arguments: dict) -> Decision:
        try:
            self.guard()
            if tool.name == "SubmitNodeResult":
                return Decision(effect="allow", reason="DAG result/evidence operation")
            if tool.name == "CaptureArtifact":
                scoped_path(arguments["file_path"], self.node.read_set + self.node.write_set, self.root)
                # Evidence capture is a real file read, so an explicit parent
                # ReadFile denial or disabled reader also constrains it.
                if not self.parent.registry.is_enabled("ReadFile"):
                    raise DAGError("Evidence capture requires parent ReadFile capability")
                if self.parent.permission_checker:
                    reader = self.parent.registry.get("ReadFile")
                    return self.parent.permission_checker.check(reader, arguments)
                return Decision(effect="allow", reason="DAG evidence read")
            check_capability(tool.name, arguments, self.node, self.root)
        except DAGError as exc:
            return Decision(effect="deny", reason=str(exc))
        if self.parent.permission_checker:
            decision = self.parent.permission_checker.check(tool, arguments)
            # Preserve ASK so the common Agent gate can satisfy it with an
            # exact plan grant. Without a grant the noninteractive loop denies it.
            if decision.effect != "allow":
                return decision
        return Decision(effect="allow", reason="Within parent and DAG capabilities")


def check_capability(name: str, arguments: dict, node: NodeSpec, root: Path) -> None:
    if name not in ROLE_TOOLS[node.role]:
        raise DAGError("Tool is outside role capabilities")
    if name == "Bash":
        if arguments.get("command") is not None:
            raise DAGError("DAG requires exact argv; shell strings are forbidden")
        if not any(arguments.get("argv") == c.argv and arguments.get("timeout", 120) == c.timeout
                   for c in node.commands):
            raise DAGError("Command arguments differ from the declared invocation")
        return
    write = name in {"WriteFile", "EditFile"}
    raw = arguments.get("file_path") if name in {"ReadFile", "WriteFile", "EditFile"} else arguments.get("path", ".")
    if not isinstance(raw, str):
        raise DAGError("File path must be a string")
    scoped_path(raw, node.write_set if write else node.read_set, root, write=write)


class ScopedTool(Tool):
    def __init__(self, original: Tool, context: Any):
        self.original = original
        self.context = context
        for attr in ("name", "description", "params_model", "category", "is_concurrency_safe", "is_system_tool"):
            setattr(self, attr, getattr(original, attr))

    async def execute(self, params: Any) -> ToolResult:
        ctx = self.context
        ctx.guard()
        if ctx.audit_error or ctx.pending_effects or ctx.mutation_error:
            raise DAGError("Node has unresolved execution or audit effects; automatic continuation is forbidden")
        if ctx.submitted is not None:
            raise DAGError("Tools cannot execute after final node submission")
        args = params.model_dump()
        check_capability(self.name, args, ctx.node, ctx.root)
        ctx.event("tool_started", {"tool": self.name, "arguments": args})
        has_effects = self.name in {"WriteFile", "EditFile", "Bash"}
        if has_effects:
            # Never clear this marker in finally: an exception/cancellation may
            # follow a real side effect before its completion is durable.
            ctx.pending_effects += 1
        result = await self.original.execute(params)
        if self.name in {"WriteFile", "EditFile"} and result.is_error:
            ctx.mutation_error = True
        if self.name in {"WriteFile", "EditFile"} and not result.is_error:
            path = scoped_path(args["file_path"], ctx.node.write_set, ctx.root, write=True)
            ctx.writes.add(path.relative_to(ctx.root).as_posix())
        if self.name in READ_TOOLS and not result.is_error:
            ctx.reads += 1
        if self.name == "Bash":
            receipt = json.dumps({"argv": args["argv"], "timeout": args["timeout"],
                                  "is_error": result.is_error, "output": result.output}, sort_keys=True).encode()
            evidence = ctx.artifacts.capture_bytes(receipt,
                relative_path=f"@commands/{len(ctx.commands)}.json", run_id=ctx.run_id,
                node_id=ctx.node.id, attempt=ctx.attempt)
            ctx.record_artifact(evidence)
            ctx.commands.append({"argv": args["argv"], "timeout": args["timeout"],
                                 "success": not result.is_error, "evidence": evidence})
        # Account for the actual effect before attempting fallible persistence.
        ctx.event("tool_finished", {"tool": self.name, "is_error": result.is_error,
                                     "output": result.output})
        if has_effects:
            ctx.pending_effects -= 1
        return result

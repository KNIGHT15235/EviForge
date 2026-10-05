"""Version-bound plan approval and an in-process, single-use execution gate."""
from __future__ import annotations

import json
import math
import re
import threading
import time
import uuid
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from eviforge.permissions.capabilities import (
    ApprovalGrant, GateDecision, PlanAction, canonical_path, content_digest,
    execution_intent, normalize_action, within,
)
from eviforge.planning.audit import PlanAudit


class PlanError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


class PlanState(str, Enum):
    DRAFT = "draft"
    SUBMITTED = "submitted"
    APPROVED = "approved"
    EXECUTING = "executing"
    COMPLETED = "completed"
    REJECTED = "rejected"
    INVALIDATED = "invalidated"
    EXPIRED = "expired"
    FAILED = "failed"


@dataclass(frozen=True)
class PlanSession:
    plan_id: str
    session_id: str
    source_turn_id: str
    content: str
    actions: tuple[PlanAction, ...]
    content_hash: str
    version: int = 1
    state: PlanState = PlanState.DRAFT
    execution_turn_id: str | None = None
    approved_agent_id: str | None = None
    expires_at: float | None = None
    plan_path: str | None = None
    created_at: float = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1, "plan_id": self.plan_id, "session_id": self.session_id,
            "source_turn_id": self.source_turn_id, "execution_turn_id": self.execution_turn_id,
            "content": self.content, "actions": [action.as_dict() for action in self.actions],
            "content_hash": self.content_hash, "version": self.version, "state": self.state.value,
            "approved_agent_id": self.approved_agent_id, "expires_at": self.expires_at,
            "plan_path": self.plan_path, "created_at": self.created_at,
        }


class PlanService:
    """Owns snapshots and grants; only trusted UI/CLI code may call ``approve``.

    Snapshots persist for inspection. Grants are deliberately never deserialized.
    In-process Agent tools cannot write the control files through built-in file
    tools. This does not protect against the OS user or arbitrary allowed code.
    """

    def __init__(self, work_dir: str | Path, *, clock: Callable[[], float] = time.time, tool_resolver: Callable | None = None) -> None:
        self.work_dir = str(Path(work_dir).resolve())
        self._directory = Path(self.work_dir) / ".eviforge" / "plans"
        self._clock = clock
        self._lock = threading.RLock()
        self._plans: dict[str, PlanSession] = {}
        self._grants: list[ApprovalGrant] = []
        # Agent labels can be replaced by a trace ID during child assembly.
        # Bind policy to the actual instance so a label change cannot remove
        # its gate; grants still require the exact approved public agent ID.
        self._active: dict[int, str] = {}
        self._bound_agents: dict[int, Any] = {}
        self._draft_owners: dict[str, int] = {}
        self._audit = PlanAudit(self._directory / "audit.jsonl")
        self.tool_resolver = tool_resolver

    @staticmethod
    def _hash(content: str, actions: tuple[PlanAction, ...]) -> str:
        return content_digest({"content": content, "actions": [action.as_dict() for action in actions]})

    def _event(self, event: str, plan: PlanSession, **fields: Any) -> None:
        self._audit.append(event, timestamp=self._clock(), plan_id=plan.plan_id,
                           session_id=plan.session_id, source_turn_id=plan.source_turn_id,
                           content_hash=plan.content_hash, state=plan.state.value, **fields)

    def _store(self, plan: PlanSession, event: str) -> PlanSession:
        self._directory.mkdir(parents=True, exist_ok=True)
        target = self._directory / f"{plan.plan_id}.json"
        temporary = self._directory / f".{plan.plan_id}.{uuid.uuid4().hex}.tmp"
        try:
            temporary.write_text(json.dumps(plan.as_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        self._plans[plan.plan_id] = plan
        self._event(event, plan)
        return plan

    def _actions(self, actions: list[dict[str, Any]] | tuple[PlanAction, ...]) -> tuple[PlanAction, ...]:
        normalized = tuple(action if isinstance(action, PlanAction) else normalize_action(action, self.work_dir, self.tool_resolver) for action in actions)
        for action in normalized:
            if not within(action.cwd, [self.work_dir]):
                raise PlanError("CWD_OUTSIDE_PROJECT", "Action cwd must stay in the project")
            if any(not within(path, [self.work_dir]) for path in (*action.read_paths, *action.write_paths)):
                raise PlanError("PATH_OUTSIDE_PROJECT", "Plan scopes cannot expand the project boundary")
            if any(self._protected(path) for path in action.write_paths):
                raise PlanError("CONTROL_PATH_DENIED", "Agent actions cannot modify approval/control records")
        return normalized

    def create(self, session_id: str, turn_id: str, content: str, actions: list[dict[str, Any]] | None = None, plan_path: str | Path | None = None) -> PlanSession:
        if not session_id or not turn_id:
            raise PlanError("BINDING_REQUIRED", "Plan requires a session and logical user turn")
        with self._lock:
            normalized = self._actions(actions or [])
            path = canonical_path(str(plan_path), self.work_dir) if plan_path is not None else None
            if path is not None and (not within(path, [self.work_dir]) or self._protected(path)):
                raise PlanError("CONTROL_PATH_DENIED", "Draft path must be an ordinary file within the project")
            plan = PlanSession(uuid.uuid4().hex, session_id, turn_id, content, normalized,
                               self._hash(content, normalized), plan_path=path, created_at=self._clock())
            return self._store(plan, "plan_created")

    def get(self, plan_id: str) -> PlanSession:
        if not re.fullmatch(r"[a-f0-9]{32}", plan_id):
            raise PlanError("INVALID_PLAN_ID", "Invalid plan identifier")
        with self._lock:
            if plan_id in self._plans:
                return self._plans[plan_id]
            try:
                raw = json.loads((self._directory / f"{plan_id}.json").read_text(encoding="utf-8"))
                if raw.pop("schema_version") != 1 or raw["plan_id"] != plan_id:
                    raise ValueError("Invalid snapshot schema or identity")
                restored = []
                for action in raw["actions"]:
                    if action.get("capability_fingerprint"):
                        snapshot = dict(action)
                        from eviforge.permissions.capabilities import canonical_json
                        snapshot["arguments_json"] = canonical_json(snapshot.pop("arguments"))
                        for key in ("read_paths", "write_paths", "network_hosts", "resource_ids"):
                            if key in snapshot:
                                snapshot[key] = tuple(snapshot[key])
                        restored.append(PlanAction(**snapshot))
                    else:
                        restored.append(normalize_action(action, self.work_dir))
                raw["actions"] = self._actions(tuple(restored))
                raw["state"] = PlanState(raw["state"])
                plan = PlanSession(**raw)
                if self._hash(plan.content, plan.actions) != plan.content_hash:
                    raise ValueError("Snapshot content hash does not match")
            except (OSError, ValueError, TypeError, KeyError) as exc:
                raise PlanError("INVALID_PLAN", str(exc)) from exc
            self._plans[plan_id] = plan
            return plan

    def list_plans(self) -> list[PlanSession]:
        return [self.get(path.stem) for path in sorted(self._directory.glob("*.json"))]

    def _revoke(self, plan_id: str) -> None:
        self._grants = [grant for grant in self._grants if grant.plan_id != plan_id]

    def update(self, plan_id: str, content: str, actions: list[dict[str, Any]] | None = None) -> PlanSession:
        with self._lock:
            plan = self.get(plan_id)
            if plan.state == PlanState.COMPLETED:
                raise PlanError("INVALID_TRANSITION", "Completed plans cannot be changed; create a new plan")
            normalized = self._actions(actions) if actions is not None else plan.actions
            self._revoke(plan_id)
            return self._store(replace(plan, content=content, actions=normalized,
                content_hash=self._hash(content, normalized), version=plan.version + 1,
                state=PlanState.DRAFT, execution_turn_id=None, approved_agent_id=None, expires_at=None), "plan_updated")

    def submit(self, plan_id: str, *, content: str | None = None, actions: list[dict[str, Any]] | None = None) -> PlanSession:
        with self._lock:
            plan = self.get(plan_id)
            if plan.state != PlanState.DRAFT:
                raise PlanError("INVALID_TRANSITION", "Only a draft can be submitted")
            if content is None and plan.plan_path is not None:
                try:
                    content = Path(plan.plan_path).read_text(encoding="utf-8")
                except OSError as exc:
                    raise PlanError("PLAN_FILE_MISSING", str(exc)) from exc
            if content is not None and content != plan.content or actions is not None:
                plan = self.update(plan_id, content if content is not None else plan.content, actions)
            if not plan.content.strip():
                raise PlanError("EMPTY_PLAN", "A plan must contain reviewable content")
            return self._store(replace(plan, state=PlanState.SUBMITTED), "plan_submitted")

    def _check_content(self, plan: PlanSession) -> None:
        if plan.plan_path is not None:
            try:
                content = Path(plan.plan_path).read_text(encoding="utf-8")
            except OSError:
                content = None
            if content != plan.content:
                self.invalidate(plan.plan_id, "plan file changed after submission")
                raise PlanError("PLAN_CHANGED", "Plan content changed; submit and approve the new version")

    def approve(self, plan_id: str, expected_hash: str, *, session_id: str, source_turn_id: str,
                execution_turn_id: str, agent_id: str, ttl_seconds: float = 300) -> PlanSession:
        with self._lock:
            plan = self.get(plan_id)
            if plan.state not in {PlanState.SUBMITTED, PlanState.APPROVED, PlanState.EXPIRED}:
                raise PlanError("INVALID_TRANSITION", "Only a submitted plan may be approved")
            if expected_hash != plan.content_hash:
                raise PlanError("PLAN_CHANGED", "Approval hash does not match the reviewed snapshot")
            if session_id != plan.session_id or source_turn_id != plan.source_turn_id:
                raise PlanError("BINDING_MISMATCH", "Approval is bound to another session or source turn")
            if not execution_turn_id or not agent_id:
                raise PlanError("BINDING_REQUIRED", "Execution turn and agent audience are required")
            if not math.isfinite(ttl_seconds) or not 0 < ttl_seconds <= 3600:
                raise PlanError("INVALID_EXPIRY", "Approval validity must be greater than zero and at most one hour")
            self._check_content(plan)
            for action in plan.actions:
                # Snapshot inspection does not confer authority. Revalidate
                # external adapters against this process's current registry.
                normalize_action(action.as_dict(), self.work_dir, self.tool_resolver)
            self._revoke(plan_id)
            expiry = self._clock() + ttl_seconds
            plan = self._store(replace(plan, state=PlanState.APPROVED, execution_turn_id=execution_turn_id,
                approved_agent_id=agent_id, expires_at=expiry), "plan_approved")
            self._grants.extend(ApprovalGrant(uuid.uuid4().hex, plan_id, plan.content_hash,
                session_id, source_turn_id, execution_turn_id, agent_id, action, expiry, action.uses) for action in plan.actions)
            return plan

    def reject(self, plan_id: str, reason: str = "user rejected") -> PlanSession:
        with self._lock:
            plan = self.get(plan_id)
            if plan.state == PlanState.COMPLETED:
                raise PlanError("INVALID_TRANSITION", "Completed plans cannot be rejected")
            self._revoke(plan_id)
            result = self._store(replace(plan, state=PlanState.REJECTED), "plan_rejected")
            self._event("rejection_reason", result, reason=reason)
            return result

    def invalidate(self, plan_id: str, reason: str = "context changed") -> PlanSession:
        with self._lock:
            plan = self.get(plan_id)
            self._revoke(plan_id)
            result = self._store(replace(plan, state=PlanState.INVALIDATED), "plan_invalidated")
            self._event("invalidation_reason", result, reason=reason)
            return result

    def finish(self, plan_id: str, *, success: bool = True) -> PlanSession:
        with self._lock:
            plan = self.get(plan_id)
            if plan.state not in {PlanState.APPROVED, PlanState.EXECUTING}:
                raise PlanError("INVALID_TRANSITION", "Only an approved/executing plan can finish")
            self._revoke(plan_id)
            return self._store(replace(plan, state=PlanState.COMPLETED if success else PlanState.FAILED), "plan_finished")

    def bind_agent(self, agent: Any) -> None:
        agent.plan_service = self
        self._bound_agents[id(agent)] = agent
        if not getattr(agent, "agent_id", ""):
            agent.agent_id = uuid.uuid4().hex
        if not getattr(agent, "session_id", ""):
            agent.session_id = uuid.uuid4().hex
        if not getattr(agent, "turn_id", ""):
            agent.turn_id = uuid.uuid4().hex

    attach_agent = bind_agent

    def inherit(self, parent: Any, child: Any) -> None:
        self.bind_agent(child)
        child.session_id, child.turn_id = parent.session_id, parent.turn_id
        with self._lock:
            if id(parent) in self._active:
                self._active[id(child)] = self._active[id(parent)]

    def current_plan(self, agent: Any) -> PlanSession | None:
        with self._lock:
            plan_id = self._active.get(id(agent))
            return self.get(plan_id) if plan_id else None

    def activate(self, agent: Any, plan_id: str, expected_hash: str) -> None:
        self.bind_agent(agent)
        with self._lock:
            plan = self.get(plan_id)
            if expected_hash != plan.content_hash:
                raise PlanError("PLAN_CHANGED", "Cannot activate a different plan version")
            if agent.session_id != plan.session_id:
                raise PlanError("BINDING_MISMATCH", "Cannot activate another session's plan")
            expected_turn = plan.execution_turn_id if plan.state in {PlanState.APPROVED, PlanState.EXECUTING} else plan.source_turn_id
            if agent.turn_id != expected_turn:
                raise PlanError("TURN_MISMATCH", "Cannot activate the plan in another turn")
            self._active[id(agent)] = plan_id
            if plan.state == PlanState.DRAFT:
                self._draft_owners.setdefault(plan_id, id(agent))

    def bind_draft(self, agent: Any, plan_id: str) -> None:
        self.activate(agent, plan_id, self.get(plan_id).content_hash)

    def begin_turn(self, agent: Any, *, turn_id: str | None = None, continue_plan: bool = False) -> str:
        self.bind_agent(agent)
        with self._lock:
            plan = self.current_plan(agent)
            if continue_plan:
                if plan is None or plan.execution_turn_id != (turn_id or agent.turn_id):
                    raise PlanError("TURN_MISMATCH", "Continuation must use the approved execution turn")
                agent.turn_id = plan.execution_turn_id
                return agent.turn_id
            new_turn = turn_id or uuid.uuid4().hex
            if plan is not None and new_turn != agent.turn_id:
                self.invalidate(plan.plan_id, "new logical user turn")
            agent.turn_id = new_turn
            return new_turn

    def clear_agent(self, agent: Any) -> None:
        with self._lock:
            plan = self.current_plan(agent)
            if plan is not None and plan.state not in {PlanState.COMPLETED, PlanState.REJECTED, PlanState.INVALIDATED, PlanState.FAILED}:
                self.invalidate(plan.plan_id, "left plan execution")
            self._active.pop(id(agent), None)

    def _protected(self, path: str) -> bool:
        relative = None
        for root in (Path(self.work_dir), Path.home()):
            try:
                relative = Path(path).relative_to(root / ".eviforge")
                break
            except ValueError:
                continue
        if relative is None:
            return False
        if not relative.parts:
            return True
        first = relative.parts[0]
        if first in {"governance", "approvals", "audit", "dag", "mcp", "integration"} or first.startswith(("permissions", "governance.sqlite3")):
            return True
        return first == "plans" and (len(relative.parts) == 1 or Path(path).suffix != ".md")

    def precheck(self, agent: Any, tool: Any, arguments: dict[str, Any], cwd: str) -> GateDecision | None:
        return self._gate(agent, tool, arguments, cwd, consume=False)

    def consume(self, agent: Any, tool: Any, arguments: dict[str, Any], cwd: str) -> GateDecision | None:
        return self._gate(agent, tool, arguments, cwd, consume=True)

    def _gate(self, agent: Any, tool: Any, arguments: dict[str, Any], cwd: str, *, consume: bool) -> GateDecision | None:
        with self._lock:
            if tool.name in {"WriteFile", "EditFile"}:
                try:
                    path = canonical_path(arguments.get("file_path", ""), cwd)
                    if self._protected(path):
                        return GateDecision(False, "CONTROL_PATH_DENIED", "Approval/control records cannot be modified by Agent tools")
                except (OSError, ValueError, RuntimeError):
                    return GateDecision(False, "INVALID_PATH", "Cannot resolve tool target")
            plan = self.current_plan(agent)
            if plan is None:
                return None

            def deny(code: str, message: str) -> GateDecision:
                self._event("tool_denied", plan, agent_id=agent.agent_id, tool_name=tool.name,
                            code=code, stage="execution" if consume else "precheck")
                return GateDecision(False, code, message)

            if agent.session_id != plan.session_id:
                return deny("SESSION_MISMATCH", "Plan belongs to another session")
            if plan.state in {PlanState.REJECTED, PlanState.INVALIDATED, PlanState.COMPLETED, PlanState.EXPIRED, PlanState.FAILED}:
                return deny("PLAN_INACTIVE", f"Plan is {plan.state.value}")
            planning = plan.state in {PlanState.DRAFT, PlanState.SUBMITTED}
            if agent.turn_id != (plan.source_turn_id if planning else plan.execution_turn_id):
                return deny("TURN_MISMATCH", "Plan belongs to another logical turn")
            if not planning:
                try:
                    self._check_content(plan)
                except PlanError as exc:
                    return deny(exc.code, str(exc))
                if plan.expires_at is None or self._clock() >= plan.expires_at:
                    self._revoke(plan.plan_id)
                    self._store(replace(plan, state=PlanState.EXPIRED), "plan_expired")
                    return deny("APPROVAL_EXPIRED", "Plan approval expired")

            # This tiny set contains no user-defined/external implementations.
            from eviforge.tools.exit_plan_mode import ExitPlanModeTool
            from eviforge.tools.impl.tool_search import ToolSearchTool
            from eviforge.tools.ask_user import AskUserTool
            from eviforge.tools.agent_tool import AgentTool
            from eviforge.dag.agent_runner import CaptureArtifact, SubmitNodeResult
            if type(tool) is AgentTool:
                subagent_type = arguments.get("subagent_type")
                if (plan.state == PlanState.DRAFT and isinstance(subagent_type, str)
                    and subagent_type.strip().lower() in {"explore", "plan"}
                    and not arguments.get("team_name") and not arguments.get("isolation")
                    and tool._parent_agent is agent):
                    return GateDecision(True, "PLAN_READ_DELEGATION", "Read-only planning child inherits this plan and receives no grants")
                return deny("PLAN_DELEGATION_DENIED", "Only read-only Explore/Plan delegation is allowed while drafting")
            if type(tool) in {ExitPlanModeTool, ToolSearchTool, AskUserTool, SubmitNodeResult}:
                return GateDecision(True, "PLAN_CONTROL", "Built-in planning control")
            try:
                if type(tool) is CaptureArtifact:
                    from eviforge.tools.read_file import ReadFile
                    # Evidence capture reads workspace bytes; both the Plan
                    # read ceiling here and DAG scope in execute must allow it.
                    intent = execution_intent(ReadFile(), {"file_path": arguments["file_path"]}, cwd)
                else:
                    intent = execution_intent(tool, arguments, cwd)
            except (ValueError, TypeError, KeyError, OSError, RuntimeError) as exc:
                return deny("UNSUPPORTED_INTENT", str(exc))
            if not within(intent.cwd, [self.work_dir]):
                return deny("CWD_OUTSIDE_PROJECT", "Tool cwd is outside the project")
            if any(not within(path, [self.work_dir]) for path in (*intent.read_paths, *intent.write_paths)):
                return deny("PATH_OUTSIDE_PROJECT", "Tool path is outside the project")
            if planning:
                from eviforge.mcp.tool_wrapper import MCPToolWrapper
                if type(tool) is MCPToolWrapper and tool.config.integration != "custom" and tool.category == "read":
                    return GateDecision(True, "PLAN_MCP_READ", "Read through a locally reviewed resource adapter")
                if intent.tool_name in {"ReadFile", "Glob", "Grep"}:
                    return GateDecision(True, "PLAN_READ", "Planning read within the project")
                if (plan.state == PlanState.DRAFT and plan.plan_path and intent.write_paths == (plan.plan_path,)
                    and self._draft_owners.get(plan.plan_id) == id(agent)):
                    return GateDecision(True, "PLAN_DRAFT_WRITE", "Writing this plan's draft")
                return deny("APPROVAL_REQUIRED", "Submit a concrete action manifest for user approval")
            read_roots = tuple(path for action in plan.actions for path in (*action.read_paths, *action.write_paths))
            if intent.tool_name in {"ReadFile", "Glob", "Grep"} and all(within(path, read_roots) for path in intent.read_paths):
                return GateDecision(True, "PLAN_SCOPED_READ", "Read within approved file scopes")
            matches = [grant for grant in self._grants if grant.plan_id == plan.plan_id
                and grant.content_hash == plan.content_hash and grant.session_id == agent.session_id
                and grant.source_turn_id == plan.source_turn_id and grant.execution_turn_id == agent.turn_id
                and grant.agent_id == agent.agent_id and grant.expires_at > self._clock()
                and grant.remaining_uses > 0 and grant.action.matches(intent)]
            if not matches:
                return deny("CAPABILITY_DENIED", "No unconsumed approval matches these exact arguments, cwd and resource scopes")
            if consume:
                grant = matches[0]
                grant.remaining_uses -= 1
                if plan.state == PlanState.APPROVED:
                    plan = self._store(replace(plan, state=PlanState.EXECUTING), "plan_execution_started")
                self._event("grant_consumed", plan, grant_id=grant.grant_id, agent_id=agent.agent_id,
                            intent_hash=intent.intent_hash, arguments_hash=intent.arguments_hash,
                            tool_name=tool.name, opaque_process=intent.opaque_process)
            return GateDecision(True, "CAPABILITY_APPROVED", "Exact approved invocation; process effects are not OS-isolated" if intent.opaque_process else "Exact approved invocation")

    def record_result(self, agent: Any, tool_name: str, *, is_error: bool) -> None:
        with self._lock:
            plan = self.current_plan(agent)
            if plan is not None:
                self._event("tool_result", plan, agent_id=agent.agent_id, tool_name=tool_name, is_error=is_error)

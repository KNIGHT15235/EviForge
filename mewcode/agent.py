from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator, Callable

from mewcode.client import LLMClient
from mewcode.context import (
    CompactBoundary,
    CompactCircuitBreaker,
    CompactEvent,
    ContentReplacementRecord,
    ContentReplacementState,
    RecoveryState,
    append_replacement_records,
    apply_tool_result_budget,
    auto_compact,
    create_replacement_state,
    ensure_session_dir,
    load_replacement_records,
    reconstruct_replacement_state,
)
from mewcode.conversation import ConversationManager, ToolResultBlock, ToolUseBlock
from mewcode.conversation import ThinkingBlock as ConvThinkingBlock
from mewcode.evidence import EvidenceOrchestrator, GateVerdict, RequirementContract
from mewcode.execution import (
    ExecutionAssessment,
    ExecutionContext,
    ExecutionGateway,
    InvocationGrant,
    RiskEngine,
    ToolInvocation,
    policy_for,
)
from mewcode.memory.auto_memory import MemoryManager
from mewcode.permissions import (
    Decision,
    PermissionChecker,
    PermissionMode,
)
from mewcode.hooks import HookContext, HookEngine, ToolRejectedError
from mewcode.hooks.engine import HookNotification
from mewcode.plan_session import (
    PlanReviewError,
    PlanSession,
    PlanSessionState,
    plan_content_fingerprint,
)
from mewcode.prompts import build_environment_context, build_plan_mode_reminder, build_system_prompt
from mewcode.tools import ToolRegistry
from mewcode.tools.base import (
    MAX_OUTPUT_CHARS,
    StreamEnd,
    StreamEvent,
    TextDelta,
    ThinkingComplete,
    ThinkingDelta,
    ToolCallComplete,
    ToolCallDelta,
    ToolCallStart,
    ToolResult,
)

if TYPE_CHECKING:
    from mewcode.runtime import TaskRuntime

log = logging.getLogger(__name__)

MEMORY_EXTRACTION_INTERVAL = 5
MAX_TOKENS_CEILING = 64000
MAX_OUTPUT_TOKENS_RECOVERIES = 3

# These host-owned control tools must remain callable while an inline Skill
# narrows ordinary tools. Other lifecycle tools already opt in through
# ``Tool.is_system_tool``; ExitPlanMode predates that marker but is required to
# leave the read-only planning state safely.
_SKILL_INTERNAL_TOOL_NAMES = frozenset({"ExitPlanMode"})


# ---------------------------------------------------------------------------
# AgentEvent 事件类型
# ---------------------------------------------------------------------------

@dataclass
class StreamText:
    text: str


@dataclass
class ThinkingText:
    text: str


@dataclass
class RetryEvent:
    reason: str
    wait: float = 0.0


@dataclass
class ToolUseEvent:
    tool_name: str
    tool_id: str
    arguments: dict[str, Any]


@dataclass
class ToolResultEvent:
    tool_id: str
    tool_name: str
    output: str
    is_error: bool
    elapsed: float


@dataclass
class TurnComplete:
    turn: int


@dataclass
class LoopComplete:
    total_turns: int
    # Populated only when ExitPlanMode succeeded in this exact model turn.
    # The UI must present/approve this host-owned session and fingerprint,
    # never infer readiness from permission_mode alone.
    plan_review_session_id: str = ""
    plan_fingerprint: str = ""


@dataclass(frozen=True)
class CompletionBlockedEvent:
    """A model claimed completion, but deterministic evidence did not pass."""

    verdict: str
    reasons: tuple[str, ...]
    bundle_ref: str


class CompletionBlockedError(RuntimeError):
    """Raised by the headless API when it exhausts its budget without PASS evidence."""

    def __init__(self, event: CompletionBlockedEvent) -> None:
        self.event = event
        detail = "; ".join(event.reasons) or "verification did not pass"
        super().__init__(f"Completion blocked ({event.verdict}): {detail}")


@dataclass
class UsageEvent:
    input_tokens: int
    output_tokens: int


@dataclass
class ErrorEvent:
    message: str


@dataclass
class CompactNotification:
    before_tokens: int
    message: str
    # 结构化 boundary（摘要 + 原文保留尾部），UI/session 层用它持久化 compact_boundary 记录。
    # 失败路径下为 None。
    boundary: "CompactBoundary | None" = None


@dataclass
class HookEvent:
    hook_id: str
    event: str
    output: str
    success: bool


class PermissionResponse(Enum):
    ALLOW = "allow"
    DENY = "deny"
    ALLOW_ALWAYS = "allow_always"


@dataclass
class PermissionRequest:
    tool_name: str
    description: str
    future: asyncio.Future[PermissionResponse]
    risk_level: str = "unknown"
    reason_codes: tuple[str, ...] = ()
    approval_scope: str = "single_use"


AgentEvent = (
    StreamText
    | ThinkingText
    | RetryEvent
    | ToolUseEvent
    | ToolResultEvent
    | TurnComplete
    | LoopComplete
    | CompletionBlockedEvent
    | UsageEvent
    | ErrorEvent
    | PermissionRequest
    | CompactNotification
    | HookEvent
)


# ---------------------------------------------------------------------------
# LLM 响应收集器
# ---------------------------------------------------------------------------

@dataclass
class ThinkingBlock:
    thinking: str
    signature: str


@dataclass
class LLMResponse:
    text: str = ""
    tool_calls: list[ToolCallComplete] = field(default_factory=list)
    thinking_blocks: list[ThinkingBlock] = field(default_factory=list)
    stop_reason: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_creation: int = 0


class StreamCollector:
    def __init__(self) -> None:
        self.response = LLMResponse()

    async def consume(
        self, stream: AsyncIterator[StreamEvent]
    ) -> AsyncIterator[AgentEvent]:
        async for event in stream:
            if isinstance(event, TextDelta):
                self.response.text += event.text
                yield StreamText(text=event.text)
            elif isinstance(event, ThinkingDelta):
                yield ThinkingText(text=event.text)
            elif isinstance(event, ThinkingComplete):
                self.response.thinking_blocks.append(
                    ThinkingBlock(thinking=event.thinking, signature=event.signature)
                )
            elif isinstance(event, ToolCallStart):
                pass
            elif isinstance(event, ToolCallDelta):
                pass
            elif isinstance(event, ToolCallComplete):
                self.response.tool_calls.append(event)
                yield ToolUseEvent(
                    tool_name=event.tool_name,
                    tool_id=event.tool_id,
                    arguments=event.arguments,
                )
            elif isinstance(event, StreamEnd):
                self.response.stop_reason = event.stop_reason
                self.response.input_tokens = event.input_tokens
                self.response.output_tokens = event.output_tokens
                self.response.cache_read = event.cache_read
                self.response.cache_creation = event.cache_creation


# ---------------------------------------------------------------------------
# tool 批量执行
# ---------------------------------------------------------------------------

@dataclass
class ToolBatch:
    concurrent: bool
    calls: list[ToolCallComplete]


def partition_tool_calls(
    tool_calls: list[ToolCallComplete],
    registry: ToolRegistry,
) -> list[ToolBatch]:
    batches: list[ToolBatch] = []
    for tc in tool_calls:
        tool = registry.get(tc.tool_name)
        # Only read-only calls can use the prompt-free fast path.  Several
        # legacy team tools are concurrency-safe for locking purposes but are
        # still commands with side effects and require the approval flow.
        safe = (
            tool is not None
            and tool.is_read_only
            and tool.is_concurrency_safe
            and registry.is_enabled(tc.tool_name)
        )

        if safe and batches and batches[-1].concurrent:
            batches[-1].calls.append(tc)
        else:
            batches.append(ToolBatch(concurrent=safe, calls=[tc]))
    return batches


# ---------------------------------------------------------------------------
# streaming 执行器 — 在 LLM streaming 期间启动 tool 执行
# ---------------------------------------------------------------------------

@dataclass
class _ToolExecResult:
    tool_id: str
    tool_name: str
    result: ToolResult
    elapsed: float
    is_unknown: bool


class StreamingExecutor:
    def __init__(self) -> None:
        self._tasks: list[tuple[int, asyncio.Task[_ToolExecResult]]] = []
        self._order = 0

    def submit(
        self,
        coro: Any,
    ) -> None:
        task = asyncio.create_task(coro)
        self._tasks.append((self._order, task))
        self._order += 1

    async def collect_results(self) -> list[_ToolExecResult]:
        if not self._tasks:
            return []
        tasks = [t for _, t in sorted(self._tasks, key=lambda x: x[0])]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        out: list[_ToolExecResult] = []
        for r in results:
            if isinstance(r, Exception):
                out.append(_ToolExecResult(
                    tool_id="",
                    tool_name="",
                    result=ToolResult(output=f"Tool execution error: {r}", is_error=True),
                    elapsed=0.0,
                    is_unknown=False,
                ))
            else:
                out.append(r)
        return out


class _AuthorizedExecutionPolicy:
    """Allow execution only after Agent-owned permission handling.

    Model arguments and invocation metadata cannot select this policy.  The
    gateway still validates the schema and applies its L4 hard-deny rules.
    """

    def check(self, tool: Any, arguments: dict[str, Any]) -> Decision:
        return Decision(effect="allow", reason="Agent permission decision already resolved")


# ---------------------------------------------------------------------------
# Agent 主循环
# ---------------------------------------------------------------------------

class Agent:
    def __init__(
        self,
        client: LLMClient,
        registry: ToolRegistry,
        protocol: str,
        work_dir: str = ".",
        max_iterations: int = 50,
        permission_checker: PermissionChecker | None = None,
        context_window: int = 200_000,
        instructions_content: str = "",
        memory_manager: MemoryManager | None = None,
        hook_engine: HookEngine | None = None,
        execution_gateway: ExecutionGateway | None = None,
        requirement_contract: RequirementContract | None = None,
        evidence_orchestrator: EvidenceOrchestrator | None = None,
        task_runtime: "TaskRuntime | None" = None,
        execution_context: ExecutionContext | None = None,
        evolution_adapter: Any | None = None,
    ) -> None:
        self.client = client
        self.registry = registry
        self.protocol = protocol
        self.work_dir = work_dir
        self.max_iterations = max_iterations
        self.permission_checker = permission_checker
        self.permission_mode: PermissionMode = (
            permission_checker.mode if permission_checker else PermissionMode.DEFAULT
        )
        self.context_window = context_window
        self.session_dir = ensure_session_dir(work_dir)
        self.compact_breaker = CompactCircuitBreaker()
        self.replacement_state: ContentReplacementState = create_replacement_state()
        # 保存重建工作上下文所需的快照，在 Layer 2 压缩对话后使用：
        # 最近的文件读取和 skill 调用。每次 ReadFile / skill 调用时记录，
        # auto_compact 触发阈值时消费。
        self.recovery_state: RecoveryState = RecoveryState()
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.instructions_content = instructions_content
        self.memory_manager = memory_manager
        self.hook_engine = hook_engine
        # The Agent keeps the existing PermissionRequest flow.  The gateway is
        # the final, mandatory execution boundary and independently enforces
        # strict argument validation and deterministic L4 hard-denies.
        self.execution_gateway = execution_gateway or ExecutionGateway(
            # Embedders that construct Agent directly must not accidentally
            # bypass their PathSandbox/permission rules on the parallel-read
            # fast path.  The permissive adapter is only appropriate when no
            # legacy policy was supplied at all.
            permission_checker=permission_checker or _AuthorizedExecutionPolicy(),
            risk_engine=RiskEngine(workspace_root=work_dir),
        )
        self.execution_context = execution_context or getattr(
            self.execution_gateway, "execution_context", None
        )
        self.requirement_contract = requirement_contract
        self.evidence_orchestrator = (
            evidence_orchestrator
            if evidence_orchestrator is not None
            else (EvidenceOrchestrator() if requirement_contract is not None else None)
        )
        self.task_runtime = task_runtime
        self.evolution_adapter = evolution_adapter
        if self.hook_engine is not None and hasattr(
            self.hook_engine, "bind_execution_policy"
        ):
            # Resolve the context per action: Plan approval and worktree entry
            # replace this object during the Agent lifetime. Raw command/HTTP
            # hooks then fail closed whenever a reviewed manifest is active.
            self.hook_engine.bind_execution_policy(
                context_provider=self._active_execution_context,
                gateway=self.execution_gateway,
            )
        self._completion_gate_dirty = True
        self._last_completion_block: CompletionBlockedEvent | None = None
        self._last_verified_diff_sha256 = ""
        self._unchanged_completion_checks = 0
        self._completion_gate_exhausted = False
        self._evidence_passed = requirement_contract is None
        self._completion_had_nonpass = False
        self._last_evidence_bundle_ref = ""
        self._last_gate_decision_id = ""
        self._loop_count = 0
        self._extracting = False
        self.session_id: str = ""
        self.active_skills: dict[str, str] = {}
        # ``None`` means the Skill declared no allowlist (all ordinary tools).
        # Multiple restrictive inline Skills compose by intersection: a tool
        # must satisfy every active contract, never broaden an earlier one.
        self._active_skill_tool_allowlists: dict[
            str, frozenset[str] | None
        ] = {}
        self._skill_catalog: str = ""
        self._agent_catalog: str = ""
        self._agent_catalog_list: list[tuple[str, str]] = []
        self.agent_id: str = uuid.uuid4().hex[:12]
        self.parent_id: str | None = None
        self.trace_id: str | None = None
        self.coordinator_mode: bool = False
        self.team_name: str = ""
        self._team_manager: Any = None
        self.notification_fn: Callable[[], list[str]] | None = None
        # Headless Session persistence consumes these structured boundaries.
        # Interactive mode already receives the same value through
        # CompactNotification and persists it in the UI layer.
        self._compact_boundaries: list[CompactBoundary] = []
        self.file_history: Any = None
        # Plan paths and approvals are instance/session scoped.  The former
        # class-level cache could leak a prior Agent's Plan into a new review.
        self._plan_path_cache: Path | None = None
        self._plan_session: PlanSession | None = None
        if self.permission_mode is PermissionMode.PLAN:
            self.begin_plan_session(pre_permission_mode=PermissionMode.DEFAULT)

    @property
    def _transcript_path(self) -> str:
        if self.session_id:
            return str(Path(self.work_dir) / ".mewcode" / "sessions" / f"{self.session_id}.jsonl")
        return ""

    @property
    def plan_mode(self) -> bool:
        return self.permission_mode == PermissionMode.PLAN

    @property
    def plan_session(self) -> PlanSession | None:
        return self._plan_session

    def _set_permission_mode_raw(self, mode: PermissionMode) -> None:
        self.permission_mode = mode
        if self.permission_checker:
            self.permission_checker.mode = mode

    def begin_plan_session(
        self,
        *,
        pre_permission_mode: PermissionMode | None = None,
    ) -> PlanSession:
        """Enter Plan mode with a fresh path and no inherited approval state."""

        previous = pre_permission_mode
        if previous is None:
            previous = (
                self.permission_mode
                if self.permission_mode is not PermissionMode.PLAN
                else PermissionMode.DEFAULT
            )
        if previous is PermissionMode.PLAN:
            previous = PermissionMode.DEFAULT

        if self._plan_session is not None:
            self._plan_session.close()
        self._plan_path_cache = None

        session_id = uuid.uuid4().hex
        path = self._allocate_plan_path(session_id)
        self._plan_session = PlanSession(
            session_id=session_id,
            plan_path=path,
            pre_permission_mode=previous,
        )
        self._set_permission_mode_raw(PermissionMode.PLAN)
        if self.permission_checker:
            self.permission_checker.plan_file_path = str(path)
        log.info(
            "Plan session started session=%s restore_mode=%s",
            session_id,
            previous.value,
        )
        return self._plan_session

    def close_plan_session(
        self,
        *,
        restore_mode: PermissionMode | None = None,
    ) -> PermissionMode:
        """Close without approval and restore an explicit non-Plan mode."""

        session = self._plan_session
        target = restore_mode or (
            session.pre_permission_mode if session else PermissionMode.DEFAULT
        )
        if target is PermissionMode.PLAN:
            target = PermissionMode.DEFAULT
        if session is not None:
            session.close()
            log.info(
                "Plan session closed session=%s restore_mode=%s",
                session.session_id,
                target.value,
            )
        if self.permission_checker:
            self.permission_checker.plan_file_path = ""
        self._set_permission_mode_raw(target)
        return target

    def cancel_plan_review(self) -> None:
        """Dismiss review while remaining in the same read-only Plan draft."""

        session = self._plan_session
        if session is None:
            return
        session.cancel_review()
        self._set_permission_mode_raw(PermissionMode.PLAN)
        log.info("Plan review cancelled session=%s", session.session_id)

    def _allocate_plan_path(self, session_id: str) -> Path:
        import datetime
        import random

        adjectives = [
            "bold", "bright", "calm", "cool", "deep", "fair", "fast", "fine",
            "glad", "keen", "kind", "lean", "mild", "neat", "pure", "safe",
            "slim", "soft", "tall", "warm", "wise", "grand", "swift", "vivid",
        ]
        nouns = [
            "sketch", "draft", "spark", "bloom", "trail", "ridge", "creek", "grove",
            "cliff", "cloud", "field", "forge", "frost", "haven", "pearl", "stone",
            "storm", "river", "tower", "delta", "flame", "orbit", "pulse", "shore",
        ]
        plans_dir = Path(self.work_dir) / ".mewcode" / "plans"
        plans_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now().strftime("%m%d-%H%M%S")
        slug = (
            f"{random.choice(adjectives)}-{random.choice(nouns)}-{ts}-"
            f"{session_id[:8]}"
        )
        self._plan_path_cache = plans_dir / f"{slug}.md"
        return self._plan_path_cache

    def _get_plan_path(self) -> Path:
        if self._plan_path_cache is not None:
            return self._plan_path_cache
        # Compatibility for embedders that request a path before explicitly
        # entering Plan mode.  Such a path has no approval authority.
        return self._allocate_plan_path(uuid.uuid4().hex)

    def _mark_plan_ready(self, *, turn: int) -> tuple[str, str] | None:
        session = self._plan_session
        if not self.plan_mode or session is None:
            return None
        try:
            content = session.plan_path.read_bytes()
        except OSError:
            return None
        if not content.strip():
            return None
        fingerprint = plan_content_fingerprint(content)
        session.mark_ready(fingerprint, turn=turn)
        log.info(
            "Plan ready for review session=%s plan_hash=%s turn=%d",
            session.session_id,
            fingerprint,
            turn,
        )
        return session.session_id, fingerprint

    def is_plan_review_ready(self, session_id: str, fingerprint: str) -> bool:
        session = self._plan_session
        return bool(
            self.plan_mode
            and session is not None
            and session.state is PlanSessionState.READY_FOR_REVIEW
            and session.session_id == session_id
            and session.review_fingerprint == fingerprint
        )

    def read_reviewed_plan(
        self,
        *,
        session_id: str,
        displayed_fingerprint: str,
    ) -> str:
        """Return the exact reviewed content, rejecting stale UI decisions."""

        session = self._plan_session
        if session is None:
            raise PlanReviewError("there is no active Plan session")
        try:
            content = session.plan_path.read_bytes()
        except OSError as exc:
            session.cancel_review()
            raise PlanReviewError(f"the Plan file cannot be read: {exc}") from exc
        current_fingerprint = plan_content_fingerprint(content)
        try:
            session.assert_current_review(
                session_id=session_id,
                displayed_fingerprint=displayed_fingerprint,
                current_fingerprint=current_fingerprint,
            )
        except PlanReviewError:
            # A changed/stale Plan must go through ExitPlanMode again so the UI
            # cannot approve bytes it never displayed.
            if (
                session.state is PlanSessionState.READY_FOR_REVIEW
                and session.session_id == session_id
            ):
                session.cancel_review()
            raise
        try:
            return content.decode("utf-8")
        except UnicodeDecodeError as exc:
            session.cancel_review()
            raise PlanReviewError("the Plan file is not valid UTF-8") from exc

    def start_plan_execution(
        self,
        *,
        session_id: str,
        displayed_fingerprint: str,
        execution_mode: PermissionMode,
    ) -> None:
        """Atomically bind explicit approval to this Plan and leave Plan mode."""

        # Re-read immediately before the state transition.  Compilation may be
        # expensive and an external editor could have changed the file since
        # the first validation in the UI handler.
        self.read_reviewed_plan(
            session_id=session_id,
            displayed_fingerprint=displayed_fingerprint,
        )
        session = self._plan_session
        assert session is not None
        session.approve()
        session.begin_execution()
        if self.permission_checker:
            self.permission_checker.plan_file_path = ""
        self._set_permission_mode_raw(execution_mode)
        log.info(
            "Plan approved session=%s plan_hash=%s execution_mode=%s",
            session.session_id,
            displayed_fingerprint,
            execution_mode.value,
        )

    def set_permission_mode(self, mode: PermissionMode) -> None:
        """Compatibility entry point routed through the Plan state machine."""

        if mode is PermissionMode.PLAN:
            if self.plan_mode and self._plan_session is not None:
                return
            self.begin_plan_session()
            return
        if self.plan_mode:
            self.close_plan_session(restore_mode=mode)
            return
        self._set_permission_mode_raw(mode)

    def activate_skill(
        self,
        name: str,
        prompt_body: str,
        *,
        allowed_tools: list[str] | tuple[str, ...] | None = None,
    ) -> None:
        self.active_skills[name] = prompt_body
        policies = getattr(self, "_active_skill_tool_allowlists", None)
        if not isinstance(policies, dict):
            policies = {}
            self._active_skill_tool_allowlists = policies
        normalized = frozenset(allowed_tools or ())
        policies[name] = normalized or None

    def clear_active_skills(self) -> None:
        self.active_skills.clear()
        policies = getattr(self, "_active_skill_tool_allowlists", None)
        if isinstance(policies, dict):
            policies.clear()

    def _skill_tool_is_allowed(self, tool_name: str) -> bool:
        """Return whether active inline Skill contracts permit ``tool_name``."""

        tool = self.registry.get(tool_name)
        if tool is not None and (
            getattr(tool, "is_system_tool", False)
            or tool_name in _SKILL_INTERNAL_TOOL_NAMES
        ):
            return True

        policies = getattr(self, "_active_skill_tool_allowlists", {})
        if not isinstance(policies, dict):
            return True
        restrictive = [allowed for allowed in policies.values() if allowed is not None]
        return all(tool_name in allowed for allowed in restrictive)

    def _skill_tool_denial(self, tool_name: str) -> str | None:
        if self._skill_tool_is_allowed(tool_name):
            return None
        policies = getattr(self, "_active_skill_tool_allowlists", {})
        blocked_by = sorted(
            name
            for name, allowed in policies.items()
            if allowed is not None and tool_name not in allowed
        )
        scope = ", ".join(blocked_by) or "active Skill policy"
        return (
            f"Skill tool restriction: tool '{tool_name}' is not allowed by "
            f"active inline Skill(s): {scope}. Clear the Skill or use a tool "
            "listed in allowedTools."
        )

    def _tool_schemas_for_active_skills(self) -> list[dict[str, Any]]:
        return [
            schema
            for schema in self.registry.get_all_schemas(self.protocol)
            if self._skill_tool_is_allowed(str(schema.get("name", "")))
        ]

    def _deferred_tool_names_for_active_skills(self) -> list[str]:
        return [
            name
            for name in self.registry.get_deferred_tool_names()
            if self._skill_tool_is_allowed(name)
        ]

    def set_requirement_contract(
        self,
        contract: RequirementContract | None,
        *,
        orchestrator: EvidenceOrchestrator | None = None,
    ) -> None:
        """Attach a reviewed definition-of-done for the next execution.

        Resetting every gate cache here prevents a PASS receipt from a previous
        contract/session from authorizing a later completion claim.
        """

        self.requirement_contract = contract
        self.evidence_orchestrator = (
            orchestrator
            if orchestrator is not None
            else (EvidenceOrchestrator() if contract is not None else None)
        )
        self._completion_gate_dirty = True
        self._last_completion_block = None
        self._last_verified_diff_sha256 = ""
        self._unchanged_completion_checks = 0
        self._completion_gate_exhausted = False
        self._evidence_passed = contract is None
        self._completion_had_nonpass = False
        self._last_evidence_bundle_ref = ""
        self._last_gate_decision_id = ""
        if contract is not None and self.task_runtime is not None:
            from mewcode.runtime import TaskState

            state = self.task_runtime.run.state
            if state == TaskState.RECEIVED:
                self.task_runtime.prepare_contract(contract.contract_id)
                self.task_runtime.begin_planning()
            elif state in {TaskState.REPLANNING, TaskState.NEEDS_HUMAN}:
                self.task_runtime.begin_planning()

    def begin_contract_execution(self) -> None:
        """Advance the durable task only after a reviewed plan is approved."""

        if self.task_runtime is None or self.requirement_contract is None:
            return
        from mewcode.runtime import TaskState

        state = self.task_runtime.run.state
        if state == TaskState.PLANNING:
            self.task_runtime.begin_execution()
        elif state == TaskState.AWAITING_APPROVAL:
            self.task_runtime.approval_granted("interactive-plan-approval")

    def set_execution_manifest(self, manifest: object) -> ExecutionContext:
        """Bind every later tool call to the reviewed Plan manifest.

        The context is host-owned and never reconstructed from model tool
        arguments.  This turns Plan write/command/network declarations into
        enforceable gateway constraints instead of a prompt-only reminder.
        """

        task_id = (
            self.task_runtime.task_id
            if self.task_runtime is not None
            else str(getattr(getattr(manifest, "contract", None), "task_id", self.agent_id))
        )
        context = ExecutionContext.from_manifest(
            manifest,
            task_id=task_id,
            cwd=self.work_dir,
            workspace_root=self.work_dir,
        )
        self.execution_context = context
        if hasattr(self.execution_gateway, "execution_context"):
            self.execution_gateway.execution_context = context
        return context

    def _ensure_runtime_executing(self) -> None:
        """Resume a FAIL/blocked task before a new repair tool is run."""

        if self.task_runtime is None or self.requirement_contract is None:
            return
        from mewcode.runtime import TaskState

        state = self.task_runtime.run.state
        if state in {TaskState.REPLANNING, TaskState.NEEDS_HUMAN}:
            self.task_runtime.begin_planning()
            state = self.task_runtime.run.state
        if state == TaskState.PLANNING:
            self.task_runtime.begin_execution()

    def set_skill_catalog(self, catalog: str) -> None:
        self._skill_catalog = catalog


    def set_agent_catalog(self, catalog: str, catalog_list: list[tuple[str, str]] | None = None) -> None:
        self._agent_catalog = catalog
        if catalog_list is not None:
            self._agent_catalog_list = catalog_list

    def _build_hook_context(self, event: str, **kwargs: str | dict) -> HookContext:
        return HookContext(
            event_name=event,
            tool_name=str(kwargs.get("tool_name", "")),
            tool_args=kwargs.get("tool_args", {}),
            file_path=str(kwargs.get("file_path", "")),
            message=str(kwargs.get("message", "")),
            error=str(kwargs.get("error", "")),
        )

    def _infer_file_path(self, args: dict) -> str:
        return str(args.get("file_path", args.get("path", "")))

    def _drain_hook_events(self) -> list[HookEvent]:
        if not self.hook_engine:
            return []
        return [
            HookEvent(
                hook_id=n.hook_id,
                event=n.event,
                output=n.output,
                success=n.success,
            )
            for n in self.hook_engine.drain_notifications()
        ]

    async def run(self, conversation: ConversationManager) -> AsyncIterator[AgentEvent]:
        self._current_conversation = conversation
        env_context = build_environment_context(
            self.work_dir, self.active_skills, self._skill_catalog, self._agent_catalog
        )
        conversation.inject_environment(env_context)

        memory_content = self.memory_manager.load() if self.memory_manager else ""
        conversation.inject_long_term_memory(self.instructions_content, memory_content)

        if self.hook_engine:
            ctx = self._build_hook_context("session_start")
            await self.hook_engine.run_hooks("session_start", ctx)
            for he in self._drain_hook_events():
                yield he

        iteration = 0
        consecutive_unknown = 0
        max_tokens_escalated = False
        output_recoveries = 0

        while True:
            iteration += 1

            if iteration > self.max_iterations:
                yield ErrorEvent(
                    message=f"Agent reached maximum iterations ({self.max_iterations})"
                )
                break

            if self.hook_engine:
                ctx = self._build_hook_context("turn_start")
                await self.hook_engine.run_hooks("turn_start", ctx)
                for he in self._drain_hook_events():
                    yield he

            self._consume_mailbox(conversation)
            if self.notification_fn:
                for note in self.notification_fn():
                    conversation.add_system_reminder(note)

            # Layer 2: 接近 context window 上限时自动 compact（操作原始对话）
            compact_result = await auto_compact(
                conversation,
                self.client,
                self.context_window,
                self.session_dir,
                protocol=self.protocol,
                breaker=self.compact_breaker,
                recovery=self.recovery_state,
                tool_schemas=self._tool_schemas_for_active_skills(),
                transcript_path=self._transcript_path,
            )
            if isinstance(compact_result, CompactEvent):
                yield CompactNotification(
                    before_tokens=compact_result.before_tokens,
                    message=f"上下文已压缩（压缩前 {compact_result.before_tokens:,} tokens）",
                    boundary=compact_result.boundary,
                )
                conversation.inject_environment(env_context)
                mem = self.memory_manager.load() if self.memory_manager else ""
                conversation.inject_long_term_memory(
                    self.instructions_content, mem
                )
            elif isinstance(compact_result, str):
                yield ErrorEvent(message=compact_result)

            if self.hook_engine:
                ctx = self._build_hook_context("pre_send")
                await self.hook_engine.run_hooks("pre_send", ctx)
                for he in self._drain_hook_events():
                    yield he

            hook_prompts = (
                self.hook_engine.get_prompt_messages() if self.hook_engine else None
            )
            system = build_system_prompt(
                hook_prompts=hook_prompts,
                coordinator_mode=self.coordinator_mode,
                agent_catalog=self._agent_catalog_list or None,
                work_dir=self.work_dir,
            )

            if self.plan_mode:
                plan_path = str(self._get_plan_path())
                if self.permission_checker:
                    self.permission_checker.plan_file_path = plan_path
                plan_exists = self._get_plan_path().exists()
                plan_reminder = build_plan_mode_reminder(
                    plan_path, plan_exists, iteration
                )
                conversation.add_system_reminder(plan_reminder)

            if self.hook_engine:
                for note in self.hook_engine.drain_notifications():
                    conversation.add_system_reminder(
                        f"Hook [{note.hook_id}] {note.event}: {note.output}"
                    )

            deferred_names = self._deferred_tool_names_for_active_skills()
            if deferred_names:
                conversation.add_system_reminder(
                    "The following deferred tools are available via ToolSearch. "
                    "Their schemas are NOT loaded - use ToolSearch with "
                    'query "select:<name>[,<name>...]" to load tool schemas before calling them:\n'
                    + "\n".join(deferred_names)
                )

            tools = self._tool_schemas_for_active_skills()

            # Layer 1: 在 LLM 调用前应用 tool-result budget，确保 api_conv 反映
            # 本轮迭代中所有已发生的写入（system reminders、hook 通知等）。
            # 原始 conversation 不会被修改；替换决策保存在 self.replacement_state 中。
            api_conv, _new_records = apply_tool_result_budget(
                conversation, self.session_dir, self.replacement_state
            )
            if _new_records:
                append_replacement_records(self.session_dir, _new_records)

            collector = StreamCollector()
            llm_stream = self.client.stream(api_conv, system=system, tools=tools)
            async for event in collector.consume(llm_stream):
                yield event

            response = collector.response

            if self.hook_engine:
                ctx = self._build_hook_context("post_receive", message=response.text)
                await self.hook_engine.run_hooks("post_receive", ctx)
                for he in self._drain_hook_events():
                    yield he

            self.total_input_tokens += response.input_tokens
            self.total_output_tokens += response.output_tokens
            yield UsageEvent(
                input_tokens=self.total_input_tokens,
                output_tokens=self.total_output_tokens,
            )

            conv_thinking = [
                ConvThinkingBlock(thinking=tb.thinking, signature=tb.signature)
                for tb in response.thinking_blocks
            ]

            if response.stop_reason == "max_tokens":
                if not max_tokens_escalated:
                    self.client.set_max_output_tokens(MAX_TOKENS_CEILING)
                    max_tokens_escalated = True
                    if response.text:
                        conversation.add_assistant_message(
                            response.text, thinking_blocks=conv_thinking
                        )
                        conversation.add_user_message(
                            "Output token limit hit. Resume directly from where you stopped. "
                            "Do not apologize or repeat previous content. Pick up mid-thought if needed."
                        )
                    yield RetryEvent(reason="max_tokens escalation")
                    continue
                elif output_recoveries < MAX_OUTPUT_TOKENS_RECOVERIES:
                    output_recoveries += 1
                    conversation.add_assistant_message(
                        response.text, thinking_blocks=conv_thinking
                    )
                    conversation.add_user_message(
                        "Output token limit hit. Resume directly from where you stopped. "
                        "Break remaining work into smaller pieces."
                    )
                    yield RetryEvent(
                        reason=f"max_tokens recovery {output_recoveries}/{MAX_OUTPUT_TOKENS_RECOVERIES}"
                    )
                    continue
            else:
                output_recoveries = 0

            if not response.tool_calls:
                conversation.add_assistant_message(
                    response.text, thinking_blocks=conv_thinking
                )
                completion_block = await self._attempt_completion(conversation)
                if completion_block is not None:
                    yield completion_block
                    if self._completion_gate_exhausted:
                        yield ErrorEvent(
                            message=(
                                "Completion blocked by deterministic verification "
                                f"({completion_block.verdict}); no workspace change followed feedback"
                            )
                        )
                        break
                    yield TurnComplete(turn=iteration)
                    continue
                self._loop_count += 1
                if (
                    self._loop_count % MEMORY_EXTRACTION_INTERVAL == 0
                    and self.memory_manager
                ):
                    asyncio.ensure_future(self._extract_memories(conversation))
                if self.hook_engine:
                    ctx = self._build_hook_context("turn_end")
                    await self.hook_engine.run_hooks("turn_end", ctx)
                    ctx = self._build_hook_context("session_end")
                    await self.hook_engine.run_hooks("session_end", ctx)
                    for he in self._drain_hook_events():
                        yield he
                if self.file_history is not None:
                    summary = response.text[:60] + "..." if len(response.text) > 60 else response.text
                    self.file_history.make_snapshot(len(conversation.history), summary)
                yield LoopComplete(total_turns=iteration)
                break

            tool_uses = [
                ToolUseBlock(
                    tool_use_id=tc.tool_id,
                    tool_name=tc.tool_name,
                    arguments=tc.arguments,
                )
                for tc in response.tool_calls
            ]
            conversation.add_assistant_message(
                response.text, tool_uses, thinking_blocks=conv_thinking
            )
            # 在 assistant 回复加入历史后锚定实际用量：基线（input + cache + output）
            # 覆盖到当前位置，因此下一轮迭代顶部的 auto-compact 检查只需对
            # 接下来追加的 tool results 做字符估算。
            conversation.record_usage_anchor(
                response.input_tokens,
                response.output_tokens,
                response.cache_read,
                response.cache_creation,
            )

            tool_results: list[ToolResultBlock] = []
            exit_plan_succeeded = False
            batches = partition_tool_calls(response.tool_calls, self.registry)

            for batch in batches:
                if batch.concurrent and len(batch.calls) > 1:
                    batch_results = await self._execute_batch_parallel(batch.calls)
                    for br in batch_results:
                        if br.is_unknown:
                            consecutive_unknown += 1
                        else:
                            consecutive_unknown = 0
                        content = self._maybe_persist_or_truncate(
                            br.tool_id, br.result.output
                        )
                        tool_results.append(
                            ToolResultBlock(
                                tool_use_id=br.tool_id,
                                content=content,
                                is_error=br.result.is_error,
                            )
                        )
                        yield ToolResultEvent(
                            tool_id=br.tool_id,
                            tool_name=br.tool_name,
                            output=br.result.output,
                            is_error=br.result.is_error,
                            elapsed=br.elapsed,
                        )
                        if br.tool_name == "ExitPlanMode" and not br.result.is_error:
                            exit_plan_succeeded = True
                else:
                    for tc in batch.calls:
                        result: ToolResult | None = None
                        elapsed = 0.0
                        is_unknown = False

                        if self.hook_engine:
                            file_path = self._infer_file_path(tc.arguments)
                            hook_ctx = self._build_hook_context(
                                "pre_tool_use",
                                tool_name=tc.tool_name,
                                tool_args=tc.arguments,
                                file_path=file_path,
                            )
                            rejection = await self.hook_engine.run_pre_tool_hooks(hook_ctx)
                            for he in self._drain_hook_events():
                                yield he
                            if rejection is not None:
                                result = ToolResult(
                                    output=f"Hook rejected: {rejection.reason}",
                                    is_error=True,
                                )
                                content = self._maybe_persist_or_truncate(
                                    tc.tool_id, result.output
                                )
                                tool_results.append(
                                    ToolResultBlock(
                                        tool_use_id=tc.tool_id,
                                        content=content,
                                        is_error=True,
                                    )
                                )
                                yield ToolResultEvent(
                                    tool_id=tc.tool_id,
                                    tool_name=tc.tool_name,
                                    output=result.output,
                                    is_error=True,
                                    elapsed=0.0,
                                )
                                continue

                        async for item in self._execute_tool(tc):
                            if isinstance(item, PermissionRequest):
                                yield item
                            else:
                                result, elapsed, is_unknown = item

                        if result is None:
                            result = ToolResult(output="Error: no result from tool", is_error=True)

                        if is_unknown:
                            consecutive_unknown += 1
                        else:
                            consecutive_unknown = 0

                        if self.hook_engine:
                            file_path = self._infer_file_path(tc.arguments)
                            hook_ctx = self._build_hook_context(
                                "post_tool_use",
                                tool_name=tc.tool_name,
                                tool_args=tc.arguments,
                                file_path=file_path,
                            )
                            await self.hook_engine.run_hooks("post_tool_use", hook_ctx)
                            for he in self._drain_hook_events():
                                yield he

                        content = self._maybe_persist_or_truncate(
                            tc.tool_id, result.output
                        )
                        tool_results.append(
                            ToolResultBlock(
                                tool_use_id=tc.tool_id,
                                content=content,
                                is_error=result.is_error,
                            )
                        )
                        yield ToolResultEvent(
                            tool_id=tc.tool_id,
                            tool_name=tc.tool_name,
                            output=result.output,
                            is_error=result.is_error,
                            elapsed=elapsed,
                        )
                        if tc.tool_name == "ExitPlanMode" and not result.is_error:
                            exit_plan_succeeded = True

            if consecutive_unknown >= 3:
                yield ErrorEvent(
                    message="Agent terminated: too many consecutive unknown tool calls"
                )
                break

            conversation.add_tool_results_message(tool_results)
            if exit_plan_succeeded:
                review = self._mark_plan_ready(turn=iteration)
                if review is not None:
                    review_session_id, fingerprint = review
                    yield TurnComplete(turn=iteration)
                    yield LoopComplete(
                        total_turns=iteration,
                        plan_review_session_id=review_session_id,
                        plan_fingerprint=fingerprint,
                    )
                    break
                yield ErrorEvent(
                    message=(
                        "ExitPlanMode did not open approval because the active "
                        "Plan file is missing or empty. Continue planning and call "
                        "ExitPlanMode again after writing it."
                    )
                )

            if self.hook_engine:
                ctx = self._build_hook_context("turn_end")
                await self.hook_engine.run_hooks("turn_end", ctx)
                for he in self._drain_hook_events():
                    yield he
            yield TurnComplete(turn=iteration)


    def _consume_mailbox(self, conversation: ConversationManager) -> None:
        if not self.team_name or not self._team_manager:
            return
        try:
            mailbox = self._team_manager.get_mailbox(self.team_name)
            if mailbox is None:
                return
            messages = mailbox.consume(self.agent_id)
            for msg in messages:
                prefix = f"[Message from {msg.from_agent}]"
                if msg.message_type != "text":
                    prefix = f"[{msg.message_type} from {msg.from_agent}]"
                content = f"{prefix} {msg.content}"
                conversation.add_user_message(content)
        except Exception as e:
            log.debug("Mailbox consumption failed: %s", e)

    def _active_execution_context(self) -> ExecutionContext | None:
        """Return a context whose cwd follows the current worktree."""

        context = self.execution_context
        if context is None:
            return None
        current = str(Path(self.work_dir).expanduser().resolve(strict=False))
        if str(Path(context.cwd).expanduser().resolve(strict=False)) == current:
            return context
        # Relative manifest entries remain valid when a reviewed task moves to
        # its isolated worktree. Rebinding never broadens a declared set.
        context = ExecutionContext(
            task_id=context.task_id,
            cwd=current,
            plan_hash=context.plan_hash,
            expected_pre_state_hash="worktree-not-attested",
            workspace_root=current,
            write_set=context.write_set,
            commands=context.commands,
            network_hosts=context.network_hosts,
        )
        self.execution_context = context
        if hasattr(self.execution_gateway, "execution_context"):
            self.execution_gateway.execution_context = context
        return context

    def _preview_execution(
        self, tool: Any, tc: ToolCallComplete
    ) -> ExecutionAssessment | None:
        preview = getattr(self.execution_gateway, "preview", None)
        if not callable(preview):
            return None
        return preview(
            tool,
            dict(tc.arguments),
            invocation_id=tc.tool_id,
            actor=f"agent:{self.agent_id}",
            context=self._active_execution_context(),
        )

    def _issue_execution_grant(
        self,
        assessment: ExecutionAssessment | None,
        *,
        approver: str,
    ) -> InvocationGrant | None:
        if assessment is None:
            return None
        try:
            return self.execution_gateway.issue_grant(
                assessment,
                approver=approver,
                context=self._active_execution_context(),
            )
        except ValueError:
            # Invalid manifests and L4 calls must still traverse execute() so
            # they receive a durable, redacted denial trace.
            return None

    def _build_permission_description(
        self,
        tc: ToolCallComplete,
        assessment: ExecutionAssessment | None = None,
    ) -> str:
        if tc.tool_name == "Bash":
            target = tc.arguments.get("command", tc.tool_name)
        elif tc.tool_name in ("ReadFile", "WriteFile", "EditFile"):
            target = tc.arguments.get("file_path", tc.tool_name)
        else:
            target = str(tc.arguments)
        if assessment is None:
            return str(target)
        if not assessment.valid:
            return f"{target}\nPolicy: blocked ({', '.join(assessment.reason_codes)})"
        assert assessment.risk is not None
        policy = policy_for(assessment.risk)
        codes = ", ".join(assessment.reason_codes) or "none"
        return (
            f"{target}\nRisk: {assessment.risk.level}; approval scope: "
            f"{policy.approval_scope}; reasons: {codes}"
        )

    async def _execute_via_gateway(
        self,
        tool: Any,
        tc: ToolCallComplete,
        *,
        grant: InvocationGrant | None = None,
    ) -> ToolResult:
        """Execute one already-authorized call through the single gateway.

        The invocation contains no model-controlled authorization flag.  The
        Agent resolves its legacy permission UI before entering this method;
        the default gateway then independently validates arguments and applies
        the non-bypassable L4 policy.
        """

        self._ensure_runtime_executing()
        invocation = ToolInvocation(
            invocation_id=tc.tool_id,
            tool_name=tc.tool_name,
            arguments=dict(tc.arguments),
            actor=f"agent:{self.agent_id}",
        )
        try:
            if callable(getattr(self.execution_gateway, "preview", None)):
                execution = await self.execution_gateway.execute(
                    tool,
                    invocation,
                    grant=grant,
                    context=self._active_execution_context(),
                )
            else:
                # Compatibility for embedders using the legacy two-argument
                # gateway protocol.
                execution = await self.execution_gateway.execute(tool, invocation)
        except Exception as exc:
            return ToolResult(
                output=f"Tool execution error: {type(exc).__name__}: {exc}",
                is_error=True,
            )

        # Test doubles may return a ToolResult, while the production boundary
        # returns ExecutionResult.  Both remain safely converted at this edge.
        if isinstance(execution, ToolResult):
            result = execution
            executed = True
        else:
            result = ToolResult(
                output=str(execution.output),
                is_error=bool(execution.is_error),
            )
            executed = bool(execution.executed)
        if executed and not tool.is_read_only:
            self._completion_gate_dirty = True
        return result

    async def _attempt_completion(
        self, conversation: ConversationManager
    ) -> CompletionBlockedEvent | None:
        """Verify a model completion claim and inject actionable feedback.

        ``None`` means completion is allowed.  A blocked event means the caller
        must continue (or stop without claiming success when repeated evidence
        against an unchanged workspace cannot make progress).
        """

        if self.requirement_contract is None:
            return None
        if self.task_runtime is not None:
            from mewcode.runtime import TaskState

            state = self.task_runtime.run.state
            if state in {TaskState.REPLANNING, TaskState.NEEDS_HUMAN}:
                self.task_runtime.begin_planning()
                self.task_runtime.begin_execution()
                state = self.task_runtime.run.state
            if state == TaskState.PLANNING:
                self.task_runtime.begin_execution()
                state = self.task_runtime.run.state
            if state == TaskState.EXECUTING:
                self.task_runtime.begin_verification()
        if self.evidence_orchestrator is None:
            event = CompletionBlockedEvent(
                verdict=GateVerdict.BLOCKED.value,
                reasons=("evidence orchestrator is not configured",),
                bundle_ref="unavailable",
            )
            self._completion_gate_exhausted = True
            self._inject_verification_feedback(conversation, event)
            self._last_completion_block = event
            return event

        # Permit one retry on the same workspace: a transient verifier can
        # recover, and tests can turn PARTIAL into PASS.  A third identical
        # completion claim reuses the durable result instead of looping forever.
        if (
            not self._completion_gate_dirty
            and self._last_completion_block is not None
            and self._unchanged_completion_checks >= 2
        ):
            self._completion_gate_exhausted = True
            self._inject_verification_feedback(conversation, self._last_completion_block)
            return self._last_completion_block

        try:
            outcome = await self.evidence_orchestrator.run(
                self.requirement_contract,
                repo_root=self.work_dir,
            )
            verdict_value = getattr(outcome.decision.verdict, "value", outcome.decision.verdict)
            verdict = str(verdict_value)
            current_diff = str(outcome.decision.current_diff_sha256)
            bundle_ref = str(outcome.bundle.directory)
            reasons = tuple(str(reason) for reason in outcome.decision.reasons)
            decision_id = str(
                getattr(outcome.decision, "decision_id", "gate-decision")
            )
        except Exception as exc:
            verdict = GateVerdict.BLOCKED.value
            current_diff = ""
            bundle_ref = "unavailable"
            reasons = (f"evidence verification failed: {type(exc).__name__}: {exc}",)
            decision_id = "gate-unavailable"

        if self.task_runtime is not None:
            from mewcode.runtime import TaskState

            if self.task_runtime.run.state == TaskState.VERIFYING:
                self.task_runtime.apply_gate_verdict(
                    verdict,
                    decision_id=decision_id,
                    bundle_ref=bundle_ref,
                    reasons=reasons,
                )

        if verdict == GateVerdict.PASS.value:
            self._evidence_passed = True
            self._last_completion_block = None
            self._last_verified_diff_sha256 = current_diff
            self._last_evidence_bundle_ref = bundle_ref
            self._last_gate_decision_id = decision_id
            self._unchanged_completion_checks = 0
            self._completion_gate_dirty = False
            self._completion_gate_exhausted = False
            self._capture_evolution_candidate(current_diff)
            return None

        if current_diff and current_diff == self._last_verified_diff_sha256:
            self._unchanged_completion_checks += 1
        else:
            self._last_verified_diff_sha256 = current_diff
            self._unchanged_completion_checks = 1
        self._completion_gate_dirty = False
        self._completion_had_nonpass = True
        self._completion_gate_exhausted = self._unchanged_completion_checks >= 2
        event = CompletionBlockedEvent(
            verdict=verdict,
            reasons=reasons,
            bundle_ref=bundle_ref,
        )
        self._last_completion_block = event
        self._inject_verification_feedback(conversation, event)
        return event

    def _capture_evolution_candidate(self, source_code_hash: str) -> None:
        """Persist a quarantined candidate only after repair + Evidence PASS.

        The draft contains application-owned procedure codes and hashes, never
        arbitrary chat, source, or tool output.  It cannot become an active
        Skill without independent validation and the registry promotion gate.
        Evolution is deliberately best-effort and cannot turn a verified task
        into a failed user request.
        """

        if (
            self.evolution_adapter is None
            or self.task_runtime is None
            or self.requirement_contract is None
            or len(source_code_hash) != 64
        ):
            return
        try:
            from mewcode.evolution import EvolutionDraft

            structured_failures = tuple(
                self.evolution_adapter.service.structured_failures(
                    self.task_runtime.task_id
                )
            )
            if not self._completion_had_nonpass and not structured_failures:
                return

            commit = ""
            try:
                import subprocess

                commit = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=self.work_dir,
                    capture_output=True,
                    check=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=5,
                ).stdout.strip().casefold()
            except (OSError, subprocess.SubprocessError):
                return
            if not (7 <= len(commit) <= 64) or any(
                char not in "0123456789abcdef" for char in commit
            ):
                return
            criterion_codes = tuple(
                f"verify-criterion:{criterion.criterion_id}"
                for criterion in self.requirement_contract.criteria
            )
            self.evolution_adapter.queue(
                EvolutionDraft(
                    task_id=self.task_runtime.task_id,
                    objective=self.requirement_contract.objective,
                    decision_code="repair-until-deterministic-evidence-pass",
                    procedure_codes=(
                        "address-structured-gate-failure",
                        "rerun-predeclared-verifiers",
                        "require-current-diff-receipt",
                        *criterion_codes,
                    ),
                    source_commit=commit,
                    source_code_hash=source_code_hash,
                    metadata={
                        "gate": "deterministic-pass",
                        "repair_signal": (
                            "prior-gate-nonpass"
                            if self._completion_had_nonpass
                            else "structured-tool-failure"
                        ),
                    },
                )
            )
            self.task_runtime.queue_evolution()
            self.evolution_adapter.flush_task(self.task_runtime.task_id)
        except Exception as exc:
            log.warning("Experience candidate capture skipped: %s", exc)

    def _inject_verification_feedback(
        self,
        conversation: ConversationManager,
        event: CompletionBlockedEvent,
    ) -> None:
        payload = {
            "type": "verification-feedback",
            "verdict": event.verdict,
            "reasons": list(event.reasons),
            "bundle_ref": event.bundle_ref,
            "instruction": (
                "Completion is not accepted. Address the failed criteria, use tools when "
                "needed, and do not claim success until deterministic verification passes."
            ),
        }
        conversation.add_system_reminder(
            "<verification-feedback>\n"
            + json.dumps(payload, ensure_ascii=False, sort_keys=True)
            + "\n</verification-feedback>"
        )

    async def _execute_single_tool_direct(
        self, tc: ToolCallComplete
    ) -> _ToolExecResult:
        tool = self.registry.get(tc.tool_name)
        start = time.monotonic()

        if tool is None:
            return _ToolExecResult(
                tool_id=tc.tool_id,
                tool_name=tc.tool_name,
                result=ToolResult(output=f"Error: unknown tool '{tc.tool_name}'", is_error=True),
                elapsed=time.monotonic() - start,
                is_unknown=True,
            )

        skill_denial = self._skill_tool_denial(tc.tool_name)
        if skill_denial is not None:
            return _ToolExecResult(
                tool_id=tc.tool_id,
                tool_name=tc.tool_name,
                result=ToolResult(output=skill_denial, is_error=True),
                elapsed=time.monotonic() - start,
                is_unknown=False,
            )

        if not self.registry.is_enabled(tc.tool_name):
            return _ToolExecResult(
                tool_id=tc.tool_id,
                tool_name=tc.tool_name,
                result=ToolResult(output=f"Error: tool '{tc.tool_name}' is disabled", is_error=True),
                elapsed=time.monotonic() - start,
                is_unknown=False,
            )

        result = await self._execute_via_gateway(tool, tc)

        self._snapshot_for_recovery(tc, result)

        return _ToolExecResult(
            tool_id=tc.tool_id,
            tool_name=tc.tool_name,
            result=result,
            elapsed=time.monotonic() - start,
            is_unknown=False,
        )


    async def _execute_batch_parallel(
        self, calls: list[ToolCallComplete]
    ) -> list[_ToolExecResult]:
        tasks = [self._execute_single_tool_direct(tc) for tc in calls]
        return list(await asyncio.gather(*tasks))

    async def _execute_tool(
        self, tc: ToolCallComplete
    ) -> AsyncIterator[tuple[ToolResult, float, bool]]:
        tool = self.registry.get(tc.tool_name)
        start = time.monotonic()
        is_unknown = False

        if tool is None:
            result = ToolResult(
                output=f"Error: unknown tool '{tc.tool_name}'", is_error=True
            )
            is_unknown = True
            elapsed = time.monotonic() - start
            yield result, elapsed, is_unknown
            return

        skill_denial = self._skill_tool_denial(tc.tool_name)
        if skill_denial is not None:
            result = ToolResult(output=skill_denial, is_error=True)
            elapsed = time.monotonic() - start
            yield result, elapsed, is_unknown
            return

        if not self.registry.is_enabled(tc.tool_name):
            result = ToolResult(
                output=f"Error: tool '{tc.tool_name}' is disabled in current mode",
                is_error=True,
            )
            elapsed = time.monotonic() - start
            yield result, elapsed, is_unknown
            return

        assessment = self._preview_execution(tool, tc)
        if assessment is not None and (
            not assessment.valid
            or (assessment.risk is not None and assessment.risk.hard_deny)
        ):
            result = await self._execute_via_gateway(tool, tc)
            elapsed = time.monotonic() - start
            yield result, elapsed, is_unknown
            return

        grant: InvocationGrant | None = None
        # 权限检查
        if self.permission_checker:
            decision = self.permission_checker.check(tool, tc.arguments)

            if decision.effect == "deny":
                result = ToolResult(
                    output=f"Permission denied: {decision.reason}",
                    is_error=True,
                )
                elapsed = time.monotonic() - start
                yield result, elapsed, is_unknown
                return

            if decision.effect == "ask":
                loop = asyncio.get_running_loop()
                future: asyncio.Future[PermissionResponse] = loop.create_future()
                desc = self._build_permission_description(tc, assessment)
                risk_level = (
                    str(assessment.risk.level)
                    if assessment is not None and assessment.risk is not None
                    else "unknown"
                )
                approval_scope = (
                    policy_for(assessment.risk).approval_scope
                    if assessment is not None and assessment.risk is not None
                    else "single_use"
                )
                # 向调用方 yield 权限请求事件，由调用方处理
                yield PermissionRequest(
                    tool_name=tc.tool_name,
                    description=desc,
                    future=future,
                    risk_level=risk_level,
                    reason_codes=(assessment.reason_codes if assessment else ()),
                    approval_scope=approval_scope,
                )
                response = await future

                if response == PermissionResponse.DENY:
                    result = ToolResult(
                        output="Permission denied: 用户拒绝了此操作",
                        is_error=True,
                    )
                    elapsed = time.monotonic() - start
                    yield result, elapsed, is_unknown
                    return

                if response == PermissionResponse.ALLOW_ALWAYS:
                    from mewcode.permissions.rules import Rule, extract_content
                    content = extract_content(tc.tool_name, tc.arguments)
                    pattern = f"{content[:60]}*" if len(content) > 60 else f"{content}*"
                    rule = Rule(tool_name=tc.tool_name, pattern=pattern, effect="allow")
                    self.permission_checker.rule_engine.append_local_rule(rule)

                grant = self._issue_execution_grant(
                    assessment,
                    approver=(
                        "user:interactive-always"
                        if response == PermissionResponse.ALLOW_ALWAYS
                        else "user:interactive"
                    ),
                )
            else:
                grant = self._issue_execution_grant(
                    assessment,
                    approver=f"permission-policy:{decision.reason}",
                )

        result = await self._execute_via_gateway(tool, tc, grant=grant)

        self._snapshot_for_recovery(tc, result)

        elapsed = time.monotonic() - start
        yield result, elapsed, is_unknown

    def _snapshot_for_recovery(
        self, tc: ToolCallComplete, result: ToolResult
    ) -> None:
        """捕获 ReadFile 刚交给模型的内容，以便 Layer 2 压缩对话后
        auto_compact 能重新附加这些数据。每次 ReadFile 多一次磁盘读取，
        比从 tool 输出中反向解析行号要划算。
        """
        if result.is_error or tc.tool_name != "ReadFile":
            return
        path = tc.arguments.get("file_path") if isinstance(tc.arguments, dict) else None
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                content = fh.read()
        except OSError:
            return
        self.recovery_state.record_file_read(path, content)

    async def _extract_memories(
        self, conversation: ConversationManager
    ) -> None:
        if self._extracting or not self.memory_manager:
            return
        self._extracting = True
        try:
            source_task = (
                self.task_runtime.task_id
                if self.task_runtime is not None
                else None
            )
            source_trace = (
                self.task_runtime.run.trace_id
                if self.task_runtime is not None
                else None
            )
            await self.memory_manager.extract(
                self.client,
                conversation,
                self.protocol,
                source_task=source_task,
                source_trace=source_trace,
            )
        except Exception as e:
            self.memory_manager.record_diagnostic(
                "MEMORY_EXTRACTION_FAILED",
                type(e).__name__,
            )
        finally:
            self._extracting = False

    async def manual_compact(
        self, conversation: ConversationManager
    ) -> CompactNotification | ErrorEvent:
        # auto_compact 会用摘要替换 conversation.history，所有 tool-result 内容
        # （原始或已替换的）都将被丢弃。这里跳过 apply_tool_result_budget —
        # 它在主循环中的唯一目的是为 LLM 调用生成 api_conv，而本路径不需要
        # 发起看到替换结果的 LLM 调用（auto_compact 内部的摘要调用操作的是原始对话）。
        result = await auto_compact(
            conversation,
            self.client,
            self.context_window,
            self.session_dir,
            protocol=self.protocol,
            manual=True,
            breaker=self.compact_breaker,
            recovery=self.recovery_state,
            tool_schemas=self._tool_schemas_for_active_skills(),
            transcript_path=self._transcript_path,
        )
        if isinstance(result, CompactEvent):
            env_context = build_environment_context(
            self.work_dir, self.active_skills, self._skill_catalog, self._agent_catalog
        )
            conversation.inject_environment(env_context)
            memory_content = self.memory_manager.load() if self.memory_manager else ""
            conversation.inject_long_term_memory(
                self.instructions_content, memory_content
            )
            return CompactNotification(
                before_tokens=result.before_tokens,
                message=f"上下文已压缩（压缩前 {result.before_tokens:,} tokens）",
                boundary=result.boundary,
            )
        return ErrorEvent(message=result or "压缩失败：对话历史为空或未达到压缩条件")

    async def run_to_completion(
        self, task: str, conversation: ConversationManager | None = None,
        event_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> str:
        if conversation is None:
            conversation = ConversationManager()

        # CLI callers deliberately pass a ConversationManager so that teammate
        # notifications share one history.  Environment/instructions must not
        # depend on who allocated that object, and env_context must always exist
        # when auto-compaction later rebuilds the prefix.
        env_context = build_environment_context(
            self.work_dir, self.active_skills, self._skill_catalog, self._agent_catalog
        )
        conversation.inject_environment(env_context)
        memory_content = self.memory_manager.load() if self.memory_manager else ""
        conversation.inject_long_term_memory(
            self.instructions_content, memory_content
        )

        if task:
            conversation.add_user_message(task)

        tools = self._tool_schemas_for_active_skills()

        log.info(
            "[run_to_completion] agent=%s tools=%d names=%s coordinator=%s",
            self.agent_id,
            len(tools),
            [t["name"] for t in tools][:10],
            self.coordinator_mode,
        )

        last_text = ""

        for iteration in range(1, self.max_iterations + 1):
            if self.hook_engine:
                ctx = self._build_hook_context("turn_start")
                await self.hook_engine.run_hooks("turn_start", ctx)

            # Prompt actions produced by this turn's Hook execution must enter
            # this turn's system prompt.  Building the prompt before
            # ``turn_start`` silently delayed (and, for one-shot headless
            # runs, discarded) the declared action.
            hook_prompts = (
                self.hook_engine.get_prompt_messages()
                if self.hook_engine
                else None
            )
            system = build_system_prompt(
                hook_prompts=hook_prompts,
                coordinator_mode=self.coordinator_mode,
                agent_catalog=self._agent_catalog_list or None,
                work_dir=self.work_dir,
            )

            self._consume_mailbox(conversation)
            if self.notification_fn:
                for note in self.notification_fn():
                    conversation.add_system_reminder(note)

            compact_result = await auto_compact(
                conversation,
                self.client,
                self.context_window,
                self.session_dir,
                protocol=self.protocol,
                breaker=self.compact_breaker,
                recovery=self.recovery_state,
                tool_schemas=self._tool_schemas_for_active_skills(),
                transcript_path=self._transcript_path,
            )
            if isinstance(compact_result, CompactEvent):
                if compact_result.boundary is not None:
                    self._compact_boundaries.append(compact_result.boundary)
                conversation.inject_environment(env_context)
                memory_content = self.memory_manager.load() if self.memory_manager else ""
                conversation.inject_long_term_memory(
                    self.instructions_content, memory_content
                )

            deferred_names = self._deferred_tool_names_for_active_skills()
            if deferred_names:
                conversation.add_system_reminder(
                    "The following deferred tools are available via ToolSearch. "
                    "Their schemas are NOT loaded - use ToolSearch with "
                    'query "select:<name>[,<name>...]" to load tool schemas before calling them:\n'
                    + "\n".join(deferred_names)
                )

            api_conv, _new_records = apply_tool_result_budget(
                conversation, self.session_dir, self.replacement_state
            )
            if _new_records:
                append_replacement_records(self.session_dir, _new_records)

            # LoadSkill may have activated a stricter allowlist in the previous
            # iteration, so headless mode must refresh schemas per turn.
            tools = self._tool_schemas_for_active_skills()
            collector = StreamCollector()
            llm_stream = self.client.stream(api_conv, system=system, tools=tools)
            async for _event in collector.consume(llm_stream):
                pass

            response = collector.response
            self.total_input_tokens += response.input_tokens
            self.total_output_tokens += response.output_tokens

            if event_callback:
                event_callback({
                    "type": "usage",
                    "usage": {
                        "inputTokens": self.total_input_tokens,
                        "outputTokens": self.total_output_tokens,
                    },
                })

            if response.text:
                last_text = response.text
                if event_callback:
                    event_callback({
                        "type": "stream_text",
                        "text": response.text,
                    })

            log.info(
                "[run_to_completion] agent=%s iter=%d tool_calls=%d text_len=%d stop=%s",
                self.agent_id, iteration, len(response.tool_calls),
                len(response.text), response.stop_reason,
            )

            if not response.tool_calls:
                conversation.add_assistant_message(response.text)
                if self.hook_engine:
                    ctx = self._build_hook_context("turn_end")
                    await self.hook_engine.run_hooks("turn_end", ctx)
                completion_block = await self._attempt_completion(conversation)
                if completion_block is not None:
                    if event_callback:
                        event_callback({
                            "type": "completion_blocked",
                            "verdict": completion_block.verdict,
                            "reasons": list(completion_block.reasons),
                            "bundleRef": completion_block.bundle_ref,
                        })
                    if self._completion_gate_exhausted:
                        break
                    continue
                if self.file_history is not None:
                    summary = response.text[:60] + "..." if len(response.text) > 60 else response.text
                    self.file_history.make_snapshot(len(conversation.history), summary)
                break

            tool_uses = [
                ToolUseBlock(
                    tool_use_id=tc.tool_id,
                    tool_name=tc.tool_name,
                    arguments=tc.arguments,
                )
                for tc in response.tool_calls
            ]
            conversation.add_assistant_message(response.text, tool_uses)
            # assistant 回复已在历史中，锚定实际用量；下一轮迭代只需对
            # 下方追加的 tool results 做字符估算。
            conversation.record_usage_anchor(
                response.input_tokens,
                response.output_tokens,
                response.cache_read,
                response.cache_creation,
            )

            tool_results: list[ToolResultBlock] = []
            for tc in response.tool_calls:
                if event_callback:
                    event_callback({
                        "type": "tool_use",
                        "toolName": tc.tool_name,
                        "args": tc.arguments,
                    })
                result = await self._execute_tool_noninteractive(tc)
                content = self._maybe_persist_or_truncate(tc.tool_id, result.output)
                tool_results.append(
                    ToolResultBlock(
                        tool_use_id=tc.tool_id,
                        content=content,
                        is_error=result.is_error,
                    )
                )

            conversation.add_tool_results_message(tool_results)

            if self.hook_engine:
                ctx = self._build_hook_context("turn_end")
                await self.hook_engine.run_hooks("turn_end", ctx)

        if self.requirement_contract is not None and not self._evidence_passed:
            if self._last_completion_block is None:
                self._last_completion_block = CompletionBlockedEvent(
                    verdict=GateVerdict.PARTIAL.value,
                    reasons=("iteration budget ended before deterministic verification passed",),
                    bundle_ref="unavailable",
                )
            raise CompletionBlockedError(self._last_completion_block)
        return last_text

    async def _execute_tool_noninteractive(
        self, tc: ToolCallComplete
    ) -> ToolResult:
        tool = self.registry.get(tc.tool_name)

        if tool is None:
            return ToolResult(
                output=f"Error: unknown tool '{tc.tool_name}'", is_error=True
            )

        skill_denial = self._skill_tool_denial(tc.tool_name)
        if skill_denial is not None:
            return ToolResult(output=skill_denial, is_error=True)

        if not self.registry.is_enabled(tc.tool_name):
            return ToolResult(
                output=f"Error: tool '{tc.tool_name}' is disabled",
                is_error=True,
            )

        if self.hook_engine:
            file_path = self._infer_file_path(tc.arguments)
            hook_ctx = self._build_hook_context(
                "pre_tool_use",
                tool_name=tc.tool_name,
                tool_args=tc.arguments,
                file_path=file_path,
            )
            rejection = await self.hook_engine.run_pre_tool_hooks(hook_ctx)
            if rejection is not None:
                return ToolResult(
                    output=f"Hook rejected: {rejection.reason}",
                    is_error=True,
                )

        assessment = self._preview_execution(tool, tc)
        if assessment is not None and (
            not assessment.valid
            or (assessment.risk is not None and assessment.risk.hard_deny)
        ):
            return await self._execute_via_gateway(tool, tc)

        grant: InvocationGrant | None = None
        if self.permission_checker:
            decision = self.permission_checker.check(tool, tc.arguments)
            if decision.effect == "deny":
                return ToolResult(
                    output=f"Permission denied: {decision.reason}",
                    is_error=True,
                )
            if decision.effect == "ask":
                if self.permission_mode == PermissionMode.DONT_ASK:
                    pass  # 自动批准
                elif self._active_execution_context().plan_hash.startswith("automation:"):
                    # A user-selected automation manifest supplies exact
                    # write/argv/host constraints.  The gateway preview above
                    # has already rejected any drift, so this scoped grant is
                    # narrower than switching the whole agent to dontAsk.
                    pass
                else:
                    return ToolResult(
                        output="Permission denied: non-interactive agent cannot prompt user",
                        is_error=True,
                    )
            context = self._active_execution_context()
            approval_source = (
                f"automation-manifest:{context.plan_hash.removeprefix('automation:')[:16]}"
                if context.plan_hash.startswith("automation:")
                else f"noninteractive-policy:{decision.reason}"
            )
            grant = self._issue_execution_grant(
                assessment,
                approver=approval_source,
            )

        result = await self._execute_via_gateway(tool, tc, grant=grant)

        if self.hook_engine:
            file_path = self._infer_file_path(tc.arguments)
            hook_ctx = self._build_hook_context(
                "post_tool_use",
                tool_name=tc.tool_name,
                tool_args=tc.arguments,
                file_path=file_path,
            )
            await self.hook_engine.run_hooks("post_tool_use", hook_ctx)

        return result

    def _maybe_persist_or_truncate(self, tool_use_id: str, text: str) -> str:
        from mewcode.context.manager import (
            SINGLE_RESULT_CHAR_LIMIT,
            make_persisted_preview,
            persist_tool_result,
        )

        if len(text) > SINGLE_RESULT_CHAR_LIMIT:
            fp = persist_tool_result(tool_use_id, text, self.session_dir)
            return make_persisted_preview(text, fp)
        if len(text) > MAX_OUTPUT_CHARS:
            return text[:MAX_OUTPUT_CHARS] + "\n… (output truncated)"
        return text

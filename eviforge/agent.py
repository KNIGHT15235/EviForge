from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from pydantic import ValidationError

from eviforge.client import LLMClient
from eviforge.reliability import RetryPolicy, stream_with_recovery
from eviforge.context import (
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
from eviforge.conversation import ConversationManager, ToolResultBlock, ToolUseBlock
from eviforge.conversation import ThinkingBlock as ConvThinkingBlock
from eviforge.memory.auto_memory import MemoryManager
from eviforge.permissions import (
    Decision,
    PermissionChecker,
    PermissionMode,
)
from eviforge.hooks import HookContext, HookEngine, ToolRejectedError
from eviforge.hooks.engine import HookNotification
from eviforge.prompts import build_environment_context, build_plan_mode_reminder, build_system_prompt
from eviforge.tools import ToolRegistry
from eviforge.tools.base import (
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
    RetryNotice,
)
from eviforge.tools.work_dir import resolve_tool_path, tool_working_directory

log = logging.getLogger(__name__)

MEMORY_EXTRACTION_INTERVAL = 5
MAX_TOKENS_CEILING = 64000
MAX_OUTPUT_TOKENS_RECOVERIES = 3


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


AgentEvent = (
    StreamText
    | ThinkingText
    | RetryEvent
    | ToolUseEvent
    | ToolResultEvent
    | TurnComplete
    | LoopComplete
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
            elif isinstance(event, RetryNotice):
                yield RetryEvent(reason=f"{event.reason} (attempt {event.attempt})", wait=event.wait)


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
        safe = tool is not None and tool.is_concurrency_safe and registry.is_enabled(tc.tool_name)

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
        query_source: str = "main",
        system_prompt_override: str | None = None,
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
        self.query_source = query_source
        self.system_prompt_override = system_prompt_override
        self._last_system_prompt: str | None = None
        self._current_conversation: ConversationManager | None = None
        self._loop_count = 0
        self._extracting = False
        self.session_id: str = ""
        self.active_skills: dict[str, str] = {}
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
        self.file_history: Any = None
        self.retry_policy = RetryPolicy()
        self.last_run_status = "idle"
        self.last_run_error = ""
        self._background_tasks: set[asyncio.Task] = set()
        self._run_event_callback: Callable[[dict[str, Any]], None] | None = None
        self.turn_id = ""
        self.plan_service = None
        self.skill_loader = None
        self.children: list[Agent] = []
        self.runtime = None

    def bind_child(self, child: Agent) -> None:
        """Inherit policy context without copying one-time authorization grants."""
        self.children.append(child)
        child.session_id = self.session_id
        child.turn_id = self.turn_id
        child.retry_policy = self.retry_policy
        child.runtime = self.runtime
        if self.plan_service is not None:
            self.plan_service.inherit(self, child)
        if self.memory_manager is not None and getattr(self.memory_manager, "governance", None) is not None:
            child.memory_manager = self.memory_manager
            child.skill_loader = self.skill_loader

    def _legacy_memory_content(self) -> str:
        if self.memory_manager is None or getattr(self.memory_manager, "governance", None) is not None:
            return ""
        return self.memory_manager.load()

    def _refresh_governed_context(self, conversation: ConversationManager) -> None:
        if self.memory_manager is not None and getattr(self.memory_manager, "governance", None) is not None:
            self.memory_manager.refresh_context(conversation)
        if self.skill_loader is not None:
            self.skill_loader.refresh_active_skills(self)

    def spawn_background(self, coroutine: Any, *, name: str = "agent-background") -> asyncio.Task:
        task = asyncio.create_task(coroutine, name=name)
        self._background_tasks.add(task)
        def finished(done: asyncio.Task) -> None:
            self._background_tasks.discard(done)
            if not done.cancelled():
                error = done.exception()
                if error is not None:
                    log.warning("Agent background task failed: %s", error)
        task.add_done_callback(finished)
        return task

    async def wait_background(self, timeout: float | None = None) -> None:
        if self._background_tasks:
            _, pending = await asyncio.wait(tuple(self._background_tasks), timeout=timeout)
            if pending:
                raise TimeoutError("Agent background tasks are still running")

    async def cancel_background(self) -> None:
        tasks = tuple(self._background_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for child in self.children:
            await child.cancel_background()

    async def _provider_stream(self, conversation: ConversationManager, *, system: str, tools: list[dict[str, Any]]) -> AsyncIterator[StreamEvent]:
        try:
            async for event in stream_with_recovery(
                self.client, conversation, system=system, tools=tools,
                policy=self.retry_policy, on_retry=self._run_event_callback,
            ):
                yield event
        except asyncio.CancelledError:
            self.last_run_status = "cancelled"
            raise
        except Exception as exc:
            from eviforge.client import AmbiguousStreamError
            self.last_run_status = "ambiguous" if isinstance(exc, AmbiguousStreamError) else "failed"
            self.last_run_error = str(exc)
            raise

    @property
    def _transcript_path(self) -> str:
        if self.session_id:
            return str(Path(self.work_dir) / ".eviforge" / "sessions" / f"{self.session_id}.jsonl")
        return ""

    @property
    def plan_mode(self) -> bool:
        return self.permission_mode == PermissionMode.PLAN

    _plan_path_cache: Path | None = None

    def _get_plan_path(self) -> Path:
        if self._plan_path_cache is not None:
            return self._plan_path_cache
        import random
        import datetime
        _ADJECTIVES = ["bold", "bright", "calm", "cool", "deep", "fair", "fast", "fine",
                       "glad", "keen", "kind", "lean", "mild", "neat", "pure", "safe",
                       "slim", "soft", "tall", "warm", "wise", "grand", "swift", "vivid"]
        _NOUNS = ["sketch", "draft", "spark", "bloom", "trail", "ridge", "creek", "grove",
                  "cliff", "cloud", "field", "forge", "frost", "haven", "pearl", "stone",
                  "storm", "river", "tower", "delta", "flame", "orbit", "pulse", "shore"]
        plans_dir = Path(self.work_dir) / ".eviforge" / "plans"
        plans_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now().strftime("%m%d-%H%M")
        slug = f"{random.choice(_ADJECTIVES)}-{random.choice(_NOUNS)}-{ts}"
        self._plan_path_cache = plans_dir / f"{slug}.md"
        return self._plan_path_cache

    def set_permission_mode(self, mode: PermissionMode) -> None:
        self.permission_mode = mode
        if self.permission_checker:
            self.permission_checker.mode = mode

    def activate_skill(self, name: str, prompt_body: str) -> None:
        self.active_skills[name] = prompt_body

    def clear_active_skills(self) -> None:
        self.active_skills.clear()

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
            work_dir=str(kwargs.get("work_dir", self.work_dir)),
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
        self.last_run_status = "running"
        self.last_run_error = ""
        runtime = getattr(self, "runtime", None)
        if runtime is not None and runtime.mcp_manager.required_failures:
            self.last_run_status = "failed"
            self.last_run_error = "Required MCP services unavailable: " + ", ".join(runtime.mcp_manager.required_failures)
            yield ErrorEvent(message=self.last_run_error)
            return
        self.turn_id = self.turn_id or uuid.uuid4().hex
        self._current_conversation = conversation
        env_context = build_environment_context(
            self.work_dir, self.active_skills, self._skill_catalog, self._agent_catalog
        )
        conversation.inject_environment(env_context)

        memory_content = self._legacy_memory_content()
        conversation.inject_long_term_memory(self.instructions_content, memory_content)

        if self.hook_engine:
            ctx = self._build_hook_context("session_start")
            await self.hook_engine.run_hooks("session_start", ctx)
            for he in self._drain_hook_events():
                yield he

        iteration = 0
        consecutive_unknown = 0
        permission_denied = False
        max_tokens_escalated = False
        output_recoveries = 0

        while True:
            iteration += 1

            if iteration > self.max_iterations:
                self.last_run_status = "failed"
                self.last_run_error = "Agent reached maximum iterations"
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
            self._refresh_governed_context(conversation)
            compact_result = await auto_compact(
                conversation,
                self.client,
                self.context_window,
                self.session_dir,
                protocol=self.protocol,
                breaker=self.compact_breaker,
                recovery=self.recovery_state,
                tool_schemas=self.registry.get_all_schemas(self.protocol),
                transcript_path=self._transcript_path,
                retry_policy=self.retry_policy,
            )
            if isinstance(compact_result, CompactEvent):
                yield CompactNotification(
                    before_tokens=compact_result.before_tokens,
                    message=f"上下文已压缩（压缩前 {compact_result.before_tokens:,} tokens）",
                    boundary=compact_result.boundary,
                )
                env_context = build_environment_context(
                    self.work_dir,
                    self.active_skills,
                    self._skill_catalog,
                    self._agent_catalog,
                )
                conversation.inject_environment(env_context)
                mem = self._legacy_memory_content()
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
                self.hook_engine.get_prompt_messages()
                if self.hook_engine and self.system_prompt_override is None else None
            )
            system = build_system_prompt(
                hook_prompts=hook_prompts,
                coordinator_mode=self.coordinator_mode,
                agent_catalog=self._agent_catalog_list or None,
            )
            if self.system_prompt_override is not None:
                system = self.system_prompt_override
            self._last_system_prompt = system

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

            deferred_names = self.registry.get_deferred_tool_names()
            if deferred_names:
                conversation.add_system_reminder(
                    "The following deferred tools are available via ToolSearch. "
                    "Their schemas are NOT loaded - use ToolSearch with "
                    'query "select:<name>[,<name>...]" to load tool schemas before calling them:\n'
                    + "\n".join(deferred_names)
                )

            tools = self.registry.get_all_schemas(self.protocol)

            # Layer 1: 在 LLM 调用前应用 tool-result budget，确保 api_conv 反映
            # 本轮迭代中所有已发生的写入（system reminders、hook 通知等）。
            # 原始 conversation 不会被修改；替换决策保存在 self.replacement_state 中。
            self._refresh_governed_context(conversation)
            api_conv, _new_records = apply_tool_result_budget(
                conversation, self.session_dir, self.replacement_state
            )
            if _new_records:
                append_replacement_records(self.session_dir, _new_records)

            collector = StreamCollector()
            llm_stream = self._provider_stream(api_conv, system=system, tools=tools)
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
                self.last_run_status = "failed"
                self.last_run_error = "OUTPUT_LIMIT: response remained truncated after the continuation budget was exhausted"
                conversation.add_assistant_message(response.text, thinking_blocks=conv_thinking)
                yield ErrorEvent(message=self.last_run_error)
                yield LoopComplete(total_turns=iteration)
                break
            else:
                output_recoveries = 0

            if response.stop_reason not in {"end_turn", "stop_sequence", "tool_use"}:
                self.last_run_status = "blocked" if response.stop_reason == "content_filter" else "failed"
                self.last_run_error = f"PROVIDER_INCOMPLETE: response terminated with {response.stop_reason or 'missing stop reason'}"
                conversation.add_assistant_message(response.text, thinking_blocks=conv_thinking)
                yield ErrorEvent(message=self.last_run_error)
                yield LoopComplete(total_turns=iteration)
                break

            if not response.tool_calls:
                conversation.add_assistant_message(
                    response.text, thinking_blocks=conv_thinking
                )
                self._loop_count += 1
                if (
                    self._loop_count % MEMORY_EXTRACTION_INTERVAL == 0
                    and self.memory_manager
                ):
                    self.spawn_background(self._extract_memories(conversation), name="memory-extraction")
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
                self.last_run_status = "blocked" if permission_denied else "success"
                if permission_denied:
                    self.last_run_error = "One or more requested tool actions were denied"
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
            batches = partition_tool_calls(response.tool_calls, self.registry)

            for batch in batches:
                if (
                    batch.concurrent
                    and len(batch.calls) > 1
                    and self._batch_can_run_parallel(batch.calls)
                ):
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
                else:
                    for tc in batch.calls:
                        execution_cwd = self.work_dir
                        result: ToolResult | None = None
                        elapsed = 0.0
                        is_unknown = False

                        if self.hook_engine:
                            file_path = self._infer_file_path(tc.arguments)
                            hook_ctx = self._build_hook_context(
                                "pre_tool_use",
                                tool_name=tc.tool_name,
                                tool_args=tc.arguments,
                                work_dir=execution_cwd,
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

                        async for item in self._execute_tool(
                            tc, execution_cwd=execution_cwd
                        ):
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
                                work_dir=execution_cwd,
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

            if consecutive_unknown >= 3:
                self.last_run_status = "failed"
                self.last_run_error = "Too many consecutive unknown tool calls"
                conversation.add_tool_results_message(tool_results)
                yield ErrorEvent(
                    message="Agent terminated: too many consecutive unknown tool calls"
                )
                break

            permission_denied = permission_denied or any(
                result.is_error and result.content.startswith(("Permission denied:", "Plan denied [", "Hook rejected:"))
                for result in tool_results)
            exit_plan_ids = {tc.tool_id for tc in response.tool_calls if tc.tool_name == "ExitPlanMode"}
            exit_plan_called = any(result.tool_use_id in exit_plan_ids and not result.is_error
                                   for result in tool_results)
            conversation.add_tool_results_message(tool_results)
            if self.last_run_status == "ambiguous":
                yield TurnComplete(turn=iteration)
                yield LoopComplete(total_turns=iteration)
                break
            if exit_plan_called:
                self.last_run_status = "approval_required"
                self.last_run_error = "Plan submitted; explicit approval of its content hash is required"
                yield TurnComplete(turn=iteration)
                yield LoopComplete(total_turns=iteration)
                break

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

    def _build_permission_description(self, tc: ToolCallComplete) -> str:
        if tc.tool_name == "Bash":
            return tc.arguments.get("command", tc.tool_name)
        if tc.tool_name in ("ReadFile", "WriteFile", "EditFile"):
            return tc.arguments.get("file_path", tc.tool_name)
        return str(tc.arguments)

    def _check_tool_permission(
        self, tool: Any, arguments: dict[str, Any], work_dir: str
    ) -> Decision | None:
        from eviforge.mcp.tool_wrapper import MCPToolWrapper
        if type(tool) is MCPToolWrapper:
            try:
                tool.resource_ids(arguments, work_dir)
            except ValueError as exc:
                return Decision("deny", str(exc))
        if self.plan_service is not None:
            gate = self.plan_service.precheck(self, tool, arguments, work_dir)
            if gate is not None:
                if not gate.allowed:
                    return Decision("deny", gate.reason)
                # A valid plan grant may satisfy an ASK, but never an explicit DENY.
                if self.permission_checker is not None:
                    with tool_working_directory(work_dir):
                        existing = self.permission_checker.check(tool, arguments)
                    if existing.effect == "deny":
                        return existing
                return Decision("allow", gate.reason)
        if self.permission_checker is None:
            return None
        with tool_working_directory(work_dir):
            return self.permission_checker.check(tool, arguments)

    async def _execute_tool_in_work_dir(
        self, tool: Any, arguments: dict[str, Any], work_dir: str
    ) -> ToolResult:
        if not self.registry.is_enabled(tool.name) or self.registry.get(tool.name) is not tool:
            return ToolResult(output=f"Permission denied: tool '{tool.name}' was disabled or replaced before execution", is_error=True)
        rejection = self._fork_tool_rejection(tool.name)
        if rejection is not None:
            return rejection
        # Bind ``file_history`` even when it is None.  Tool instances can be
        # shared by multiple Agents, so their constructor-level history is only
        # a fallback for direct calls, never implicit Agent state.
        with tool_working_directory(work_dir, file_history=self.file_history):
            params = tool.validate_arguments(arguments)
            if self.permission_checker is not None:
                final_decision = self.permission_checker.check(tool, arguments)
                if final_decision.effect == "deny":
                    return ToolResult(output=f"Permission denied: {final_decision.reason}", is_error=True)
            if self.plan_service is not None:
                gate = self.plan_service.consume(self, tool, arguments, work_dir)
                if gate is not None and not gate.allowed:
                    return ToolResult(output=f"Plan denied [{gate.code}]: {gate.reason}", is_error=True)
            try:
                result = await tool.execute(params)
            except BaseException:
                if self.plan_service is not None:
                    self.plan_service.record_result(self, tool.name, is_error=True)
                raise
            if self.plan_service is not None:
                self.plan_service.record_result(self, tool.name, is_error=result.is_error)
            if result.execution_status in {"ambiguous", "completed_unarchived"}:
                self.last_run_status = "ambiguous"
                self.last_run_error = result.output
            return result

    def _fork_tool_rejection(self, tool_name: str) -> ToolResult | None:
        if self.query_source == "fork" and tool_name in {
            "Agent", "TeamCreate", "TeamDelete", "EnterWorktree", "ExitWorktree",
            "AskUserQuestion", "TaskStop", "TaskOutput", "ExitPlanMode",
        }:
            return ToolResult(
                output=f"Fork workers cannot invoke control tool '{tool_name}'.",
                is_error=True,
            )
        return None

    async def _execute_single_tool_direct(
        self, tc: ToolCallComplete
    ) -> _ToolExecResult:
        execution_cwd = self.work_dir
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

        if not self.registry.is_enabled(tc.tool_name):
            return _ToolExecResult(
                tool_id=tc.tool_id,
                tool_name=tc.tool_name,
                result=ToolResult(output=f"Error: tool '{tc.tool_name}' is disabled", is_error=True),
                elapsed=time.monotonic() - start,
                is_unknown=False,
            )

        if self.permission_checker:
            decision = self._check_tool_permission(tool, tc.arguments, execution_cwd)
            assert decision is not None
            if decision.effect != "allow":
                reason = decision.reason
                if decision.effect == "ask":
                    reason = "parallel execution requires interactive approval"
                return _ToolExecResult(
                    tool_id=tc.tool_id,
                    tool_name=tc.tool_name,
                    result=ToolResult(
                        output=f"Permission denied: {reason}",
                        is_error=True,
                    ),
                    elapsed=time.monotonic() - start,
                    is_unknown=False,
                )

        try:
            result = await self._execute_tool_in_work_dir(
                tool, tc.arguments, execution_cwd
            )
        except ValidationError as e:
            result = ToolResult(output=f"Parameter validation error: {e}", is_error=True)
        except Exception as e:
            result = ToolResult(output=f"Tool execution error: {e}", is_error=True)

        self._snapshot_for_recovery(tc, result, execution_cwd)

        return _ToolExecResult(
            tool_id=tc.tool_id,
            tool_name=tc.tool_name,
            result=result,
            elapsed=time.monotonic() - start,
            is_unknown=False,
        )


    def _batch_can_run_parallel(self, calls: list[ToolCallComplete]) -> bool:
        if self.hook_engine is not None:
            return False
        if self.permission_checker is None:
            return True
        for tc in calls:
            tool = self.registry.get(tc.tool_name)
            if tool is None or not self.registry.is_enabled(tc.tool_name):
                return False
            decision = self._check_tool_permission(tool, tc.arguments, self.work_dir)
            if decision is None or decision.effect != "allow":
                return False
        return True


    async def _execute_batch_parallel(
        self, calls: list[ToolCallComplete]
    ) -> list[_ToolExecResult]:
        tasks = [self._execute_single_tool_direct(tc) for tc in calls]
        return list(await asyncio.gather(*tasks))

    async def _execute_tool(
        self,
        tc: ToolCallComplete,
        execution_cwd: str | None = None,
    ) -> AsyncIterator[tuple[ToolResult, float, bool]]:
        if execution_cwd is None:
            execution_cwd = self.work_dir
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

        if not self.registry.is_enabled(tc.tool_name):
            result = ToolResult(
                output=f"Error: tool '{tc.tool_name}' is disabled in current mode",
                is_error=True,
            )
            elapsed = time.monotonic() - start
            yield result, elapsed, is_unknown
            return

        # 权限检查
        if self.permission_checker:
            decision = self._check_tool_permission(tool, tc.arguments, execution_cwd)
            assert decision is not None

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
                desc = self._build_permission_description(tc)
                # 向调用方 yield 权限请求事件，由调用方处理
                yield PermissionRequest(
                    tool_name=tc.tool_name,
                    description=desc,
                    future=future,
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
                    from eviforge.permissions.rules import Rule, extract_content
                    content = extract_content(tc.tool_name, tc.arguments)
                    pattern = f"{content[:60]}*" if len(content) > 60 else f"{content}*"
                    rule = Rule(tool_name=tc.tool_name, pattern=pattern, effect="allow")
                    self.permission_checker.rule_engine.append_local_rule(rule)

        try:
            result = await self._execute_tool_in_work_dir(
                tool, tc.arguments, execution_cwd
            )
        except ValidationError as e:
            result = ToolResult(
                output=f"Parameter validation error: {e}", is_error=True
            )
        except Exception as e:
            result = ToolResult(
                output=f"Tool execution error: {e}", is_error=True
            )

        self._snapshot_for_recovery(tc, result, execution_cwd)

        elapsed = time.monotonic() - start
        yield result, elapsed, is_unknown

    def _snapshot_for_recovery(
        self, tc: ToolCallComplete, result: ToolResult, work_dir: str
    ) -> None:
        """捕获 ReadFile 刚交给模型的内容，以便 Layer 2 压缩对话后
        auto_compact 能重新附加这些数据。每次 ReadFile 多一次磁盘读取，
        比从 tool 输出中反向解析行号要划算。
        """
        if result.is_error or tc.tool_name != "ReadFile":
            return
        raw_path = tc.arguments.get("file_path") if isinstance(tc.arguments, dict) else None
        if not raw_path:
            return
        with tool_working_directory(work_dir):
            path = resolve_tool_path(raw_path)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                content = fh.read()
        except OSError:
            return
        self.recovery_state.record_file_read(str(path), content)

    async def _extract_memories(
        self, conversation: ConversationManager
    ) -> None:
        if self._extracting or not self.memory_manager:
            return
        self._extracting = True
        try:
            if getattr(self.memory_manager, "governance", None) is not None:
                await self.memory_manager.extract(self.client, conversation, self.protocol,
                    source_task=self.session_id or self.agent_id,
                    source_trace=self.trace_id or self.agent_id, policy=self.retry_policy)
            else:
                await self.memory_manager.extract(self.client, conversation, self.protocol)
        except Exception as e:
            log.debug("Memory extraction failed: %s", e)
        finally:
            self._extracting = False

    async def manual_compact(
        self, conversation: ConversationManager
    ) -> CompactNotification | ErrorEvent:
        # auto_compact 会用摘要替换 conversation.history，所有 tool-result 内容
        # （原始或已替换的）都将被丢弃。这里跳过 apply_tool_result_budget —
        # 它在主循环中的唯一目的是为 LLM 调用生成 api_conv，而本路径不需要
        # 发起看到替换结果的 LLM 调用（auto_compact 内部的摘要调用操作的是原始对话）。
        self._refresh_governed_context(conversation)
        result = await auto_compact(
            conversation,
            self.client,
            self.context_window,
            self.session_dir,
            protocol=self.protocol,
            manual=True,
            breaker=self.compact_breaker,
            recovery=self.recovery_state,
            tool_schemas=self.registry.get_all_schemas(self.protocol),
            transcript_path=self._transcript_path,
            retry_policy=self.retry_policy,
        )
        if isinstance(result, CompactEvent):
            env_context = build_environment_context(
            self.work_dir, self.active_skills, self._skill_catalog, self._agent_catalog
        )
            conversation.inject_environment(env_context)
            memory_content = self._legacy_memory_content()
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
        """Headless adapter over the same ReAct loop used by the TUI."""
        conversation = conversation if conversation is not None else ConversationManager()
        if task:
            conversation.add_user_message(task)
        self._run_event_callback = event_callback
        last_text = ""
        current_text = ""
        try:
            async for event in self.run(conversation):
                payload = None
                if isinstance(event, PermissionRequest):
                    if not event.future.done():
                        event.future.set_result(PermissionResponse.DENY)
                    payload = {"type": "permission_denied", "toolName": event.tool_name,
                               "reason": "Interactive approval is unavailable in headless mode"}
                elif isinstance(event, StreamText):
                    current_text += event.text
                    last_text = current_text
                    payload = {"type": "stream_text", "text": event.text}
                elif isinstance(event, ToolUseEvent):
                    payload = {"type": "tool_use", "toolName": event.tool_name,
                               "toolId": event.tool_id, "args": event.arguments}
                elif isinstance(event, ToolResultEvent):
                    payload = {"type": "tool_result", "toolName": event.tool_name,
                               "toolId": event.tool_id, "output": event.output,
                               "is_error": event.is_error, "elapsed": event.elapsed}
                elif isinstance(event, UsageEvent):
                    payload = {"type": "usage", "usage": {"inputTokens": event.input_tokens,
                               "outputTokens": event.output_tokens}}
                elif isinstance(event, TurnComplete):
                    current_text = ""
                    payload = {"type": "turn_complete", "turn": event.turn}
                elif isinstance(event, CompactNotification):
                    callback = getattr(self, "_session_compact_callback", None)
                    if callback is not None and event.boundary is not None:
                        callback(event.boundary)
                    payload = {"type": "compact", "before_tokens": event.before_tokens,
                               "message": event.message}
                elif isinstance(event, HookEvent):
                    payload = {"type": "hook", "hook_id": event.hook_id,
                               "event": event.event, "output": event.output, "success": event.success}
                elif isinstance(event, ErrorEvent):
                    payload = {"type": "agent_error", "message": event.message}
                if event_callback and payload:
                    event_callback(payload)
            return last_text
        except asyncio.CancelledError:
            self.last_run_status = "cancelled"
            raise
        finally:
            self._run_event_callback = None

    async def _execute_tool_noninteractive(
        self, tc: ToolCallComplete
    ) -> ToolResult:
        rejection = self._fork_tool_rejection(tc.tool_name)
        if rejection is not None:
            return rejection
        execution_cwd = self.work_dir
        tool = self.registry.get(tc.tool_name)

        if tool is None:
            return ToolResult(
                output=f"Error: unknown tool '{tc.tool_name}'", is_error=True
            )

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
                work_dir=execution_cwd,
                file_path=file_path,
            )
            rejection = await self.hook_engine.run_pre_tool_hooks(hook_ctx)
            if rejection is not None:
                return ToolResult(
                    output=f"Hook rejected: {rejection.reason}",
                    is_error=True,
                )

        if self.permission_checker:
            decision = self._check_tool_permission(tool, tc.arguments, execution_cwd)
            assert decision is not None
            if decision.effect == "deny":
                return ToolResult(
                    output=f"Permission denied: {decision.reason}",
                    is_error=True,
                )
            if decision.effect == "ask":
                if self.permission_mode == PermissionMode.DONT_ASK:
                    pass  # 自动批准
                else:
                    return ToolResult(
                        output="Permission denied: non-interactive agent cannot prompt user",
                        is_error=True,
                    )

        try:
            result = await self._execute_tool_in_work_dir(
                tool, tc.arguments, execution_cwd
            )
        except ValidationError as e:
            result = ToolResult(
                output=f"Parameter validation error: {e}", is_error=True
            )
        except Exception as e:
            result = ToolResult(
                output=f"Tool execution error: {e}", is_error=True
            )

        if self.hook_engine:
            file_path = self._infer_file_path(tc.arguments)
            hook_ctx = self._build_hook_context(
                "post_tool_use",
                tool_name=tc.tool_name,
                tool_args=tc.arguments,
                work_dir=execution_cwd,
                file_path=file_path,
            )
            await self.hook_engine.run_hooks("post_tool_use", hook_ctx)

        return result

    def _maybe_persist_or_truncate(self, tool_use_id: str, text: str) -> str:
        from eviforge.context.manager import (
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

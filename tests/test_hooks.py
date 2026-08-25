
"""Hook 系统的测试 —— 涵盖事件、条件、执行器、引擎、加载器以及与 agent 的集成。"""
from __future__ import annotations

import asyncio
import os
import shlex
import subprocess
import sys
import time
from types import SimpleNamespace
from typing import Any, AsyncIterator
from unittest.mock import AsyncMock, patch

import pytest

from mewcode.hooks import (
    Action,
    ActionResult,
    ActionStatus,
    Condition,
    ConditionGroup,
    ConditionParseError,
    Hook,
    HookConfigError,
    HookContext,
    HookEngine,
    HookRuntimeProfile,
    LifecycleEvent,
    SUPPORTED_EVENTS_BY_RUNTIME,
    SUPPORTED_LIFECYCLE_EVENTS,
    ToolRejectedError,
    UNSUPPORTED_LIFECYCLE_EVENTS,
    load_hooks,
    parse_condition,
    supported_events_for,
)


def _python_shell_command(source: str) -> str:
    argv = [sys.executable, "-c", source]
    return subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)

# ---------------------------------------------------------------------------
# LifecycleEvent
# ---------------------------------------------------------------------------

class TestLifecycleEvent:
    def test_has_15_events(self):
        assert len(LifecycleEvent) == 15

    def test_string_comparison(self):
        assert LifecycleEvent.SESSION_START == "session_start"
        assert LifecycleEvent.PRE_TOOL_USE == "pre_tool_use"
        assert LifecycleEvent.SHUTDOWN == "shutdown"

    def test_all_values(self):
        expected = {
            "session_start", "session_end",
            "turn_start", "turn_end",
            "pre_tool_use", "post_tool_use",
            "pre_send", "post_receive",
            "startup", "shutdown", "error", "compact",
            "permission_request", "file_change", "command_execute",
        }
        assert {e.value for e in LifecycleEvent} == expected

    def test_declared_support_contract_is_complete_and_disjoint(self):
        assert SUPPORTED_LIFECYCLE_EVENTS | UNSUPPORTED_LIFECYCLE_EVENTS == set(
            LifecycleEvent
        )
        assert not SUPPORTED_LIFECYCLE_EVENTS & UNSUPPORTED_LIFECYCLE_EVENTS
        assert {event.value for event in UNSUPPORTED_LIFECYCLE_EVENTS} == {
            "error",
            "compact",
            "permission_request",
            "file_change",
            "command_execute",
        }

    @pytest.mark.parametrize(
        "runtime",
        list(HookRuntimeProfile),
    )
    def test_every_runtime_has_an_explicit_nonempty_event_contract(self, runtime):
        assert supported_events_for(runtime) == SUPPORTED_EVENTS_BY_RUNTIME[runtime]
        assert supported_events_for(runtime)
        assert supported_events_for(runtime) <= SUPPORTED_LIFECYCLE_EVENTS

# ---------------------------------------------------------------------------
# HookContext
# ---------------------------------------------------------------------------

class TestHookContext:

    def test_get_field_tool(self):
        ctx = HookContext(tool_name="Bash")
        assert ctx.get_field("tool") == "Bash"

    def test_get_field_event(self):
        ctx = HookContext(event_name="pre_tool_use")
        assert ctx.get_field("event") == "pre_tool_use"

    def test_get_field_args(self):
        ctx = HookContext(tool_args={"command": "ls -la", "path": "/tmp"})
        assert ctx.get_field("args.command") == "ls -la"
        assert ctx.get_field("args.path") == "/tmp"

    def test_get_field_unknown(self):
        ctx = HookContext()
        assert ctx.get_field("nonexistent") == ""
        assert ctx.get_field("args.missing") == ""

    def test_expand_all_variables(self):
        ctx = HookContext(
            event_name="post_tool_use",
            tool_name="WriteFile",
            tool_args={"file_path": "src/main.py"},
            file_path="src/main.py",
            message="done",
            error="",
        )
        template = "Event=$EVENT Tool=$TOOL_NAME File=$FILE_PATH Msg=$MESSAGE Err=$ERROR Arg=$TOOL_ARGS.file_path"
        result = ctx.expand(template)
        assert "Event=post_tool_use" in result
        assert "Tool=WriteFile" in result
        assert "File=src/main.py" in result
        assert "Msg=done" in result
        assert "Err=" in result
        assert "Arg=src/main.py" in result

    def test_expand_undefined_variable(self):
        ctx = HookContext()
        assert ctx.expand("hello $UNKNOWN world") == "hello $UNKNOWN world"
        assert ctx.expand("$FILE_PATH") == ""

# ---------------------------------------------------------------------------
# 条件解析
# ---------------------------------------------------------------------------

class TestParseCondition:
    def test_single_condition(self):
        group = parse_condition('tool == "Bash"')
        assert group is not None
        assert len(group.conditions) == 1
        assert group.conditions[0].field == "tool"
        assert group.conditions[0].operator == "=="
        assert group.conditions[0].value == "Bash"
        assert group.logic == "and"

    def test_and_combination(self):
        group = parse_condition('tool == "Bash" && args.command =~ /rm/')
        assert group is not None
        assert len(group.conditions) == 2
        assert group.logic == "and"

    def test_or_combination(self):
        group = parse_condition('tool == "Bash" || tool == "WriteFile"')
        assert group is not None
        assert len(group.conditions) == 2
        assert group.logic == "or"

    def test_mixed_operators_error(self):
        with pytest.raises(ConditionParseError, match="Cannot mix"):
            parse_condition('tool == "Bash" && args.x == "1" || args.y == "2"')

    def test_empty_condition(self):
        assert parse_condition("") is None
        assert parse_condition("   ") is None

    def test_regex_format(self):
        group = parse_condition('args.command =~ /rm\\s+-rf/')
        assert group is not None
        c = group.conditions[0]
        assert c.operator == "=~"
        assert c.value == "/rm\\s+-rf/"

    def test_no_valid_operator(self):
        with pytest.raises(ConditionParseError, match="No valid operator"):
            parse_condition("tool Bash")

# ---------------------------------------------------------------------------
# 条件求值
# ---------------------------------------------------------------------------

class TestConditionEvaluate:
    def test_eq(self):
        ctx = HookContext(tool_name="Bash")
        c = Condition(field="tool", operator="==", value="Bash")
        assert c.evaluate(ctx) is True
        c2 = Condition(field="tool", operator="==", value="WriteFile")
        assert c2.evaluate(ctx) is False

    def test_neq(self):
        ctx = HookContext(tool_name="Bash")
        c = Condition(field="tool", operator="!=", value="ReadFile")
        assert c.evaluate(ctx) is True
        c2 = Condition(field="tool", operator="!=", value="Bash")
        assert c2.evaluate(ctx) is False

    def test_regex(self):
        ctx = HookContext(tool_args={"command": "rm  -rf /"})
        c = Condition(field="args.command", operator="=~", value="/rm\\s+-rf/")
        assert c.evaluate(ctx) is True

    def test_glob(self):
        ctx = HookContext(tool_args={"path": "src/main.py"})
        c = Condition(field="args.path", operator="~=", value="*.py")
        assert c.evaluate(ctx) is True
        c2 = Condition(field="args.path", operator="~=", value="*.go")
        assert c2.evaluate(ctx) is False

class TestConditionGroupEvaluate:
    def test_and_all_pass(self):
        ctx = HookContext(tool_name="WriteFile", tool_args={"path": "src/app.py"})
        group = ConditionGroup(
            conditions=[
                Condition("tool", "==", "WriteFile"),
                Condition("args.path", "~=", "*.py"),
            ],
            logic="and",
        )
        assert group.evaluate(ctx) is True

    def test_and_partial_fail(self):
        ctx = HookContext(tool_name="WriteFile", tool_args={"path": "src/app.go"})
        group = ConditionGroup(
            conditions=[
                Condition("tool", "==", "WriteFile"),
                Condition("args.path", "~=", "*.py"),
            ],
            logic="and",
        )
        assert group.evaluate(ctx) is False

    def test_or_any_pass(self):
        ctx = HookContext(tool_name="Bash")
        group = ConditionGroup(
            conditions=[
                Condition("tool", "==", "Bash"),
                Condition("tool", "==", "WriteFile"),
            ],
            logic="or",
        )
        assert group.evaluate(ctx) is True

    def test_or_all_fail(self):
        ctx = HookContext(tool_name="ReadFile")
        group = ConditionGroup(
            conditions=[
                Condition("tool", "==", "Bash"),
                Condition("tool", "==", "WriteFile"),
            ],
            logic="or",
        )
        assert group.evaluate(ctx) is False

    def test_empty_group(self):
        ctx = HookContext()
        group = ConditionGroup(conditions=[], logic="and")
        assert group.evaluate(ctx) is True

# ---------------------------------------------------------------------------
# Executors
# ---------------------------------------------------------------------------

class TestCommandExecutor:
    @pytest.mark.asyncio
    async def test_normal_execution(self):
        from mewcode.hooks.executors import execute_command

        action = Action(type="command", command="echo hello")
        ctx = HookContext()
        result = await execute_command(action, ctx)
        assert result.success is True
        assert "hello" in result.output

    @pytest.mark.asyncio
    async def test_variable_substitution(self):
        from mewcode.hooks.executors import execute_command

        action = Action(type="command", command="echo $FILE_PATH")
        ctx = HookContext(file_path="src/main.py")
        result = await execute_command(action, ctx)
        assert "src/main.py" in result.output

    @pytest.mark.asyncio
    async def test_timeout(self):
        from mewcode.hooks.executors import execute_command

        action = Action(
            type="command",
            command=_python_shell_command("import time; time.sleep(10)"),
            timeout=1,
        )
        ctx = HookContext()
        started = time.monotonic()
        result = await execute_command(action, ctx)
        assert time.monotonic() - started < 3.0
        assert result.success is False
        assert "timed out" in result.output
        assert result.status is ActionStatus.TIMED_OUT
        assert result.error_code == "hook.command_timeout"

class TestPromptExecutor:
    @pytest.mark.asyncio
    async def test_returns_message(self):
        from mewcode.hooks.executors import execute_prompt

        action = Action(type="prompt", message="Hello $TOOL_NAME")
        ctx = HookContext(tool_name="WriteFile")
        result = await execute_prompt(action, ctx)
        assert result.success is True
        assert result.output == "Hello WriteFile"

class TestHttpExecutor:
    @pytest.mark.asyncio
    async def test_mock_request(self):
        from mewcode.hooks.executors import execute_http

        action = Action(
            type="http",
            url="https://httpbin.org/post",
            body='{"test": true}',
            timeout=7,
        )
        ctx = HookContext()
        client = AsyncMock()
        client.request.return_value = SimpleNamespace(
            status_code=200,
            content=b'{"ok": true}',
        )
        context_manager = AsyncMock()
        context_manager.__aenter__.return_value = client

        # 用 mock 避免发起真实网络请求，同时验证配置的 timeout 被传给传输层。
        with patch(
            "mewcode.hooks.executors.httpx.AsyncClient",
            return_value=context_manager,
        ) as client_factory:
            result = await execute_http(action, ctx)
            assert result.success is True
            assert "200" in result.output
            configured_timeout = client_factory.call_args.kwargs["timeout"]
            assert configured_timeout.connect == 7
            assert client_factory.call_args.kwargs["follow_redirects"] is False

    @pytest.mark.asyncio
    async def test_total_timeout_and_connection_cleanup(self):
        from mewcode.hooks.executors import execute_http

        started = asyncio.Event()
        closed = asyncio.Event()

        class HangingClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                closed.set()

            async def request(self, *_args, **_kwargs):
                started.set()
                await asyncio.Event().wait()

        action = Action(type="http", url="https://example.test/hang", timeout=0.05)
        before = time.monotonic()
        with patch(
            "mewcode.hooks.executors.httpx.AsyncClient",
            return_value=HangingClient(),
        ):
            result = await execute_http(action, HookContext())

        elapsed = time.monotonic() - before
        assert started.is_set()
        assert closed.is_set()
        assert 0.04 <= elapsed < 0.25
        assert result.success is False
        assert "timed out after 0.05s" in result.output
        assert result.status is ActionStatus.TIMED_OUT
        assert result.error_code == "hook.http_timeout"

    @pytest.mark.asyncio
    async def test_cancellation_closes_http_client(self):
        from mewcode.hooks.executors import execute_http

        started = asyncio.Event()
        closed = asyncio.Event()

        class HangingClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                closed.set()

            async def request(self, *_args, **_kwargs):
                started.set()
                await asyncio.Event().wait()

        action = Action(type="http", url="https://example.test/hang", timeout=30)
        with patch(
            "mewcode.hooks.executors.httpx.AsyncClient",
            return_value=HangingClient(),
        ):
            task = asyncio.create_task(execute_http(action, HookContext()))
            await asyncio.wait_for(started.wait(), timeout=1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert closed.is_set()

class TestAgentExecutor:
    @pytest.mark.asyncio
    async def test_unsupported_action_never_reports_success(self):
        from mewcode.hooks.executors import execute_agent

        action = Action(type="agent", prompt="Check $FILE_PATH")
        ctx = HookContext(file_path="test.py")
        result = await execute_agent(action, ctx)
        assert result.success is False
        assert result.output.startswith("unsupported_action:")
        assert result.status is ActionStatus.UNSUPPORTED
        assert result.error_code == "hook.unsupported_action"

class TestExecuteAction:
    @pytest.mark.asyncio
    async def test_dispatch(self):
        from mewcode.hooks.executors import execute_action

        action = Action(type="command", command="echo dispatch_test")
        ctx = HookContext()
        result = await execute_action(action, ctx)
        assert "dispatch_test" in result.output
        assert result.status is ActionStatus.SUCCEEDED
        assert result.elapsed_ms >= 0

    @pytest.mark.asyncio
    async def test_unknown_type(self):
        from mewcode.hooks.executors import execute_action

        action = Action(type="unknown")
        ctx = HookContext()
        result = await execute_action(action, ctx)
        assert result.success is False
        assert result.status is ActionStatus.UNSUPPORTED
        assert result.error_code == "hook.unknown_action"

    @pytest.mark.asyncio
    async def test_output_is_bounded_and_marked_as_truncated(self):
        from mewcode.hooks.executors import MAX_HOOK_OUTPUT_CHARS, execute_action

        action = Action(type="prompt", message="x" * (MAX_HOOK_OUTPUT_CHARS + 23))
        result = await execute_action(action, HookContext())

        assert result.success is True
        assert result.truncated is True
        assert result.output.startswith("x" * MAX_HOOK_OUTPUT_CHARS)
        assert "23 Hook output characters truncated" in result.output

# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

class TestLoadHooks:
    def test_full_config(self):
        raw = [
            {
                "id": "auto-format",
                "event": "post_tool_use",
                "if": 'tool == "WriteFile"',
                "action": {"type": "command", "command": "echo formatted"},
            }
        ]
        hooks = load_hooks(raw)
        assert len(hooks) == 1
        assert hooks[0].id == "auto-format"
        assert hooks[0].event == "post_tool_use"
        assert hooks[0].condition is not None

    def test_auto_id(self):
        raw = [
            {"event": "session_start", "action": {"type": "prompt", "message": "hello"}}
        ]
        hooks = load_hooks(raw)
        assert hooks[0].id == "session_start_0"

    def test_empty(self):
        assert load_hooks(None) == []
        assert load_hooks([]) == []

    def test_invalid_event(self):
        with pytest.raises(HookConfigError, match="invalid event"):
            load_hooks([{"event": "bad_event", "action": {"type": "command", "command": "x"}}])

    @pytest.mark.parametrize(
        "event",
        ["error", "compact", "permission_request", "file_change", "command_execute"],
    )
    def test_known_event_without_dispatcher_is_rejected(self, event):
        with pytest.raises(
            HookConfigError,
            match=rf"unsupported event '{event}'.*no dispatcher",
        ):
            load_hooks(
                [{"event": event, "action": {"type": "command", "command": "x"}}]
            )

    @pytest.mark.parametrize("runtime", ["headless", "dag"])
    @pytest.mark.parametrize(
        "event",
        [
            "startup",
            "shutdown",
            "session_start",
            "session_end",
            "pre_send",
            "post_receive",
        ],
    )
    def test_runtime_profile_rejects_events_it_does_not_dispatch(
        self, runtime, event
    ):
        with pytest.raises(
            HookConfigError,
            match=rf"unsupported event '{event}' for '{runtime}' runtime",
        ):
            load_hooks(
                [{"event": event, "action": {"type": "command", "command": "x"}}],
                runtime=runtime,
            )

    @pytest.mark.parametrize("runtime", list(HookRuntimeProfile))
    def test_runtime_profile_accepts_each_declared_event(self, runtime):
        raw = [
            {
                "id": f"{runtime.value}-{event.value}",
                "event": event.value,
                "action": {"type": "command", "command": "echo ok"},
            }
            for event in supported_events_for(runtime)
        ]
        assert {hook.event for hook in load_hooks(raw, runtime=runtime)} == {
            event.value for event in supported_events_for(runtime)
        }

    def test_invalid_runtime_profile_is_diagnostic(self):
        with pytest.raises(HookConfigError, match="invalid Hook runtime profile 'worker'"):
            load_hooks(
                [{"event": "turn_start", "action": {"type": "command", "command": "x"}}],
                runtime="worker",
            )

    def test_invalid_action_type(self):
        with pytest.raises(HookConfigError, match="invalid action type"):
            load_hooks([{"event": "startup", "action": {"type": "bad"}}])

    def test_unimplemented_agent_action_is_rejected_during_config_load(self):
        with pytest.raises(
            HookConfigError,
            match="unsupported action type 'agent'.*no Agent executor",
        ):
            load_hooks(
                [{"event": "startup", "action": {"type": "agent", "prompt": "x"}}]
            )

    def test_reject_on_non_pre_tool_use(self):
        with pytest.raises(HookConfigError, match="reject.*pre_tool_use"):
            load_hooks([{
                "event": "post_tool_use",
                "action": {"type": "command", "command": "x"},
                "reject": True,
            }])

    def test_async_on_pre_tool_use(self):
        with pytest.raises(HookConfigError, match="async.*pre_tool_use"):
            load_hooks([{
                "event": "pre_tool_use",
                "action": {"type": "command", "command": "x"},
                "async": True,
            }])

    def test_missing_required_field(self):
        with pytest.raises(HookConfigError, match="requires.*command"):
            load_hooks([{"event": "startup", "action": {"type": "command"}}])

        with pytest.raises(HookConfigError, match="requires.*url"):
            load_hooks([{"event": "startup", "action": {"type": "http"}}])

        with pytest.raises(HookConfigError, match="requires.*message"):
            load_hooks([{"event": "startup", "action": {"type": "prompt"}}])

# ---------------------------------------------------------------------------
# HookEngine
# ---------------------------------------------------------------------------

class TestHookEngine:

    def _make_hook(self, **kwargs) -> Hook:
        defaults = {
            "id": "test",
            "event": "post_tool_use",
            "action": Action(type="command", command="echo test"),
        }
        defaults.update(kwargs)
        return Hook(**defaults)

    def test_find_matching_hooks(self):
        h1 = self._make_hook(id="h1", event="post_tool_use")
        h2 = self._make_hook(id="h2", event="pre_tool_use")
        engine = HookEngine([h1, h2])
        ctx = HookContext(event_name="post_tool_use")
        matched = engine.find_matching_hooks("post_tool_use", ctx)
        assert len(matched) == 1
        assert matched[0].id == "h1"

    def test_find_with_condition_filter(self):
        h = self._make_hook(
            id="h1",
            event="post_tool_use",
            condition=ConditionGroup(
                conditions=[Condition("tool", "==", "WriteFile")],
                logic="and",
            ),
        )
        engine = HookEngine([h])

        ctx_match = HookContext(event_name="post_tool_use", tool_name="WriteFile")
        assert len(engine.find_matching_hooks("post_tool_use", ctx_match)) == 1

        ctx_no_match = HookContext(event_name="post_tool_use", tool_name="Bash")
        assert len(engine.find_matching_hooks("post_tool_use", ctx_no_match)) == 0

    def test_once_filter(self):
        h = self._make_hook(id="h1", once=True)
        engine = HookEngine([h])
        ctx = HookContext(event_name="post_tool_use")

        assert len(engine.find_matching_hooks("post_tool_use", ctx)) == 1
        h.mark_executed()
        assert len(engine.find_matching_hooks("post_tool_use", ctx)) == 0

    @pytest.mark.asyncio
    async def test_run_pre_tool_hooks_reject(self):
        h = self._make_hook(
            id="block-vendor",
            event="pre_tool_use",
            action=Action(type="command", command="echo rejected"),
            reject=True,
        )
        engine = HookEngine([h])
        ctx = HookContext(event_name="pre_tool_use", tool_name="WriteFile")
        result = await engine.run_pre_tool_hooks(ctx)
        assert result is not None
        assert isinstance(result, ToolRejectedError)
        assert "rejected" in result.reason

    @pytest.mark.asyncio
    async def test_run_pre_tool_hooks_no_reject(self):
        h = self._make_hook(
            id="log-only",
            event="pre_tool_use",
            action=Action(type="command", command="echo ok"),
            reject=False,
        )
        engine = HookEngine([h])
        ctx = HookContext(event_name="pre_tool_use", tool_name="WriteFile")
        result = await engine.run_pre_tool_hooks(ctx)
        assert result is None

    @pytest.mark.asyncio
    async def test_prompt_message_collection(self):
        h = self._make_hook(
            id="inject",
            event="session_start",
            action=Action(type="prompt", message="Project info here"),
        )
        engine = HookEngine([h])
        ctx = HookContext(event_name="session_start")
        await engine.run_hooks("session_start", ctx)
        messages = engine.get_prompt_messages()
        assert len(messages) == 1
        assert "Project info" in messages[0]
        assert engine.get_prompt_messages() == []

    @pytest.mark.asyncio
    async def test_error_does_not_raise(self):
        h = self._make_hook(
            id="bad",
            event="post_tool_use",
            action=Action(type="command", command="exit 1"),
        )
        engine = HookEngine([h])
        ctx = HookContext(event_name="post_tool_use")
        await engine.run_hooks("post_tool_use", ctx)

    @pytest.mark.asyncio
    async def test_async_hook_does_not_block(self):
        h = self._make_hook(
            id="slow",
            event="post_tool_use",
            action=Action(
                type="command",
                command=_python_shell_command("import time; time.sleep(5)"),
            ),
            async_exec=True,
        )
        engine = HookEngine([h])
        ctx = HookContext(event_name="post_tool_use")
        started = time.monotonic()
        await engine.run_hooks("post_tool_use", ctx)
        assert time.monotonic() - started < 1.0
        await asyncio.sleep(0)
        await engine.shutdown(timeout=0.0)
        assert not engine._background_tasks

    @pytest.mark.asyncio
    async def test_shutdown_cancels_async_http_and_closes_transport(self):
        started = asyncio.Event()
        closed = asyncio.Event()

        class HangingClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                closed.set()

            async def request(self, *_args, **_kwargs):
                started.set()
                await asyncio.Event().wait()

        hook = self._make_hook(
            id="async-http",
            action=Action(type="http", url="https://example.test/hang", timeout=30),
            async_exec=True,
        )
        engine = HookEngine([hook])
        with patch(
            "mewcode.hooks.executors.httpx.AsyncClient",
            return_value=HangingClient(),
        ):
            await engine.run_hooks(
                "post_tool_use",
                HookContext(event_name="post_tool_use"),
            )
            await asyncio.wait_for(started.wait(), timeout=1)
            await asyncio.wait_for(engine.shutdown(timeout=0.0), timeout=1)

        assert closed.is_set()
        assert not engine._background_tasks

    @pytest.mark.asyncio
    async def test_reviewed_plan_blocks_command_hook_without_running_it(self):
        from mewcode.execution import ExecutionContext

        hook = self._make_hook(
            id="planned-command",
            event="session_start",
            action=Action(type="command", command="echo must-not-run"),
        )
        engine = HookEngine([hook])
        context = ExecutionContext(
            task_id="task",
            cwd=".",
            plan_hash="reviewed-plan",
            workspace_root=".",
            commands=(("echo", "must-not-run"),),
        )
        engine.bind_execution_policy(context_provider=lambda: context)

        with patch("mewcode.hooks.engine.execute_action") as execute:
            await engine.run_hooks("session_start", HookContext(event_name="session_start"))

        execute.assert_not_called()
        notification = engine.drain_notifications()[0]
        assert notification.success is False
        assert "blocked by reviewed Plan" in notification.output

    @pytest.mark.asyncio
    async def test_reviewed_plan_blocks_http_hook_and_pre_tool_rejects_closed(self):
        from mewcode.execution import ExecutionContext

        hook = self._make_hook(
            id="planned-http",
            event="pre_tool_use",
            action=Action(type="http", url="https://example.com/audit"),
            reject=True,
        )
        engine = HookEngine([hook])
        context = ExecutionContext(
            task_id="task",
            cwd=".",
            plan_hash="reviewed-plan",
            workspace_root=".",
            network_hosts=("example.com",),
        )
        engine.bind_execution_policy(context_provider=lambda: context)

        with patch("mewcode.hooks.engine.execute_action") as execute:
            rejection = await engine.run_pre_tool_hooks(
                HookContext(event_name="pre_tool_use", tool_name="ReadFile")
            )

        execute.assert_not_called()
        assert rejection is not None
        assert "blocked by reviewed Plan" in rejection.reason

    @pytest.mark.asyncio
    async def test_execution_context_provider_is_dynamic_and_unplanned_is_compatible(self):
        from mewcode.execution import ExecutionContext

        hook = self._make_hook(
            id="dynamic-context",
            event="session_start",
            action=Action(type="command", command="echo ok"),
        )
        engine = HookEngine([hook])
        state = {
            "context": ExecutionContext.unplanned(task_id="task", cwd="."),
        }
        engine.bind_execution_policy(context_provider=lambda: state["context"])

        with patch(
            "mewcode.hooks.engine.execute_action",
            new=AsyncMock(return_value=ActionResult(output="ok", success=True)),
        ) as execute:
            await engine.run_hooks("session_start", HookContext(event_name="session_start"))
            assert execute.await_count == 1
            state["context"] = ExecutionContext(
                task_id="task",
                cwd=".",
                plan_hash="second-reviewed-plan",
                workspace_root=".",
                commands=(),
            )
            hook.executed = False
            await engine.run_hooks("session_start", HookContext(event_name="session_start"))
            assert execute.await_count == 1

# ---------------------------------------------------------------------------
# Agent 循环集成
# ---------------------------------------------------------------------------

class TestAgentHookIntegration:
    """验证 pre_tool_use 拒绝会导致工具调用被跳过。"""

    @pytest.mark.asyncio
    async def test_pre_tool_use_reject_skips_tool(self):
        from mewcode.agent import Agent, ToolResultEvent
        from mewcode.client import LLMClient
        from mewcode.conversation import ConversationManager
        from mewcode.tools import create_default_registry
        from mewcode.tools.base import StreamEnd, StreamEvent, TextDelta, ToolCallComplete

        class MockClient(LLMClient):
            def __init__(self):
                self._call = 0

            async def stream(self, conversation, system="", tools=None):
                self._call += 1
                if self._call == 1:
                    yield ToolCallComplete(
                        tool_id="t1",
                        tool_name="Bash",
                        arguments={"command": "rm -rf /"},
                    )
                    yield StreamEnd(stop_reason="tool_use", input_tokens=10, output_tokens=5)
                else:
                    yield TextDelta(text="I understand, I won't do that.")
                    yield StreamEnd(stop_reason="end_turn", input_tokens=10, output_tokens=5)

        hook = Hook(
            id="block-rm",
            event="pre_tool_use",
            action=Action(type="command", command="echo dangerous command blocked"),
            condition=parse_condition('tool == "Bash" && args.command =~ /rm\\s+-rf/'),
            reject=True,
        )
        engine = HookEngine([hook])

        client = MockClient()
        registry = create_default_registry()
        conv = ConversationManager()
        conv.add_user_message("delete everything")

        agent = Agent(
            client=client,
            registry=registry,
            protocol="anthropic",
            hook_engine=engine,
        )

        events = []
        async for event in agent.run(conv):
            events.append(event)

        tool_results = [e for e in events if isinstance(e, ToolResultEvent)]
        assert len(tool_results) >= 1
        rejected = tool_results[0]
        assert rejected.is_error is True
        assert "Hook rejected" in rejected.output

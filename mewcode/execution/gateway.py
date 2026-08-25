from __future__ import annotations

import inspect
import os
import secrets
import threading
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, cast

from pydantic import ValidationError as PydanticValidationError

from mewcode.execution.descriptor import ToolDescriptor
from mewcode.execution.context import (
    ExecutionAssessment,
    ExecutionContext,
    InvocationGrant,
    normalized_arguments_hash,
)
from mewcode.execution.invocation import (
    ExecutionResult,
    ExecutionStatus,
    ExecutionTraceEvent,
    ToolInvocation,
    TraceStage,
)
from mewcode.execution.risk import ReasonCode, RiskDecision, RiskEngine
from mewcode.permissions.checker import PermissionChecker
from mewcode.tools.base import Tool


class TraceHook(Protocol):
    def __call__(self, event: ExecutionTraceEvent) -> None | Awaitable[None]: ...


ApprovalResolver = Callable[[ToolInvocation, RiskDecision, str], bool | Awaitable[bool]]


class ActionJournal(Protocol):
    def begin(
        self,
        assessment: ExecutionAssessment,
        context: ExecutionContext,
        *,
        authorization_source: str,
    ) -> object | None: ...

    def succeed(self, handle: object | None) -> None: ...

    def fail(self, handle: object | None, *, error: str) -> None: ...

    def interrupt(self, handle: object | None, *, error: str) -> None: ...


class ExecutionGateway:
    """Single, typed entry point for legacy ``Tool`` execution.

    It validates arguments, applies deterministic risk and manifest rules,
    consumes exact grants, journals side effects, and emits one trace shape.
    """

    def __init__(
        self,
        *,
        permission_checker: PermissionChecker | None = None,
        risk_engine: RiskEngine | None = None,
        trace_hook: TraceHook | object | None = None,
        approval_resolver: ApprovalResolver | None = None,
        reject_unexpected_arguments: bool = True,
        execution_context: ExecutionContext | None = None,
        action_journal: ActionJournal | None = None,
        grant_secret: bytes | None = None,
    ) -> None:
        self.permission_checker = permission_checker
        self.risk_engine = risk_engine or RiskEngine()
        self.trace_hook = trace_hook
        self.approval_resolver = approval_resolver
        self.reject_unexpected_arguments = reject_unexpected_arguments
        self.execution_context = execution_context
        self.action_journal = action_journal
        self._grant_secret = grant_secret or secrets.token_bytes(32)
        self._grant_lock = threading.Lock()
        self._consumed_grants: set[str] = set()

    def preview(
        self,
        tool: Tool,
        arguments: dict[str, Any],
        *,
        invocation_id: str | None = None,
        actor: str = "agent",
        context: ExecutionContext | None = None,
    ) -> ExecutionAssessment:
        """Validate schema and classify risk without checking permission/executing."""

        invocation = ToolInvocation(
            tool_name=tool.name,
            arguments=arguments,
            actor=actor,
            **({"invocation_id": invocation_id} if invocation_id is not None else {}),
        )
        return self.assess(tool, invocation, context=context)

    def assess(
        self,
        tool: Tool,
        invocation: ToolInvocation,
        *,
        context: ExecutionContext | None = None,
    ) -> ExecutionAssessment:
        """Return the exact normalized object later bound to a grant/journal."""

        if invocation.tool_name != tool.name:
            code = ReasonCode.VALIDATION_TOOL_MISMATCH.value
            return ExecutionAssessment(
                invocation.invocation_id,
                invocation.tool_name,
                None,
                None,
                arguments_hash=invocation.arguments_hash,
                valid=False,
                error=f"Invocation targets {invocation.tool_name!r}, but tool is {tool.name!r}",
                reason_codes=(code,),
            )
        descriptor = ToolDescriptor.from_tool(tool)
        supplied = dict(invocation.arguments)
        unexpected = self._unexpected_arguments(descriptor, supplied)
        if unexpected:
            code = ReasonCode.VALIDATION_UNEXPECTED_ARGUMENT.value
            return ExecutionAssessment(
                invocation.invocation_id,
                invocation.tool_name,
                descriptor,
                None,
                arguments_hash=invocation.arguments_hash,
                valid=False,
                error=f"Unexpected argument(s): {', '.join(unexpected)}",
                reason_codes=(code,),
            )
        try:
            params = descriptor.params_model.model_validate(supplied)
        except PydanticValidationError as exc:
            code = ReasonCode.VALIDATION_SCHEMA_INVALID.value
            return ExecutionAssessment(
                invocation.invocation_id,
                invocation.tool_name,
                descriptor,
                None,
                arguments_hash=invocation.arguments_hash,
                valid=False,
                error=_format_validation_error(exc),
                reason_codes=(code,),
            )
        # JSON mode removes language-specific objects such as ``Path`` before
        # hashing/binding.  The tool still receives the validated BaseModel.
        normalized = params.model_dump(mode="json")
        arguments_hash = normalized_arguments_hash(normalized)
        risk = self.risk_engine.assess(descriptor, normalized)
        codes = list(risk.reason_codes)
        active_context = context or self.execution_context
        if active_context is not None:
            violation = active_context.constraint_error(descriptor, normalized)
            if violation is not None:
                code, message = violation
                codes.append(code)
                return ExecutionAssessment(
                    invocation.invocation_id,
                    invocation.tool_name,
                    descriptor,
                    risk,
                    normalized,
                    arguments_hash,
                    False,
                    message,
                    tuple(dict.fromkeys(codes)),
                    params,
                )
        return ExecutionAssessment(
            invocation.invocation_id,
            invocation.tool_name,
            descriptor,
            risk,
            normalized,
            arguments_hash,
            True,
            "",
            tuple(codes),
            params,
        )

    def issue_grant(
        self,
        assessment: ExecutionAssessment,
        *,
        approver: str,
        context: ExecutionContext | None = None,
    ) -> InvocationGrant:
        """Issue a host-only, exact invocation capability after UI approval."""

        if not assessment.valid or assessment.risk is None:
            raise ValueError("cannot grant an invalid execution assessment")
        if assessment.risk.hard_deny:
            raise ValueError("L4 execution cannot be granted")
        active_context = context or self.execution_context
        if active_context is None:
            active_context = ExecutionContext.unplanned(
                task_id=assessment.invocation_id,
                cwd=os.getcwd(),
            )
        return InvocationGrant.issue(
            self._grant_secret,
            invocation_id=assessment.invocation_id,
            tool_name=assessment.tool_name,
            arguments_hash=assessment.arguments_hash,
            context=active_context,
            approver=approver,
        )

    async def execute(
        self,
        tool: Tool,
        invocation: ToolInvocation,
        *,
        grant: InvocationGrant | None = None,
        context: ExecutionContext | None = None,
    ) -> ExecutionResult:
        started = time.perf_counter()
        base_codes: list[str] = []
        risk: RiskDecision | None = None

        assessment = self.assess(tool, invocation, context=context)
        trace_invocation = invocation
        if assessment._params is not None:
            trace_invocation = ToolInvocation(
                invocation_id=invocation.invocation_id,
                tool_name=invocation.tool_name,
                arguments=assessment.normalized_arguments,
                actor=invocation.actor,
                metadata=invocation.metadata,
            )
        await self._emit(
            trace_invocation,
            TraceStage.RECEIVED,
            argument_keys=tuple(sorted(str(key) for key in trace_invocation.arguments)),
        )

        if not assessment.valid and any(
            code.startswith("validation.") for code in assessment.reason_codes
        ):
            base_codes.extend(assessment.reason_codes)
            return await self._finish(
                invocation,
                started,
                ExecutionStatus.VALIDATION_ERROR,
                assessment.error,
                base_codes,
                executed=False,
                stage=TraceStage.VALIDATION_FAILED,
            )

        assert assessment.descriptor is not None
        assert assessment.risk is not None
        assert assessment._params is not None
        descriptor = assessment.descriptor
        params = assessment._params
        normalized_arguments = dict(assessment.normalized_arguments)
        # All stages after schema validation use the same canonical argument
        # object as the grant and durable journal.  This also includes model
        # defaults, unlike the untrusted/raw RECEIVED event.
        invocation = trace_invocation
        await self._emit(invocation, TraceStage.VALIDATED)

        risk = assessment.risk
        base_codes.extend(risk.reason_codes)
        await self._emit(
            invocation,
            TraceStage.RISK_ASSESSED,
            risk=risk,
            reason_codes=tuple(base_codes),
        )

        if risk.hard_deny:
            return await self._finish(
                invocation,
                started,
                ExecutionStatus.PERMISSION_DENIED,
                "Execution denied by L4 risk policy",
                base_codes,
                executed=False,
                risk=risk,
            )

        if not assessment.valid:
            base_codes.extend(assessment.reason_codes)
            return await self._finish(
                invocation,
                started,
                ExecutionStatus.PERMISSION_DENIED,
                assessment.error,
                base_codes,
                executed=False,
                risk=risk,
            )

        permission_effect = "allow"
        permission_reason = "No legacy permission checker configured"
        authorization_source = "risk_policy"
        active_context = context or self.execution_context
        if active_context is None:
            active_context = ExecutionContext.unplanned(
                task_id=invocation.invocation_id,
                cwd=os.getcwd(),
            )
        if grant is not None:
            if not grant.verify(
                self._grant_secret,
                invocation_id=invocation.invocation_id,
                tool_name=invocation.tool_name,
                arguments_hash=assessment.arguments_hash,
                context=active_context,
            ):
                return await self._finish(
                    invocation,
                    started,
                    ExecutionStatus.PERMISSION_DENIED,
                    "Invocation grant does not match tool, arguments, cwd, or plan",
                    [*base_codes, ReasonCode.INVOCATION_GRANT_INVALID.value],
                    executed=False,
                    risk=risk,
                )
            with self._grant_lock:
                if grant._signature in self._consumed_grants:
                    return await self._finish(
                        invocation,
                        started,
                        ExecutionStatus.PERMISSION_DENIED,
                        "Invocation grant has already been consumed",
                        [*base_codes, ReasonCode.INVOCATION_GRANT_CONSUMED.value],
                        executed=False,
                        risk=risk,
                    )
                self._consumed_grants.add(grant._signature)
            permission_reason = "Invocation-scoped host approval"
            authorization_source = f"invocation_grant:{grant.approver}"
        elif self.permission_checker is not None:
            try:
                decision = self.permission_checker.check(tool, normalized_arguments)
            except Exception as exc:
                return await self._finish(
                    invocation,
                    started,
                    ExecutionStatus.INTERNAL_ERROR,
                    f"Permission checker failed: {exc}",
                    [*base_codes, ReasonCode.PERMISSION_DENIED.value],
                    executed=False,
                    risk=risk,
                    exception_type=type(exc).__name__,
                )
            permission_effect = decision.effect
            permission_reason = decision.reason
            authorization_source = f"permission_policy:{decision.reason}"
        elif risk.level.value >= 1:
            # Fail closed for side effects until a caller supplies either the
            # existing PermissionChecker or an approval resolver.
            permission_effect = "ask"
            permission_reason = "Side effects require an explicit permission policy"

        await self._emit(
            invocation,
            TraceStage.PERMISSION_DECIDED,
            risk=risk,
            reason_codes=tuple([*base_codes, f"permission.{permission_effect}"]),
        )

        if permission_effect == "deny":
            return await self._finish(
                invocation,
                started,
                ExecutionStatus.PERMISSION_DENIED,
                permission_reason,
                [*base_codes, ReasonCode.PERMISSION_DENIED.value],
                executed=False,
                risk=risk,
            )

        if permission_effect == "ask":
            approved = False
            if self.approval_resolver is not None:
                resolved = self.approval_resolver(invocation, risk, permission_reason)
                approved = bool(await resolved) if inspect.isawaitable(resolved) else bool(resolved)
                if approved:
                    authorization_source = "approval_resolver"
            if not approved:
                return await self._finish(
                    invocation,
                    started,
                    ExecutionStatus.APPROVAL_REQUIRED,
                    permission_reason,
                    [*base_codes, ReasonCode.PERMISSION_APPROVAL_REQUIRED.value],
                    executed=False,
                    risk=risk,
                )

        base_codes.append(ReasonCode.PERMISSION_ALLOWED.value)
        journal_handle: object | None = None
        if self.action_journal is not None:
            try:
                journal_handle = self.action_journal.begin(
                    assessment,
                    active_context,
                    authorization_source=authorization_source,
                )
            except Exception as exc:
                return await self._finish(
                    invocation,
                    started,
                    ExecutionStatus.INTERNAL_ERROR,
                    f"Action journal failed before execution: {exc}",
                    [*base_codes, "journal.prepare_failed"],
                    executed=False,
                    risk=risk,
                    exception_type=type(exc).__name__,
                )
        await self._emit(
            invocation,
            TraceStage.STARTED,
            risk=risk,
            reason_codes=tuple(base_codes),
        )

        try:
            approved_argv = active_context.approved_command_argv(
                descriptor, normalized_arguments
            )
            exact_executor = getattr(tool, "execute_argv", None)
            if approved_argv is not None:
                if not callable(exact_executor):
                    raise RuntimeError(
                        "planned command tool declares exact argv support without an executor"
                    )
                tool_result = await exact_executor(
                    params,
                    approved_argv,
                    cwd=active_context.cwd,
                )
            else:
                tool_result = await tool.execute(params)
        except BaseException as exc:
            if self.action_journal is not None:
                self.action_journal.interrupt(
                    journal_handle,
                    error=f"{type(exc).__name__}: {exc}",
                )
            if not isinstance(exc, Exception):
                raise
            return await self._finish(
                invocation,
                started,
                ExecutionStatus.INTERNAL_ERROR,
                f"Tool raised {type(exc).__name__}: {exc}",
                [*base_codes, ReasonCode.TOOL_RAISED_EXCEPTION.value],
                executed=True,
                risk=risk,
                exception_type=type(exc).__name__,
            )

        status = ExecutionStatus.TOOL_ERROR if tool_result.is_error else ExecutionStatus.SUCCEEDED
        terminal_code = (
            ReasonCode.TOOL_REPORTED_ERROR.value
            if tool_result.is_error
            else ReasonCode.TOOL_SUCCEEDED.value
        )
        if self.action_journal is not None:
            if tool_result.is_error:
                self.action_journal.fail(journal_handle, error=tool_result.output)
            else:
                self.action_journal.succeed(journal_handle)
        return await self._finish(
            invocation,
            started,
            status,
            tool_result.output,
            [*base_codes, terminal_code],
            executed=True,
            risk=risk,
            is_error=tool_result.is_error,
        )

    async def invoke(
        self,
        tool: Tool,
        arguments: dict[str, Any],
        *,
        invocation_id: str | None = None,
        actor: str = "agent",
        grant: InvocationGrant | None = None,
        context: ExecutionContext | None = None,
    ) -> ExecutionResult:
        invocation = ToolInvocation(
            tool_name=tool.name,
            arguments=arguments,
            actor=actor,
            **({"invocation_id": invocation_id} if invocation_id is not None else {}),
        )
        return await self.execute(tool, invocation, grant=grant, context=context)

    def _unexpected_arguments(
        self, descriptor: ToolDescriptor, arguments: dict[str, Any]
    ) -> list[str]:
        if not self.reject_unexpected_arguments:
            return []
        extra_policy = descriptor.params_model.model_config.get("extra")
        if extra_policy == "allow":
            return []
        expected = set(descriptor.params_model.model_fields)
        return sorted(str(key) for key in arguments if key not in expected)

    async def _finish(
        self,
        invocation: ToolInvocation,
        started: float,
        status: ExecutionStatus,
        output: str,
        reason_codes: list[str],
        *,
        executed: bool,
        stage: TraceStage = TraceStage.COMPLETED,
        risk: RiskDecision | None = None,
        is_error: bool | None = None,
        exception_type: str | None = None,
    ) -> ExecutionResult:
        unique_codes = tuple(dict.fromkeys(reason_codes))
        result = ExecutionResult(
            invocation_id=invocation.invocation_id,
            tool_name=invocation.tool_name,
            status=status,
            output=output,
            is_error=is_error if is_error is not None else status is not ExecutionStatus.SUCCEEDED,
            executed=executed,
            risk_level=str(risk.level) if risk is not None else None,
            reason_codes=unique_codes,
            duration_ms=(time.perf_counter() - started) * 1000,
            arguments_hash=invocation.arguments_hash,
            exception_type=exception_type,
        )
        await self._emit(
            invocation,
            stage,
            risk=risk,
            reason_codes=unique_codes,
            status=status,
        )
        return result

    async def _emit(
        self,
        invocation: ToolInvocation,
        stage: TraceStage,
        *,
        risk: RiskDecision | None = None,
        reason_codes: tuple[str, ...] = (),
        status: ExecutionStatus | None = None,
        argument_keys: tuple[str, ...] = (),
    ) -> None:
        if self.trace_hook is None:
            return
        event = ExecutionTraceEvent(
            invocation_id=invocation.invocation_id,
            tool_name=invocation.tool_name,
            stage=stage,
            risk_level=str(risk.level) if risk is not None else None,
            reason_codes=reason_codes,
            status=status,
            argument_keys=argument_keys,
            arguments_hash=invocation.arguments_hash,
        )
        hook = self.trace_hook
        callback = getattr(hook, "emit", None)
        if callback is None:
            callback = cast(TraceHook, hook)
        emitted = callback(event)
        if inspect.isawaitable(emitted):
            await emitted


def _format_validation_error(error: PydanticValidationError) -> str:
    messages: list[str] = []
    for item in error.errors(include_url=False, include_context=False, include_input=False):
        location = ".".join(str(part) for part in item.get("loc", ())) or "arguments"
        messages.append(f"{location}: {item['msg']}")
    return "Invalid tool arguments: " + "; ".join(messages)

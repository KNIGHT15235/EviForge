from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json

import pytest

from mewcode.orchestration import (
    AgentRole,
    ArtifactContract,
    ChangeEnvelope,
    LegacyAdapterError,
    LegacyAgentDAGAdapter,
    LegacyAgentFactoryAdapter,
    NodeStatus,
    RunStatus,
    ScheduleBudget,
    TaskEnvelope,
    TaskGraph,
    TaskNode,
)
from mewcode.tools import ToolRegistry


@dataclass
class FakeLegacyAgent:
    name: str
    calls: list[tuple[str, object, object]] = field(default_factory=list)

    async def run_to_completion(self, task, conversation=None, event_callback=None):
        self.calls.append((task, conversation, event_callback))
        if event_callback:
            event_callback(
                {
                    "type": "usage",
                    "usage": {"inputTokens": 3, "outputTokens": 2},
                }
            )
        protocol = task.partition("Required output protocol (authoritative):\n")[2]
        required = {
            name for name in ("candidate", "receipt", "inventory") if name in protocol
        }
        if required:
            return json.dumps(
                {
                    "artifacts": [
                        {"name": name, "content": f"result from {self.name}"}
                        for name in sorted(required)
                    ]
                }
            )
        return f"result from {self.name}"


@pytest.mark.asyncio
async def test_production_adapter_invokes_fresh_role_agents_end_to_end() -> None:
    created: list[tuple[AgentRole, TaskEnvelope, FakeLegacyAgent]] = []

    def factory(role: AgentRole, envelope: TaskEnvelope):
        agent = FakeLegacyAgent(f"{role.value}-{len(created)}")
        created.append((role, envelope, agent))
        return agent

    async def collect_change(envelope: TaskEnvelope, agent: FakeLegacyAgent):
        if envelope.node.role is AgentRole.IMPLEMENTER:
            return ChangeEnvelope.from_lease(
                envelope.lease,
                write_set=("src/service.py",),
                patch_ref="cas://candidate",
            )
        return None

    graph = TaskGraph(
        nodes=(
            TaskNode(
                node_id="implement",
                role=AgentRole.IMPLEMENTER,
                objective="Implement the change",
                token_budget=20,
                predicted_write_set=("src/**",),
                artifact_contract=ArtifactContract(required_outputs=("candidate",)),
            ),
            TaskNode(
                node_id="verify",
                role=AgentRole.VERIFIER,
                objective="Verify independently",
                depends_on=("implement",),
                token_budget=20,
                artifact_contract=ArtifactContract(
                    required_inputs=("candidate",),
                    required_outputs=("receipt",),
                ),
            ),
        )
    )
    adapter = LegacyAgentDAGAdapter(factory, change_collector=collect_change)
    report = await adapter.run(
        graph,
        budget=ScheduleBudget(total_tokens=40, wall_time_seconds=10),
        max_concurrency=2,
        run_id="production-adapter-test",
    )

    assert report.status is RunStatus.SUCCEEDED
    assert [role for role, _, _ in created] == [
        AgentRole.IMPLEMENTER,
        AgentRole.VERIFIER,
    ]
    assert created[0][2] is not created[1][2]
    verifier_envelope = created[1][1]
    assert verifier_envelope.capability.read_only
    assert verifier_envelope.capability.allowed_write_set == ()
    assert verifier_envelope.dependency_artifacts[0].name == "candidate"
    verifier_prompt, conversation, callback = created[1][2].calls[0]
    assert conversation is None
    assert "no inherited conversation" in verifier_prompt
    assert "lease_generation" in verifier_prompt
    assert callback is not None
    assert report.metrics.total_tokens_used == 10
    assert report.accepted_changes[0].lease_generation == 1
    assert adapter.metrics.calls == 2
    assert adapter.metrics.verifier_calls == 1


@pytest.mark.asyncio
async def test_adapter_rejects_reused_agent_and_read_only_change() -> None:
    reused = FakeLegacyAgent("reused")
    adapter = LegacyAgentDAGAdapter(lambda role, envelope: reused)
    graph = TaskGraph(
        nodes=(
            TaskNode(node_id="explore", role=AgentRole.EXPLORER, objective="read"),
            TaskNode(
                node_id="verify",
                role=AgentRole.VERIFIER,
                objective="verify",
                depends_on=("explore",),
            ),
        )
    )
    report = await adapter.run(
        graph, budget=ScheduleBudget(total_tokens=2_000, wall_time_seconds=10)
    )
    assert report.nodes["verify"].status is NodeStatus.FAILED
    assert "fresh Agent" in (report.nodes["verify"].failure_reason or "")

    async def readonly_change(envelope, agent):
        return ChangeEnvelope.from_lease(envelope.lease, write_set=("README.md",))

    safe_adapter = LegacyAgentDAGAdapter(
        lambda role, envelope: FakeLegacyAgent(role.value),
        change_collector=readonly_change,
    )
    readonly_graph = TaskGraph(
        nodes=(TaskNode(node_id="verify", role=AgentRole.VERIFIER, objective="verify"),)
    )
    readonly_report = await safe_adapter.run(
        readonly_graph,
        budget=ScheduleBudget(total_tokens=1_000, wall_time_seconds=10),
    )
    assert readonly_report.nodes["verify"].status is NodeStatus.FAILED
    assert "attempted a mutation" in (readonly_report.nodes["verify"].failure_reason or "")


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_explicit_agent_closer_runs_on_failure_or_cancellation(cancelled) -> None:
    closed: list[str] = []

    class FailingAgent(FakeLegacyAgent):
        async def run_to_completion(self, task, conversation=None, event_callback=None):
            if cancelled:
                raise asyncio.CancelledError
            raise RuntimeError("model failed")

    async def closer(agent: FailingAgent) -> None:
        closed.append(agent.name)

    adapter = LegacyAgentDAGAdapter(
        lambda role, envelope: FailingAgent(envelope.node.node_id),
        agent_closer=closer,
    )
    graph = TaskGraph(
        nodes=(TaskNode(node_id="node", role=AgentRole.EXPLORER, objective="x"),)
    )

    report = await adapter.run(
        graph,
        budget=ScheduleBudget(total_tokens=1_000, wall_time_seconds=10),
    )

    assert closed == ["node"]
    expected = NodeStatus.CANCELLED if cancelled else NodeStatus.FAILED
    assert report.nodes["node"].status is expected


@pytest.mark.asyncio
async def test_factory_adapter_builds_real_agent_shape_with_verifier_policy() -> None:
    class ConstructedAgent(FakeLegacyAgent):
        def __init__(self, **kwargs):
            super().__init__("constructed")
            self.kwargs = kwargs

    registry_instances: list[object] = []

    def registry_factory(role, envelope):
        value = ToolRegistry()
        registry_instances.append(value)
        return value

    factory = LegacyAgentFactoryAdapter(
        client_factory=lambda role, envelope: object(),
        registry_factory=registry_factory,
        protocol="anthropic",
        work_dir=".",
        agent_class=ConstructedAgent,
    )
    graph = TaskGraph(
        nodes=(
            TaskNode(node_id="explore", role=AgentRole.EXPLORER, objective="inspect"),
            TaskNode(
                node_id="verify",
                role=AgentRole.VERIFIER,
                objective="verify",
                depends_on=("explore",),
            ),
        )
    )
    adapter = LegacyAgentDAGAdapter(factory)
    report = await adapter.run(
        graph,
        # This test exercises factory/policy shape, not the global timeout.
        # Leave enough headroom for loaded Windows CI hosts where event-loop
        # scheduling can otherwise consume the deliberately small DAG budget.
        budget=ScheduleBudget(total_tokens=2_000, wall_time_seconds=30),
    )
    assert report.status is RunStatus.SUCCEEDED
    assert len(registry_instances) == 2
    assert registry_instances[0] is not registry_instances[1]


@pytest.mark.asyncio
async def test_factory_adapter_host_normalizes_verifier_definition() -> None:
    from mewcode.agents.parser import AgentDef

    class ConstructedAgent(FakeLegacyAgent):
        def __init__(self, **kwargs):
            super().__init__("constructed")
            self.kwargs = kwargs

    class Loader:
        def get(self, name):
            return AgentDef(
                agent_type=name,
                when_to_use="test",
                tools=["ReadFile", "WriteFile"],
                permission_mode="default",
            )

    factory = LegacyAgentFactoryAdapter(
        client_factory=lambda role, envelope: object(),
        registry_factory=lambda role, envelope: ToolRegistry(),
        protocol="anthropic",
        work_dir=".",
        agent_loader=Loader(),
        agent_class=ConstructedAgent,
    )
    graph = TaskGraph(
        nodes=(TaskNode(node_id="verify", role=AgentRole.VERIFIER, objective="verify"),)
    )
    report = await LegacyAgentDAGAdapter(factory).run(
        graph,
        budget=ScheduleBudget(total_tokens=1_000, wall_time_seconds=10),
    )
    # The factory normalizes all read-only roles to a host-owned deny-list;
    # an unsafe project AgentDef cannot widen Verifier capability.
    assert report.nodes["verify"].status is NodeStatus.SUCCEEDED

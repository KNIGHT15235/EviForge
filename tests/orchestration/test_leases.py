from __future__ import annotations

from mewcode.orchestration import (
    AgentRole,
    ChangeEnvelope,
    LeaseRegistry,
    TaskNode,
)


def test_generation_fencing_rejects_stale_change_after_redispatch() -> None:
    task = TaskNode(
        node_id="writer",
        role=AgentRole.IMPLEMENTER,
        objective="write a scoped file",
        predicted_write_set=("src/**",),
    )
    leases = LeaseRegistry("run-1")
    first = leases.issue(task)
    stale_change = ChangeEnvelope.from_lease(
        first, write_set=("src/old-generation.py",)
    )
    second = leases.issue(task)

    valid, reason = leases.validate(stale_change)
    assert valid is False
    assert reason == "stale lease generation"
    assert second.generation == first.generation + 1

    current_change = ChangeEnvelope.from_lease(
        second, write_set=("src/current.py",)
    )
    assert leases.validate(current_change) == (True, None)


def test_fencing_checks_lease_id_and_write_scope() -> None:
    task = TaskNode(
        node_id="writer",
        role=AgentRole.INTEGRATOR,
        objective="integrate one subtree",
        predicted_write_set=("src/api/**",),
    )
    leases = LeaseRegistry("run-1")
    lease = leases.issue(task)

    wrong_id = ChangeEnvelope(
        node_id="writer",
        lease_id="forged",
        lease_generation=lease.generation,
        write_set=("src/api/client.py",),
    )
    assert leases.validate(wrong_id)[1] == "lease id does not match active generation"

    outside = ChangeEnvelope.from_lease(lease, write_set=("src/model.py",))
    assert leases.validate(outside)[1] == "change exceeds predicted write-set"

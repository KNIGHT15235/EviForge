"""Lifecycle invariants for evolved skill versions."""

from __future__ import annotations

from mewcode.evolution.models import EvolutionState


class InvalidEvolutionTransition(ValueError):
    pass


_NORMAL_TRANSITIONS: dict[EvolutionState, frozenset[EvolutionState]] = {
    EvolutionState.QUARANTINE: frozenset(
        {EvolutionState.CANARY, EvolutionState.DEPRECATED}
    ),
    EvolutionState.CANARY: frozenset(
        {EvolutionState.ACTIVE, EvolutionState.DEPRECATED, EvolutionState.ROLLED_BACK}
    ),
    EvolutionState.ACTIVE: frozenset(
        {EvolutionState.DEPRECATED, EvolutionState.ROLLED_BACK}
    ),
    EvolutionState.DEPRECATED: frozenset(),
    EvolutionState.ROLLED_BACK: frozenset(),
}


def require_transition(
    current: EvolutionState,
    target: EvolutionState,
    *,
    rollback_restore: bool = False,
) -> None:
    """Reject lifecycle skips; rollback restore is a narrowly scoped exception."""

    if rollback_restore and (
        current is EvolutionState.DEPRECATED and target is EvolutionState.ACTIVE
    ):
        return
    if target not in _NORMAL_TRANSITIONS[current]:
        raise InvalidEvolutionTransition(
            f"invalid evolution transition: {current.value} -> {target.value}"
        )

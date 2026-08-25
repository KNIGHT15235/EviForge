"""Stable process exit-code families for CLI automation."""

from enum import IntEnum


class ExitCode(IntEnum):
    OK = 0
    RUNTIME_FAILURE = 1
    CONFIGURATION = 2
    AUTHENTICATION = 3
    NETWORK = 4
    PERMISSION = 5
    EVIDENCE_GATE = 6
    BUDGET = 7
    INTERNAL = 70


class BudgetExceededError(RuntimeError):
    """Typed stop raised when a non-DAG headless execution exhausts its budget."""

    error_code = "budget.exceeded"


__all__ = ["BudgetExceededError", "ExitCode"]

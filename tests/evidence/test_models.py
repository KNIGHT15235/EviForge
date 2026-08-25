from __future__ import annotations

import pytest
from pydantic import ValidationError

from mewcode.evidence import RequirementContract, RequirementCriterion, VerifierSpec


def test_contract_rejects_unknown_verifier_reference() -> None:
    with pytest.raises(ValidationError, match="unknown verifiers"):
        RequirementContract(
            task_id="task-1",
            objective="Implement a feature",
            criteria=(
                RequirementCriterion(
                    criterion_id="criterion-1",
                    description="Feature is observable",
                    verifier_ids=("missing",),
                ),
            ),
        )


def test_contract_round_trips_as_strict_pydantic_json() -> None:
    contract = RequirementContract(
        task_id="task-1",
        objective="Implement a feature",
        criteria=(
            RequirementCriterion(
                criterion_id="criterion-1",
                description="Feature is observable",
                verifier_ids=("unit",),
            ),
        ),
        verifiers=(VerifierSpec(verifier_id="unit", name="unit tests", argv=("pytest", "-q")),),
    )

    restored = RequirementContract.model_validate_json(contract.model_dump_json())

    assert restored == contract
    with pytest.raises(ValidationError):
        RequirementContract.model_validate({**contract.model_dump(), "misspelled_field": True})


def test_verifier_requires_an_argument_vector() -> None:
    with pytest.raises(ValidationError):
        VerifierSpec(verifier_id="unit", name="unit", argv=())


def test_durable_identifier_cannot_traverse_store_paths() -> None:
    with pytest.raises(ValidationError):
        RequirementContract(
            contract_id="../escape",
            task_id="task-1",
            objective="Implement a feature",
            criteria=(RequirementCriterion(criterion_id="criterion", description="done"),),
        )

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from mewcode.commands.handlers.experience import handle_experience
from mewcode.commands.registry import CommandContext
from mewcode.evolution import (
    EvolutionRegistry,
    ExperienceCandidate,
    ProductionEvolutionAdapter,
    PromotionGateError,
    ScopeKind,
    SkillScope,
)
from mewcode.runtime import RuntimeStore


@dataclass
class RecordingUI:
    messages: list[str] = field(default_factory=list)

    def add_system_message(self, text: str) -> None:
        self.messages.append(text)


def _context(
    adapter,
    ui: RecordingUI,
    args: str,
    *,
    agent=None,
    session=None,
) -> CommandContext:
    return CommandContext(
        args=args,
        agent=agent,
        conversation=None,
        session=session,
        session_manager=None,
        memory_manager=None,
        ui=ui,
        config={"evolution_adapter": adapter},
    )


def _candidate(candidate_id: str) -> ExperienceCandidate:
    project = "project-command"
    return ExperienceCandidate(
        candidate_id=candidate_id,
        task_signature="fix asyncio cancellation leak",
        failure_signature="CancelledError swallowed in worker",
        root_cause_family="asyncio-cancellation",
        symptom="Worker hangs after cancellation.",
        context_constraints=("Python 3.11+",),
        decision="Re-raise cancellation after deterministic cleanup.",
        procedure=("Clean resources in finally.", "Run the replay fixture."),
        failed_attempts=("Only cancel the outer task.",),
        evidence_refs=(f"evidence://{candidate_id}/receipt",),
        source_trace_ids=(f"trace-{candidate_id}",),
        source_commit="a1b2c3d",
        source_code_hash="a" * 64,
        project_fingerprint=project,
        scope=SkillScope(
            kind=ScopeKind.PROJECT,
            project_fingerprints=(project,),
            languages=("python",),
        ),
        confidence_prior=0.5,
        created_at=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_experience_command_closes_review_validate_promote_loop(
    tmp_path,
) -> None:
    store = RuntimeStore(control_root=tmp_path / "control", workspace_id="command")
    with EvolutionRegistry(tmp_path / "evolution.db") as registry:
        adapter = ProductionEvolutionAdapter(registry, store, workspace=tmp_path)
        registry.register_candidate(_candidate("candidate-command"))
        ui = RecordingUI()

        await handle_experience(_context(adapter, ui, "status"))
        assert "candidate/新版本默认 quarantine" in ui.messages[-1]

        await handle_experience(
            _context(adapter, ui, "review candidate candidate-command")
        )
        review = ui.messages[-1]
        assert "trace-candidate-command" in review
        assert "evidence://candidate-command/receipt" in review
        assert "来源代码哈希" in review

        await handle_experience(
            _context(
                adapter,
                ui,
                "create candidate-command async-command "
                "repo-group-a,repo-group-b Async command cleanup",
            )
        )
        assert "in quarantine" in ui.messages[-1]
        assert "manifest=" in ui.messages[-1]

        with pytest.raises(PromotionGateError, match="replay/validation gate"):
            await handle_experience(
                _context(adapter, ui, "promote async-command 1 canary confirm")
            )

        await handle_experience(
            _context(
                adapter,
                ui,
                "validate async-command 1 repo-group-a repository-a run-a "
                "pass 0.25 0.80 evidence://replay/run-a",
            )
        )
        assert "Recorded replay" in ui.messages[-1]
        assert "canary=True" in ui.messages[-1]

        await handle_experience(
            _context(adapter, ui, "promote async-command 1 canary confirm")
        )
        assert "→ canary" in ui.messages[-1]

        await handle_experience(
            _context(adapter, ui, "review skill async-command 1")
        )
        skill_review = ui.messages[-1]
        assert "run=run-a" in skill_review
        assert "evidence=evidence://replay/run-a" in skill_review
        assert "Manifest 哈希" in skill_review
        assert "审计轨迹" in skill_review

        await handle_experience(
            _context(
                adapter,
                ui,
                "validate async-command 1 repo-group-b repository-b run-b "
                "pass 0.20 0.75 evidence://replay/run-b",
            )
        )
        await handle_experience(
            _context(adapter, ui, "promote async-command 1 active confirm")
        )
        feedback_agent = SimpleNamespace(
            _last_evidence_bundle_ref="evidence://task-feedback/failure",
            task_runtime=SimpleNamespace(task_id="task-feedback"),
        )
        await handle_experience(
            _context(
                adapter,
                ui,
                "feedback async-command 1 harm",
                agent=feedback_agent,
            )
        )
        assert "rolled back automatically" in ui.messages[-1]
        assert registry.get_manifest("async-command", 1).rollout_state.value == (
            "rolled_back"
        )


@pytest.mark.asyncio
async def test_experience_promote_requires_explicit_confirmation(
    tmp_path,
) -> None:
    store = RuntimeStore(control_root=tmp_path / "control", workspace_id="confirm")
    with EvolutionRegistry(tmp_path / "evolution.db") as registry:
        adapter = ProductionEvolutionAdapter(registry, store, workspace=tmp_path)
        registry.register_candidate(_candidate("candidate-confirm"))
        ui = RecordingUI()
        await handle_experience(
            _context(
                adapter,
                ui,
                "create candidate-confirm async-confirm repo-a,repo-b Confirm skill",
            )
        )
        await handle_experience(
            _context(adapter, ui, "promote async-confirm 1 canary")
        )
        assert "用法:" in ui.messages[-1]


@pytest.mark.asyncio
async def test_experience_rollback_requires_confirmation_and_records_reason(
    tmp_path,
) -> None:
    store = RuntimeStore(control_root=tmp_path / "control", workspace_id="rollback")
    with EvolutionRegistry(tmp_path / "evolution.db") as registry:
        adapter = ProductionEvolutionAdapter(registry, store, workspace=tmp_path)
        registry.register_candidate(_candidate("candidate-rollback"))
        ui = RecordingUI()
        await handle_experience(
            _context(
                adapter,
                ui,
                "create candidate-rollback async-rollback repo-a,repo-b Rollback skill",
            )
        )
        await handle_experience(
            _context(
                adapter,
                ui,
                "validate async-rollback 1 repo-a repository-a rollback-run-a "
                "pass 0.20 0.80 evidence://replay/rollback-a",
            )
        )
        await handle_experience(
            _context(adapter, ui, "promote async-rollback 1 canary confirm")
        )
        await handle_experience(
            _context(adapter, ui, "rollback async-rollback 1 missing-reason")
        )
        assert "用法:" in ui.messages[-1]

        await handle_experience(
            _context(
                adapter,
                ui,
                "rollback async-rollback 1 confirm replay-regression",
            )
        )
        assert "reason=replay-regression" in ui.messages[-1]
        events = registry.audit_events()
        assert events[-1].event_type == "skill_rolled_back"
        assert events[-1].payload["reason"] == "replay-regression"

"""Rebuild disposable, human-readable Skill Markdown from a manifest."""

from __future__ import annotations

import json

from mewcode.evolution.models import SkillManifest


def _scalar(value: object) -> str:
    # JSON scalar syntax is a valid YAML subset and prevents frontmatter injection.
    return json.dumps(value, ensure_ascii=False)


def render_skill_markdown(manifest: SkillManifest) -> str:
    frontmatter = [
        "---",
        f"name: {_scalar(manifest.name)}",
        f"version: {manifest.version}",
        f"description: {_scalar(manifest.description)}",
        f"taskSignatures: {_scalar(list(manifest.task_signatures))}",
        f"failureSignatures: {_scalar(list(manifest.failure_signatures))}",
        f"riskLevel: {_scalar(manifest.risk_level.value)}",
        f"scope: {_scalar(manifest.scope.model_dump(mode='json'))}",
        f"sourceEvidence: {_scalar(list(manifest.evidence_refs))}",
        f"validatedAgainst: {_scalar(list(manifest.validation_ids))}",
        f"expiresAt: {_scalar(manifest.expires_at.isoformat() if manifest.expires_at else None)}",
        f"manifestHash: {_scalar(manifest.manifest_hash)}",
        "---",
        "",
        f"# {manifest.name}",
        "",
        manifest.description,
        "",
        "## Decision",
        "",
        manifest.decision,
        "",
        "## Procedure",
        "",
    ]
    procedure = [f"{index}. {step}" for index, step in enumerate(manifest.procedure, 1)]
    failed: list[str] = []
    if manifest.failed_attempts:
        failed = ["", "## Avoid", "", *(f"- {item}" for item in manifest.failed_attempts)]
    return "\n".join([*frontmatter, *procedure, *failed, ""])

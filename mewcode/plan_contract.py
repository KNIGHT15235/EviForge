"""Compile a reviewed plan into deterministic execution metadata.

This compiler extracts only explicit, machine-readable declarations.  It does
not ask an LLM to invent acceptance criteria or risk scope at approval time.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from mewcode.evidence import RequirementContract, RequirementCriterion, VerifierSpec


_DIRECTIVE = re.compile(r"^<!--\s*eviforge:(?P<kind>[a-z-]+)\s+(?P<body>.*?)\s*-->$")


@dataclass(frozen=True, slots=True)
class ExecutionManifest:
    schema_version: str
    plan_hash: str
    write_set: tuple[str, ...]
    commands: tuple[tuple[str, ...], ...]
    network_hosts: tuple[str, ...]
    contract: RequirementContract


def compile_plan(
    plan_text: str,
    *,
    task_id: str,
    objective: str,
    workspace_root: str | Path,
    base_commit: str | None = None,
) -> ExecutionManifest:
    if not plan_text.strip():
        raise ValueError("plan must not be empty")
    workspace = Path(workspace_root).expanduser().resolve(strict=False)
    writes: list[str] = []
    commands: list[tuple[str, ...]] = []
    hosts: list[str] = []
    criteria: list[RequirementCriterion] = []
    verifiers: list[VerifierSpec] = []
    for line_number, line in enumerate(plan_text.splitlines(), 1):
        match = _DIRECTIVE.match(line.strip())
        if not match:
            continue
        kind = match.group("kind")
        body = match.group("body")
        try:
            value = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid EviForge directive on line {line_number}") from exc
        if kind == "write":
            if not isinstance(value, str) or not value.strip():
                raise ValueError("write directive requires a relative path string")
            path = Path(value)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("write directive escapes the workspace")
            resolved = (workspace / path).resolve(strict=False)
            try:
                relative = resolved.relative_to(workspace)
            except ValueError as exc:
                raise ValueError("write directive escapes the workspace") from exc
            writes.append(relative.as_posix())
        elif kind == "command":
            if not isinstance(value, list) or not value or not all(isinstance(item, str) for item in value):
                raise ValueError("command directive requires a non-empty argv JSON array")
            commands.append(tuple(value))
        elif kind == "network":
            if not isinstance(value, str) or not value.strip():
                raise ValueError("network directive requires a host string")
            hosts.append(value.casefold())
        elif kind == "criterion":
            if not isinstance(value, dict):
                raise ValueError("criterion directive requires a JSON object")
            criterion_id = str(value.get("id", "")).strip()
            description = str(value.get("description", "")).strip()
            argv = value.get("argv")
            if not criterion_id or not description:
                raise ValueError("criterion requires id and description")
            verifier_ids: tuple[str, ...] = ()
            if argv is not None:
                if not isinstance(argv, list) or not argv or not all(isinstance(item, str) for item in argv):
                    raise ValueError("criterion argv must be a non-empty JSON string array")
                verifier_id = f"verify-{criterion_id}"
                verifiers.append(
                    VerifierSpec(
                        verifier_id=verifier_id,
                        name=description,
                        argv=tuple(argv),
                        cwd=str(value.get("cwd", ".")),
                        timeout_seconds=float(value.get("timeout", 300)),
                    )
                )
                verifier_ids = (verifier_id,)
            elif bool(value.get("required", True)):
                raise ValueError(
                    "required criterion must declare an argv verifier"
                )
            criteria.append(
                RequirementCriterion(
                    criterion_id=criterion_id,
                    description=description,
                    required=bool(value.get("required", True)),
                    verifier_ids=verifier_ids,
                )
            )
        else:
            raise ValueError(f"unknown EviForge directive: {kind}")
    if not criteria:
        raise ValueError("plan has no explicit acceptance criteria")
    canonical = plan_text.replace("\r\n", "\n").encode("utf-8")
    contract = RequirementContract(
        task_id=task_id,
        objective=objective,
        base_commit=base_commit,
        criteria=tuple(criteria),
        verifiers=tuple(verifiers),
    )
    return ExecutionManifest(
        schema_version="1.0",
        plan_hash=hashlib.sha256(canonical).hexdigest(),
        write_set=tuple(dict.fromkeys(writes)),
        commands=tuple(commands),
        network_hosts=tuple(dict.fromkeys(hosts)),
        contract=contract,
    )


def example_directives() -> str:
    return (
        '<!-- eviforge:write "src/module.py" -->\n'
        '<!-- eviforge:command ["python", "-m", "pytest", "-q"] -->\n'
        '<!-- eviforge:criterion {"id":"tests","description":"tests pass",'
        '"argv":["python","-m","pytest","-q"]} -->'
    )

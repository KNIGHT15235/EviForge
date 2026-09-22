"""Local operator commands; none of these actions are registered as agent tools."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from eviforge.governance import GovernanceError, GovernanceService
from eviforge.skills.parser import SkillParseError


def register_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("governance", help="Review memory and skill candidates")
    parser.add_argument("--work-dir", default=".")
    parser.add_argument("--scope", choices=("project", "user"), default="project")
    actions = parser.add_subparsers(dest="governance_action", required=True)
    for name in ("propose", "import"):
        propose = actions.add_parser(name, help="Import an explicit file as a quarantine candidate")
        propose.add_argument("--kind", choices=("memory", "skill"), required=True)
        propose.add_argument("--name", required=True)
        propose.add_argument("--file", required=True)
        propose.add_argument("--source-task", required=True)
        propose.add_argument("--source-trace", required=True)
    listing = actions.add_parser("list")
    listing.add_argument("--kind", choices=("memory", "skill"))
    listing.add_argument("--status", choices=("quarantine", "verified", "published", "superseded", "revoked"))
    actions.add_parser("show").add_argument("entry_id")
    verify = actions.add_parser("verify", help="Record verification evidence for an exact content hash")
    verify.add_argument("entry_id")
    verify.add_argument("--content-hash", required=True)
    verify.add_argument("--outcome", choices=("pass", "fail"), required=True)
    verify.add_argument("--evidence", required=True, help="Path to a nonempty JSON object")
    verify.add_argument("--validator", required=True)
    confirm = actions.add_parser("confirm", help="Explicit human confirmation of an exact content hash")
    confirm.add_argument("entry_id")
    confirm.add_argument("--content-hash", required=True)
    confirm.add_argument("--actor", required=True)
    publish = actions.add_parser("publish")
    publish.add_argument("entry_id")
    publish.add_argument("--actor", required=True)
    revoke = actions.add_parser("revoke")
    revoke.add_argument("entry_id")
    revoke.add_argument("--actor", required=True)
    revoke.add_argument("--reason", required=True)
    feedback = actions.add_parser("feedback")
    feedback.add_argument("entry_id")
    feedback.add_argument("--sentiment", choices=("positive", "negative"), required=True)
    feedback.add_argument("--reason", required=True)
    feedback.add_argument("--source-task", required=True)
    rollback = actions.add_parser("rollback")
    rollback.add_argument("--kind", choices=("memory", "skill"), required=True)
    rollback.add_argument("--name", required=True)
    rollback.add_argument("--version", type=int, required=True)
    rollback.add_argument("--actor", required=True)


def handle(args: argparse.Namespace) -> int:
    service = GovernanceService(args.work_dir)
    scope = args.scope
    action = args.governance_action
    try:
        if action in ("propose", "import"):
            content = Path(args.file).read_text(encoding="utf-8-sig")
            propose = service.propose_memory if args.kind == "memory" else service.propose_skill
            result = propose(content, scope=scope, name=args.name,
                             source_task=args.source_task, source_trace=args.source_trace)
        elif action == "list":
            result = service.list_entries(scope=scope, status=args.status, kind=args.kind)
        elif action == "show":
            result = service.get_entry(args.entry_id, scope=scope)
        elif action == "verify":
            evidence = json.loads(Path(args.evidence).read_text(encoding="utf-8-sig"))
            result = service.record_verification(
                args.entry_id, scope=scope, content_hash=args.content_hash,
                outcome=args.outcome, evidence=evidence, validator=args.validator,
            )
        elif action == "confirm":
            result = service.confirm(args.entry_id, scope=scope, content_hash=args.content_hash, actor=args.actor)
        elif action == "publish":
            result = service.publish(args.entry_id, scope=scope, actor=args.actor)
        elif action == "feedback":
            result = service.feedback(args.entry_id, scope=scope, sentiment=args.sentiment,
                                      reason=args.reason, source_task=args.source_task)
        elif action == "revoke":
            result = service.revoke(args.entry_id, scope=scope, actor=args.actor, reason=args.reason)
        elif action == "rollback":
            result = service.rollback(kind=args.kind, name=args.name, target_version=args.version,
                                      scope=scope, actor=args.actor)
        else:
            raise GovernanceError(f"Unknown governance action: {action}")
    except (GovernanceError, SkillParseError, OSError, sqlite3.Error, ValueError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps({"ok": True, "result": result}, ensure_ascii=False))
    return 0

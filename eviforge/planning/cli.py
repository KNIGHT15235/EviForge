"""Explicit local operator commands; never registered as Agent tools."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from eviforge.planning import PlanError, PlanService


def register_parser(subparsers: Any) -> None:
    parser = subparsers.add_parser("plan", help="Inspect, submit, approve or reject a versioned plan")
    parser.add_argument("--work-dir", default=".")
    actions = parser.add_subparsers(dest="plan_command", required=True)
    create = actions.add_parser("create", help="Create and submit a reviewable plan")
    create.add_argument("--session-id", required=True)
    create.add_argument("--turn-id", required=True)
    create.add_argument("--content-file", required=True)
    create.add_argument("--actions-file", help="JSON array of exact tool action manifests")
    actions.add_parser("list")
    show = actions.add_parser("show")
    show.add_argument("plan_id")
    approve = actions.add_parser("approve", help="Record explicit human approval; runtime execution still requires --approved-plan and --plan-hash")
    approve.add_argument("plan_id")
    approve.add_argument("--hash", dest="content_hash", required=True)
    approve.add_argument("--session-id", required=True)
    approve.add_argument("--source-turn-id", required=True)
    approve.add_argument("--execution-turn-id", required=True)
    approve.add_argument("--agent-id", required=True)
    approve.add_argument("--ttl-seconds", type=float, default=300)
    reject = actions.add_parser("reject")
    reject.add_argument("plan_id")
    reject.add_argument("--reason", default="user rejected")


def handle(args: argparse.Namespace) -> int:
    try:
        service = PlanService(args.work_dir)
        if args.plan_command == "create":
            path = Path(args.content_file).resolve()
            content = path.read_text(encoding="utf-8")
            actions = json.loads(Path(args.actions_file).read_text(encoding="utf-8")) if args.actions_file else []
            if not isinstance(actions, list):
                raise ValueError("actions-file must contain a JSON array")
            plan = service.create(args.session_id, args.turn_id, content, actions, plan_path=path)
            result: Any = service.submit(plan.plan_id).as_dict()
        elif args.plan_command == "list":
            result = [plan.as_dict() for plan in service.list_plans()]
        elif args.plan_command == "show":
            result = service.get(args.plan_id).as_dict()
        elif args.plan_command == "approve":
            result = service.approve(args.plan_id, args.content_hash, session_id=args.session_id,
                source_turn_id=args.source_turn_id, execution_turn_id=args.execution_turn_id,
                agent_id=args.agent_id, ttl_seconds=args.ttl_seconds).as_dict()
            result["execution_authority"] = "No grant survives this process. Start an explicit --approved-plan/--plan-hash run to authorize execution."
        elif args.plan_command == "reject":
            result = service.reject(args.plan_id, args.reason).as_dict()
        else:
            raise ValueError("Unknown plan command")
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except (PlanError, OSError, ValueError, TypeError, KeyError) as exc:
        print(json.dumps({"error": getattr(exc, "code", "INVALID_ARGUMENT"), "message": str(exc)}, ensure_ascii=False))
        return 2

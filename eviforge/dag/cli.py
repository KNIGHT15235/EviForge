"""Explicit CLI adapters; schema/validate never load Provider configuration."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from eviforge.dag.graph import DAGError, ReplayRequired, validate_graph
from eviforge.dag.models import GraphSpec, OUTPUT_ADAPTER, INPUT_ADAPTER


def register_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("dag", help="Validate and run typed Agent dependency graphs")
    actions = parser.add_subparsers(dest="dag_action", required=True)
    schema = actions.add_parser("schema", help="Print versioned graph and output schemas")
    schema.set_defaults(handler=handle)
    validate = actions.add_parser("validate", help="Validate topology and input contracts offline")
    validate.add_argument("graph", type=Path)
    validate.set_defaults(handler=handle)
    events = actions.add_parser("events", help="Export stored node events as JSONL without a Provider")
    events.add_argument("run_id")
    events.add_argument("--work-dir", type=Path, default=Path.cwd())
    events.add_argument("--after", type=int, default=0, help="Return events after this sequence number")
    events.set_defaults(handler=handle)
    run = actions.add_parser("run", help="Run a graph with actual Agents and a durable journal")
    run.add_argument("graph", type=Path)
    run.add_argument("--work-dir", type=Path, default=Path.cwd())
    run.add_argument("--config", type=Path)
    from eviforge.permissions import PermissionMode
    run.add_argument("--mode", choices=[mode.value for mode in PermissionMode])
    identity = run.add_mutually_exclusive_group()
    identity.add_argument("--resume", metavar="RUN_ID")
    identity.add_argument("--run-id", help="Choose a new run ID so it is known before execution starts")
    run.add_argument("--replay-node", action="append", default=[], help="Explicitly authorize retry of this unfinished writable node")
    run.add_argument("--replay-reason", default="")
    run.set_defaults(handler=handle)


async def handle(args: argparse.Namespace) -> int:
    from pydantic import ValidationError
    from eviforge.automation import EXIT_CODES
    runtime = None
    try:
        if args.dag_action == "schema":
            print(json.dumps({"schema_version": "1.0", "graph": GraphSpec.model_json_schema(),
                              "node_input": INPUT_ADAPTER.json_schema(),
                              "node_output": OUTPUT_ADAPTER.json_schema()}, ensure_ascii=False, indent=2))
            return 0
        if args.dag_action == "events":
            from eviforge.dag.journal import SQLiteJournal
            path = args.work_dir.resolve() / ".eviforge" / "dag" / "journal.sqlite3"
            if not path.is_file():
                raise DAGError("No DAG journal found")
            journal = SQLiteJournal(path)
            try:
                for row in journal.events(args.run_id):
                    if row["seq"] > args.after:
                        row["data"] = json.loads(row["data"])
                        print(json.dumps({"schema_version": "1.0", **row}, ensure_ascii=False))
            finally:
                journal.close()
            return 0
        graph = GraphSpec.model_validate_json(args.graph.read_text(encoding="utf-8"))
        order = validate_graph(graph)
        if args.dag_action == "validate":
            print(json.dumps({"valid": True, "schema_version": "1.0", "order": order}))
            return 0
        from eviforge.client import create_client
        from eviforge.config import load_config
        from eviforge.permissions import PermissionMode
        from eviforge.runtime import RuntimeServices
        from eviforge.dag.scheduler import DAGRunner
        config = load_config(args.config)
        provider = config.providers[0]
        runtime = RuntimeServices.create(config, provider, client=create_client(provider),
                    work_dir=str(args.work_dir.resolve()),
                    permission_mode=PermissionMode(args.mode or config.permission_mode), interactive=False)
        runtime.begin_turn()
        result = await DAGRunner(runtime.agent).run(graph, run_id=args.resume or args.run_id, resume=bool(args.resume),
                        replay_nodes=set(args.replay_node), replay_reason=args.replay_reason)
        print(result.model_dump_json(indent=2))
        return EXIT_CODES[result.status]
    except (DAGError, ValidationError, OSError, ValueError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False))
        return EXIT_CODES["blocked"] if isinstance(exc, ReplayRequired) else EXIT_CODES["config_error"]
    finally:
        if runtime is not None:
            await runtime.close()

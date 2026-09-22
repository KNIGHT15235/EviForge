"""Offline DAG behavior through the real Agent, real tools and real SQLite."""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from eviforge.agent import Agent
from eviforge.client import LLMClient
from eviforge.dag import DAGRunner, GraphSpec, NodeSpec, InputRef, DAGError, DriftError, ReplayRequired, validate_graph
from eviforge.dag.capabilities import capability_hash
from eviforge.dag.graph import conflicts
from eviforge.dag.journal import SQLiteJournal
from eviforge.permissions import PermissionChecker, PermissionMode, DangerousCommandDetector, PathSandbox, RuleEngine
from eviforge.tools import create_default_registry
from eviforge.tools.base import StreamEnd, TextDelta, ToolCallComplete


class ScriptedClient(LLMClient):
    def __init__(self, scripts):
        self.scripts = scripts
        self.calls = Counter()
        self.schemas = {}

    async def stream(self, conversation, system="", tools=None):
        prompt = next(json.loads(m.content) for m in conversation.history
                      if m.role == "user" and m.content.startswith('{"commands":'))
        node = prompt["node_id"]
        self.schemas[node] = tools
        index = self.calls[node]
        self.calls[node] += 1
        steps = self.scripts[node]
        step = steps[index] if index < len(steps) else "done"
        if callable(step):
            step = step(conversation, prompt)
            if asyncio.iscoroutine(step):
                step = await step
        if isinstance(step, Exception):
            raise step
        if isinstance(step, tuple):
            yield ToolCallComplete(f"{node}-{index}", step[0], step[1])
        else:
            yield TextDelta(step)
        yield StreamEnd("end_turn", input_tokens=10, output_tokens=5)


@pytest.fixture
def project(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.chdir(root)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return root


def parent(project, scripts, mode=PermissionMode.DONT_ASK):
    client = ScriptedClient(scripts)
    checker = PermissionChecker(DangerousCommandDetector(), PathSandbox(str(project)), RuleEngine(), mode)
    return Agent(client, create_default_registry(), "anthropic", str(project),
                 permission_checker=checker, context_window=100000)


def submit(role="explorer", **kwargs):
    data = {"role": role, "summary": "checked", **kwargs}
    if role == "explorer":
        data.setdefault("findings", [])
    elif role in {"implementer", "integrator"}:
        data.setdefault("changes", [])
    return ("SubmitNodeResult", data)


def captured(conversation):
    refs = []
    for message in conversation.history:
        for result in message.tool_results:
            if result.content.startswith('{"sha256":'):
                refs.append(json.loads(result.content))
    return refs


def write_script(path, content="new"):
    return [("WriteFile", {"file_path": path, "content": content}),
            ("CaptureArtifact", {"file_path": path}),
            lambda conv, prompt: submit("implementer", changes=[path], evidence=captured(conv))]


def test_graph_rejects_cycle_missing_and_mistyped_references():
    for nodes in [
        [NodeSpec(id="a", role="explorer", goal="x", depends_on=["missing"])],
        [NodeSpec(id="a", role="explorer", goal="x", depends_on=["a"])],
        [NodeSpec(id="a", role="explorer", goal="x"), NodeSpec(id="b", role="implementer", goal="x",
            depends_on=["a"], inputs=[InputRef(node_id="a", role="verifier")])],
        [NodeSpec(id="a", role="verifier", goal="x")],
        [NodeSpec(id="a", role="explorer", goal="x", write_set=["file"])],
    ]:
        with pytest.raises(DAGError):
            validate_graph(GraphSpec(nodes=nodes))


@pytest.mark.parametrize("path", ["../escape", "/absolute", "C:\\outside", "src/*.py", ".git", ".eviforge/db"])
def test_strict_scope_contract(path):
    with pytest.raises(ValidationError):
        NodeSpec(id="a", role="implementer", goal="x", write_set=[path])


def test_strict_extra_fields_and_bool_coercion():
    with pytest.raises(ValidationError):
        NodeSpec(id="a", role="explorer", goal="x", surprise=True)
    from eviforge.dag.models import CheckResult
    with pytest.raises(ValidationError):
        CheckResult(name="x", passed="true", details="")


def test_directory_and_read_write_conflicts(project):
    a = NodeSpec(id="a", role="implementer", goal="x", read_set=["src/a"], write_set=["src"])
    b = NodeSpec(id="b", role="explorer", goal="x", read_set=["src/deep/b"])
    c = NodeSpec(id="c", role="implementer", goal="x", read_set=["docs"], write_set=["docs/a"])
    assert conflicts(a, b, project)
    assert not conflicts(a, c, project)


@pytest.mark.asyncio
async def test_real_agent_four_role_chain_and_evidence(project):
    scripts = {"e": [submit(findings=["add a file"])], "i": write_script("result.txt"),
               "v": [("ReadFile", {"file_path": "result.txt"}),
                     submit("verifier", implementation_refs=["i"], verdict="pass",
                            checks=[{"name": "content", "passed": True, "details": "inspected"}])],
               "g": [submit("integrator", implementation_refs=["i"], verification_refs=["v"])]}
    agent = parent(project, scripts)
    graph = GraphSpec(nodes=[NodeSpec(id="e", role="explorer", goal="explore"),
        NodeSpec(id="i", role="implementer", goal="write", depends_on=["e"],
                 inputs=[InputRef(node_id="e", role="explorer")], write_set=["result.txt"]),
        NodeSpec(id="v", role="verifier", goal="verify", depends_on=["i"], inputs=[InputRef(node_id="i", role="implementer")]),
        NodeSpec(id="g", role="integrator", goal="integrate", depends_on=["i", "v"],
                 inputs=[InputRef(node_id="i", role="implementer"), InputRef(node_id="v", role="verifier")])])
    result = await DAGRunner(agent).run(graph)
    assert result.status == "success", result.model_dump()
    assert (project / "result.txt").read_text() == "new"
    evidence = result.nodes["i"].output.evidence[0]
    assert (project / ".eviforge/dag/artifacts" / evidence.sha256).read_text() == "new"
    assert {s["name"] for s in agent.client.schemas["e"]} == {"ReadFile", "Glob", "Grep", "CaptureArtifact", "SubmitNodeResult"}
    counts = agent.client.calls.copy()
    resumed = await DAGRunner(agent).run(graph, run_id=result.run_id, resume=True)
    assert resumed.status == "success"
    assert counts == agent.client.calls
    journal = SQLiteJournal(project / ".eviforge/dag/journal.sqlite3")
    events = journal.events(result.run_id)
    journal.close()
    assert sum(e["type"] == "node_started" for e in events) == 4
    assert any(e["type"] == "tool_started" for e in events)
    assert any(e["type"] == "artifact_captured" for e in events)


@pytest.mark.asyncio
async def test_real_edit_requires_read_and_captures_after_state(project):
    (project / "code.txt").write_text("old")
    agent = parent(project, {"i": [("ReadFile", {"file_path": "code.txt"}),
        ("EditFile", {"file_path": "code.txt", "old_string": "old", "new_string": "new"}),
        ("CaptureArtifact", {"file_path": "code.txt"}),
        lambda conv, prompt: submit("implementer", changes=["code.txt"], evidence=captured(conv))]})
    result = await DAGRunner(agent).run(GraphSpec(nodes=[NodeSpec(id="i", role="implementer", goal="edit", write_set=["code.txt"])]))
    assert result.status == "success", result.model_dump()
    assert (project / "code.txt").read_text() == "new"


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["other.txt", "../outside.txt", ".eviforge/journal.sqlite3"])
async def test_real_agent_cannot_escape_write_set(project, target):
    agent = parent(project, {"i": [("WriteFile", {"file_path": target, "content": "bad"})]})
    result = await DAGRunner(agent).run(GraphSpec(nodes=[NodeSpec(id="i", role="implementer", goal="x", write_set=["allowed.txt"])]))
    assert result.status == "ambiguous"
    assert not (project / target).exists()


@pytest.mark.asyncio
async def test_symlink_escape_is_denied(project):
    outside = project.parent / "outside"
    outside.mkdir()
    (project / "link").symlink_to(outside, target_is_directory=True)
    agent = parent(project, {"i": [("WriteFile", {"file_path": "link/bad", "content": "bad"})]})
    result = await DAGRunner(agent).run(GraphSpec(nodes=[NodeSpec(id="i", role="implementer", goal="x", write_set=["link"])]))
    assert result.status == "ambiguous"
    assert not (outside / "bad").exists()


@pytest.mark.asyncio
async def test_parent_plan_gate_is_inherited_at_final_tool_boundary(project):
    seen = []
    class Gate:
        def inherit(self, parent, child):
            child.plan_service = self
        def precheck(self, agent, tool, args, cwd):
            return SimpleNamespace(allowed=True, reason="prechecked")
        def consume(self, agent, tool, args, cwd):
            seen.append((tool.name, args, cwd))
            return SimpleNamespace(allowed=tool.name != "WriteFile", reason="revoked", code="revoked")
    agent = parent(project, {"i": [("WriteFile", {"file_path": "x", "content": "bad"})]})
    agent.plan_service = Gate()
    result = await DAGRunner(agent).run(GraphSpec(nodes=[NodeSpec(id="i", role="implementer", goal="x", write_set=["x"])]))
    assert result.status == "ambiguous"
    assert seen[0] == ("WriteFile", {"file_path": "x", "content": "bad"}, str(project))
    assert not (project / "x").exists()


@pytest.mark.asyncio
async def test_parent_permission_denial_cannot_be_elevated(project):
    agent = parent(project, {"i": [("WriteFile", {"file_path": "x", "content": "bad"})]}, PermissionMode.PLAN)
    result = await DAGRunner(agent).run(GraphSpec(nodes=[NodeSpec(id="i", role="implementer", goal="x", write_set=["x"])]))
    assert result.status == "ambiguous"
    assert not (project / "x").exists()


@pytest.mark.asyncio
async def test_writable_failure_blocks_automatic_replay(project):
    agent = parent(project, {"i": [("WriteFile", {"file_path": "x", "content": "written"}), RuntimeError("crash")]})
    graph = GraphSpec(nodes=[NodeSpec(id="i", role="implementer", goal="x", write_set=["x"])])
    result = await DAGRunner(agent).run(graph)
    assert result.status == "ambiguous"
    assert (project / "x").read_text() == "written"
    calls = agent.client.calls.copy()
    with pytest.raises(ReplayRequired):
        await DAGRunner(agent).run(graph, run_id=result.run_id, resume=True)
    assert agent.client.calls == calls


@pytest.mark.asyncio
async def test_capability_and_graph_drift_refuse_resume(project):
    agent = parent(project, {"e": [submit()]})
    graph = GraphSpec(nodes=[NodeSpec(id="e", role="explorer", goal="x")])
    result = await DAGRunner(agent).run(graph)
    changed = graph.model_copy(deep=True)
    changed.nodes[0].goal = "changed"
    with pytest.raises(DriftError):
        await DAGRunner(agent).run(changed, run_id=result.run_id, resume=True)
    agent.registry.disable("ReadFile")
    with pytest.raises(DriftError):
        await DAGRunner(agent).run(graph, run_id=result.run_id, resume=True)


@pytest.mark.asyncio
async def test_cas_tampering_and_workspace_drift_detected(project):
    agent = parent(project, {"i": write_script("x")})
    graph = GraphSpec(nodes=[NodeSpec(id="i", role="implementer", goal="x", write_set=["x"])])
    result = await DAGRunner(agent).run(graph)
    assert result.status == "success"
    (project / "x").write_text("external edit")
    with pytest.raises(DriftError):
        await DAGRunner(agent).run(graph, run_id=result.run_id, resume=True)
    (project / "x").write_text("new")
    artifact = result.nodes["i"].output.evidence[0]
    (project / ".eviforge/dag/artifacts" / artifact.sha256).write_text("tampered")
    with pytest.raises(DAGError, match="integrity"):
        await DAGRunner(agent).run(graph, run_id=result.run_id, resume=True)


@pytest.mark.asyncio
async def test_independent_read_nodes_run_concurrently(project):
    arrivals = set()
    both = asyncio.Event()
    async def rendezvous(conv, prompt):
        arrivals.add(prompt["node_id"])
        if len(arrivals) == 2:
            both.set()
        await asyncio.wait_for(both.wait(), 2)
        return submit()
    agent = parent(project, {"a": [rendezvous], "b": [rendezvous]})
    graph = GraphSpec(max_concurrency=2, nodes=[NodeSpec(id=id, role="explorer", goal="x") for id in ("a", "b")])
    result = await DAGRunner(agent).run(graph)
    assert result.status == "success", result.model_dump()


@pytest.mark.asyncio
async def test_conflicting_writers_are_serialized(project):
    agent = parent(project, {"a": write_script("x"), "b": [("ReadFile", {"file_path": "x"}),
        ("EditFile", {"file_path": "x", "old_string": "new", "new_string": "second"}),
        ("CaptureArtifact", {"file_path": "x"}), lambda conv, p: submit("implementer", changes=["x"], evidence=captured(conv))]})
    graph = GraphSpec(max_concurrency=2, nodes=[NodeSpec(id=id, role="implementer", goal="x", write_set=["x"]) for id in ("a", "b")])
    result = await DAGRunner(agent).run(graph)
    assert result.status == "success", result.model_dump()
    journal = SQLiteJournal(project / ".eviforge/dag/journal.sqlite3")
    events = journal.events(result.run_id)
    journal.close()
    end_a = next(e["seq"] for e in events if e["node_id"] == "a" and e["type"] == "node_completed")
    start_b = next(e["seq"] for e in events if e["node_id"] == "b" and e["type"] == "node_started")
    assert end_a < start_b
    # Both historical versions remain verifiable even though b changed a's file.
    resumed = await DAGRunner(agent).run(graph, run_id=result.run_id, resume=True)
    assert resumed.status == "success"


@pytest.mark.asyncio
async def test_exact_verification_argv_runs_actual_subprocess(project):
    from eviforge.dag.models import CommandSpec
    argv = [sys.executable, "-c", "print('verified')"]
    agent = parent(project, {"i": [submit("implementer")], "v": [("Bash", {"argv": argv, "timeout": 5}),
        submit("verifier", implementation_refs=["i"], verdict="pass",
               checks=[{"name": "command", "passed": True, "details": "verified"}])]})
    graph = GraphSpec(nodes=[NodeSpec(id="i", role="implementer", goal="x"),
        NodeSpec(id="v", role="verifier", goal="x", depends_on=["i"], inputs=[InputRef(node_id="i", role="implementer")],
                 commands=[CommandSpec(argv=argv, timeout=5)])])
    result = await DAGRunner(agent).run(graph)
    assert result.status == "success", result.model_dump()
    refs = result.nodes["v"].output.evidence
    assert len(refs) == 1
    receipt = json.loads((project / ".eviforge/dag/artifacts" / refs[0].sha256).read_text())
    assert receipt["argv"] == argv and "verified" in receipt["output"] and receipt["is_error"] is False


def test_sqlite_owner_and_fence(project):
    path = project / "journal.sqlite3"
    first, second = SQLiteJournal(path), SQLiteJournal(path)
    first.begin_run("run", "graph", "caps", ["e"])
    with pytest.raises(DAGError, match="live owner"):
        second.begin_run("run", "graph", "caps", ["e"], resume=True)
    first.finish("interrupted")
    second.begin_run("run", "graph", "caps", ["e"], resume=True)
    with pytest.raises(DAGError, match="Stale"):
        first.append_event("e", 1, "bad", {})
    second.close()
    first.close()


@pytest.mark.asyncio
async def test_offline_cli_does_not_load_config(project, capsys, monkeypatch):
    from eviforge.dag.cli import register_parser, handle
    def forbidden(*args, **kwargs):
        raise AssertionError("Offline commands must not load Provider configuration")
    monkeypatch.setattr("eviforge.config.load_config", forbidden)
    parser = argparse.ArgumentParser()
    register_parser(parser.add_subparsers(dest="command"))
    assert await handle(parser.parse_args(["dag", "schema"])) == 0
    schema = json.loads(capsys.readouterr().out)
    assert "graph" in schema and "node_output" in schema
    path = project / "graph.json"
    path.write_text(GraphSpec(nodes=[NodeSpec(id="a", role="explorer", goal="x")]).model_dump_json())
    assert await handle(parser.parse_args(["dag", "validate", str(path)])) == 0
    assert json.loads(capsys.readouterr().out)["order"] == ["a"]


@pytest.mark.asyncio
async def test_explicit_replay_authorization_is_audited(project):
    agent = parent(project, {"i": ["missing submission"]})
    graph = GraphSpec(nodes=[NodeSpec(id="i", role="implementer", goal="x", write_set=["x"])])
    result = await DAGRunner(agent).run(graph)
    assert result.status == "ambiguous"
    agent.client.scripts["i"] = [submit("implementer")]
    agent.client.calls.clear()
    resumed = await DAGRunner(agent).run(graph, run_id=result.run_id, resume=True,
                                         replay_nodes={"i"}, replay_reason="Inspected workspace; retry authorized")
    assert resumed.status == "success"
    journal = SQLiteJournal(project / ".eviforge/dag/journal.sqlite3")
    events = journal.events(result.run_id)
    journal.close()
    assert sum(e["type"] == "node_started" for e in events) == 2
    assert sum(e["type"] == "replay_authorized" for e in events) == 1
    with pytest.raises(ReplayRequired, match="Completed"):
        await DAGRunner(agent).run(graph, run_id=result.run_id, resume=True,
                                  replay_nodes={"i"}, replay_reason="retry")


@pytest.mark.asyncio
async def test_cancellation_reaps_node_tasks_and_persists_checkpoint(project):
    started = asyncio.Event()
    finalized = asyncio.Event()
    async def block(conv, prompt):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            finalized.set()
    agent = parent(project, {"e": [block]})
    graph = GraphSpec(nodes=[NodeSpec(id="e", role="explorer", goal="x")])
    task = asyncio.create_task(DAGRunner(agent).run(graph, run_id="cancel-test"))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finalized.is_set()
    assert not [t for t in asyncio.all_tasks() if t.get_name().startswith("dag:") and not t.done()]
    db = sqlite3.connect(project / ".eviforge/dag/journal.sqlite3")
    assert db.execute("SELECT status FROM nodes WHERE id='e'").fetchone()[0] == "cancelled"
    db.close()


@pytest.mark.asyncio
async def test_forged_artifact_and_untyped_completion_fail(project):
    forged = {"sha256": "0" * 64, "size": 1, "path": "x", "run_id": "fake", "node_id": "e", "attempt": 1}
    agent = parent(project, {"e": [submit(evidence=[forged])]})
    result = await DAGRunner(agent).run(GraphSpec(nodes=[NodeSpec(id="e", role="explorer", goal="x")]))
    assert result.status == "failed"
    assert result.nodes["e"].output is None


@pytest.mark.asyncio
async def test_verifier_cannot_claim_pass_without_inspection(project):
    agent = parent(project, {"i": [submit("implementer")], "v": [submit("verifier", implementation_refs=["i"],
        verdict="pass", checks=[{"name": "invented", "passed": True, "details": "claim"}])]})
    graph = GraphSpec(nodes=[NodeSpec(id="i", role="implementer", goal="x"),
        NodeSpec(id="v", role="verifier", goal="x", depends_on=["i"], inputs=[InputRef(node_id="i", role="implementer")])])
    result = await DAGRunner(agent).run(graph)
    assert result.status == "failed"
    assert result.nodes["v"].status == "failed"


@pytest.mark.asyncio
async def test_modified_argv_and_shell_string_cannot_execute(project):
    from eviforge.dag.models import CommandSpec
    argv = [sys.executable, "-c", "print('approved')"]
    forbidden = [sys.executable, "-c", "from pathlib import Path; Path('escaped').write_text('bad')"]
    agent = parent(project, {"i": [submit("implementer")], "v": [("Bash", {"argv": forbidden, "timeout": 5}),
        ("Bash", {"command": "touch escaped", "timeout": 5})]})
    graph = GraphSpec(nodes=[NodeSpec(id="i", role="implementer", goal="x"),
        NodeSpec(id="v", role="verifier", goal="x", depends_on=["i"], inputs=[InputRef(node_id="i", role="implementer")],
                 commands=[CommandSpec(argv=argv, timeout=5)])])
    result = await DAGRunner(agent).run(graph)
    assert result.status == "ambiguous"
    assert not (project / "escaped").exists()


def test_capability_hash_reads_effective_rules_and_actual_schemas(project):
    from eviforge.permissions.rules import Rule
    agent = parent(project, {})
    nodes = [NodeSpec(id="e", role="explorer", goal="x")]
    before = capability_hash(agent, nodes)
    agent.permission_checker.mode = PermissionMode.DEFAULT
    assert capability_hash(agent, nodes) != before
    agent.permission_checker.mode = PermissionMode.DONT_ASK
    agent.permission_checker.rule_engine._inherited_denials = (Rule("ReadFile", "*", "deny"),)
    assert capability_hash(agent, nodes) != before
    agent.permission_checker.rule_engine._inherited_denials = ()
    agent.registry.get("ReadFile").description = "changed actual schema"
    assert capability_hash(agent, nodes) != before


def test_case_variant_root_does_not_authorize_a_linux_sibling(project):
    from eviforge.dag.capabilities import scoped_path
    if sys.platform == "win32":
        pytest.skip("Case-sensitive filesystem regression")
    sibling = project.parent / "PROJECT"
    sibling.mkdir()
    with pytest.raises(DAGError, match="escapes"):
        scoped_path(str(sibling / "file"), ["."], project)


@pytest.mark.asyncio
async def test_real_plan_service_parent_grant_is_not_copied_to_dag_child(project):
    from eviforge.planning import PlanService
    agent = parent(project, {"i": [("WriteFile", {"file_path": "out.txt", "content": "approved"})]})
    agent.session_id, agent.turn_id = "session", "turn"
    service = PlanService(project)
    service.bind_agent(agent)
    plan = service.create("session", "turn", "write one file", [
        {"tool_name": "WriteFile", "arguments": {"file_path": "out.txt", "content": "approved"}}])
    plan = service.submit(plan.plan_id)
    plan = service.approve(plan.plan_id, plan.content_hash, session_id="session", source_turn_id="turn",
                           execution_turn_id="turn", agent_id=agent.agent_id)
    service.activate(agent, plan.plan_id, plan.content_hash)
    result = await DAGRunner(agent).run(GraphSpec(nodes=[NodeSpec(id="i", role="implementer", goal="x", write_set=["out.txt"])]))
    assert result.status == "ambiguous"
    assert not (project / "out.txt").exists()
    # The parent grant remains unspent; no same-name wrapper can steal it.
    assert service.precheck(agent, agent.registry.get("WriteFile"),
                            {"file_path": "out.txt", "content": "approved"}, str(project)).allowed


@pytest.mark.asyncio
async def test_cli_exports_versioned_jsonl_without_provider(project, capsys):
    from eviforge.dag.cli import register_parser, handle
    agent = parent(project, {"e": [submit()]})
    result = await DAGRunner(agent).run(GraphSpec(nodes=[NodeSpec(id="e", role="explorer", goal="x")]))
    parser = argparse.ArgumentParser()
    register_parser(parser.add_subparsers(dest="command"))
    assert await handle(parser.parse_args(["dag", "events", result.run_id, "--work-dir", str(project)])) == 0
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events and all(event["schema_version"] == "1.0" and event["run_id"] == result.run_id for event in events)
    assert isinstance(events[-1]["data"], dict)


@pytest.mark.asyncio
@pytest.mark.parametrize("target,accepted", [("allowed.txt", True), ("other.txt", False)])
async def test_real_plan_capture_intersects_plan_and_dag_scopes(project, target, accepted):
    from eviforge.planning import PlanService
    (project / "allowed.txt").write_text("allowed")
    (project / "other.txt").write_text("not approved")
    script = [("CaptureArtifact", {"file_path": target}),
              lambda conv, p: submit(evidence=captured(conv))] if accepted else [("CaptureArtifact", {"file_path": target})]
    agent = parent(project, {"e": script})
    agent.session_id, agent.turn_id = "session", "turn"
    service = PlanService(project)
    service.bind_agent(agent)
    plan = service.create("session", "turn", "inspect approved file", [
        {"tool_name": "ReadFile", "arguments": {"file_path": "allowed.txt"}}])
    plan = service.submit(plan.plan_id)
    plan = service.approve(plan.plan_id, plan.content_hash, session_id="session", source_turn_id="turn",
                          execution_turn_id="turn", agent_id=agent.agent_id)
    service.activate(agent, plan.plan_id, plan.content_hash)
    result = await DAGRunner(agent).run(GraphSpec(nodes=[NodeSpec(id="e", role="explorer", goal="x")]))
    assert result.status == ("success" if accepted else "failed"), result.model_dump()
    if not accepted:
        db = sqlite3.connect(project / ".eviforge/dag/journal.sqlite3")
        assert db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0
        db.close()


def test_role_specific_input_schemas_reject_wrong_nested_results():
    from eviforge.dag.models import VerifierInput, ExplorerOutput
    with pytest.raises(ValidationError):
        VerifierInput(role="verifier", goal="x", implementations={
            "wrong": ExplorerOutput(role="explorer", summary="x", findings=[])})


@pytest.mark.asyncio
async def test_disjoint_writers_really_execute_concurrently(project):
    arrivals = set()
    ready = asyncio.Event()
    async def begin(conv, prompt):
        key = prompt["node_id"]
        arrivals.add(key)
        if len(arrivals) == 2:
            ready.set()
        await asyncio.wait_for(ready.wait(), 2)
        return ("WriteFile", {"file_path": key, "content": key})
    scripts = {key: [begin, ("CaptureArtifact", {"file_path": key}),
                     lambda conv, p: submit("implementer", changes=[p["node_id"]], evidence=captured(conv))]
               for key in ("a", "b")}
    agent = parent(project, scripts)
    graph = GraphSpec(nodes=[NodeSpec(id=key, role="implementer", goal="x", read_set=[key], write_set=[key])
                             for key in ("a", "b")])
    result = await DAGRunner(agent).run(graph)
    assert result.status == "success", result.model_dump()
    assert (project / "a").read_text() == "a" and (project / "b").read_text() == "b"


@pytest.mark.asyncio
async def test_evidence_capture_cannot_bypass_parent_read_denial(project):
    from eviforge.permissions.rules import Rule
    (project / "denied").write_text("private test fixture")
    agent = parent(project, {"e": [("CaptureArtifact", {"file_path": "denied"})]})
    agent.permission_checker.rule_engine._inherited_denials = (Rule("ReadFile", "denied", "deny"),)
    result = await DAGRunner(agent).run(GraphSpec(nodes=[NodeSpec(id="e", role="explorer", goal="x")]))
    assert result.status == "failed"
    db = sqlite3.connect(project / ".eviforge/dag/journal.sqlite3")
    assert db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0
    db.close()


@pytest.mark.asyncio
async def test_failed_verification_is_not_a_successful_run(project):
    (project / "inspect").write_text("needs work")
    agent = parent(project, {"i": [submit("implementer")], "v": [
        ("ReadFile", {"file_path": "inspect"}),
        submit("verifier", implementation_refs=["i"], verdict="fail",
               checks=[{"name": "content", "passed": False, "details": "needs work"}])]})
    graph = GraphSpec(nodes=[NodeSpec(id="i", role="implementer", goal="x"),
        NodeSpec(id="v", role="verifier", goal="x", depends_on=["i"], inputs=[InputRef(node_id="i", role="implementer")])])
    result = await DAGRunner(agent).run(graph)
    assert result.status == "failed" and result.nodes["v"].status == "completed"
    calls = agent.client.calls.copy()
    resumed = await DAGRunner(agent).run(graph, run_id=result.run_id, resume=True)
    assert resumed.status == "failed" and calls == agent.client.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("role,steps,expected", [("explorer", [submit()], 0), ("implementer", ["no submission"], 4)])
async def test_cli_run_uses_real_runtime_and_common_exit_codes(project, monkeypatch, capsys, role, steps, expected):
    from eviforge.dag.cli import register_parser, handle
    config = project / "offline.yaml"
    config.write_text("providers:\n  - name: offline\n    protocol: anthropic\n    base_url: http://unused.invalid\n    model: offline\n    api_key: test-only\n")
    graph_path = project / "graph.json"
    graph_path.write_text(GraphSpec(nodes=[NodeSpec(id="node", role=role, goal="x",
                                                   write_set=["out"] if role == "implementer" else [])]).model_dump_json())
    client = ScriptedClient({"node": steps})
    monkeypatch.setattr("eviforge.client.create_client", lambda provider: client)
    parser = argparse.ArgumentParser()
    register_parser(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["dag", "run", str(graph_path), "--config", str(config), "--work-dir", str(project), "--run-id", "cli-run"])
    assert await handle(args) == expected
    result = json.loads(capsys.readouterr().out)
    assert result["run_id"] == "cli-run" and result["schema_version"] == "1.0"
    assert result["status"] == ("success" if expected == 0 else "ambiguous")
    assert client.calls["node"] > 0


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["WriteFile", "EditFile"])
async def test_journal_failure_after_actual_write_cannot_hide_the_mutation(project, monkeypatch, tool_name):
    original = SQLiteJournal._event
    failed = False
    def fail_once(self, node, attempt, kind, data):
        nonlocal failed
        if not failed and kind == "tool_finished" and data.get("tool") == tool_name:
            failed = True
            raise sqlite3.OperationalError("injected write-completion journal failure")
        return original(self, node, attempt, kind, data)
    monkeypatch.setattr(SQLiteJournal, "_event", fail_once)
    if tool_name == "EditFile":
        (project / "out").write_text("before")
        steps = [("ReadFile", {"file_path": "out"}),
                 ("EditFile", {"file_path": "out", "old_string": "before", "new_string": "after"})]
    else:
        steps = [("WriteFile", {"file_path": "out", "content": "after"})]
    steps.append(submit("implementer", changes=[]))
    agent = parent(project, {"i": steps})
    graph = GraphSpec(nodes=[NodeSpec(id="i", role="implementer", goal="x", write_set=["out"])])
    result = await DAGRunner(agent).run(graph)
    assert failed and (project / "out").read_text() == "after"
    assert result.status == "ambiguous", result.model_dump()
    assert result.nodes["i"].output is None
    db = sqlite3.connect(project / ".eviforge/dag/journal.sqlite3")
    assert db.execute("SELECT status FROM nodes WHERE id='i'").fetchone()[0] == "ambiguous"
    assert db.execute("SELECT COUNT(*) FROM events WHERE type='node_completed'").fetchone()[0] == 0
    db.close()
    with pytest.raises(ReplayRequired):
        await DAGRunner(agent).run(graph, run_id=result.run_id, resume=True)


@pytest.mark.asyncio
async def test_journal_failure_after_result_submission_cannot_complete(project, monkeypatch):
    original = SQLiteJournal._event
    failed = False
    def fail_once(self, node, attempt, kind, data):
        nonlocal failed
        if not failed and kind == "result_submitted":
            failed = True
            raise sqlite3.OperationalError("injected result-submission journal failure")
        return original(self, node, attempt, kind, data)
    monkeypatch.setattr(SQLiteJournal, "_event", fail_once)
    agent = parent(project, {"i": write_script("out")})
    result = await DAGRunner(agent).run(GraphSpec(nodes=[NodeSpec(id="i", role="implementer", goal="x", write_set=["out"])]))
    assert failed and (project / "out").read_text() == "new"
    assert result.status == "ambiguous", result.model_dump()
    assert result.nodes["i"].output is None


@pytest.mark.asyncio
async def test_exception_after_real_side_effect_blocks_further_mutations(project, monkeypatch):
    from eviforge.tools.write_file import WriteFile
    original = WriteFile.execute
    invocations = 0
    async def write_then_fail(self, params):
        nonlocal invocations
        invocations += 1
        result = await original(self, params)
        assert not result.is_error
        raise OSError("injected failure after the file was written")
    monkeypatch.setattr(WriteFile, "execute", write_then_fail)
    agent = parent(project, {"i": [("WriteFile", {"file_path": "first", "content": "written"}),
        ("WriteFile", {"file_path": "second", "content": "must not run"}), submit("implementer", changes=[])]})
    result = await DAGRunner(agent).run(GraphSpec(nodes=[NodeSpec(id="i", role="implementer", goal="x", write_set=["first", "second"])]))
    assert (project / "first").read_text() == "written"
    assert not (project / "second").exists() and invocations == 1
    assert result.status == "ambiguous" and result.nodes["i"].output is None

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor

import httpx
from pydantic import ValidationError
import pytest

from eviforge.permissions.capabilities import normalize_action
from eviforge.permissions.rules import extract_content
from eviforge.planning import PlanError, PlanService, PlanState
from eviforge.tools.bash import Bash, Params as BashParams
from eviforge.tools.http_request import HttpRequest, HttpRequestParams
from eviforge.tools.read_file import ReadFile
from eviforge.tools.write_file import WriteFile
from eviforge.tools.work_dir import tool_working_directory


def actor(**changes):
    return SimpleNamespace(**({"agent_id": "a", "session_id": "s", "turn_id": "t"} | changes))


def approved(tmp_path, *, actions=None, clock=lambda: 100.0, ttl=300, plan_path=None):
    service = PlanService(tmp_path, clock=clock)
    agent = actor()
    service.bind_agent(agent)
    args = {"file_path": "out.txt", "content": "approved"}
    actions = actions if actions is not None else [{"tool_name": "WriteFile", "arguments": args}]
    plan = service.create("s", "t", "Write the approved result", actions, plan_path=plan_path)
    plan = service.submit(plan.plan_id)
    plan = service.approve(plan.plan_id, plan.content_hash, session_id="s", source_turn_id="t",
                           execution_turn_id="t", agent_id="a", ttl_seconds=ttl)
    service.activate(agent, plan.plan_id, plan.content_hash)
    return service, agent, plan, args


def test_exact_args_cwd_and_single_use(tmp_path):
    service, agent, plan, args = approved(tmp_path)
    tool = WriteFile()
    assert not service.precheck(agent, tool, args | {"content": "changed"}, str(tmp_path)).allowed
    other = tmp_path / "other"
    other.mkdir()
    assert not service.consume(agent, tool, args, str(other)).allowed
    assert service.precheck(agent, tool, args, str(tmp_path)).allowed
    assert service.consume(agent, tool, args, str(tmp_path)).allowed
    assert not service.consume(agent, tool, args, str(tmp_path)).allowed
    assert service.get(plan.plan_id).state == PlanState.EXECUTING
    audit = [json.loads(line) for line in (tmp_path / ".eviforge/plans/audit.jsonl").read_text().splitlines()]
    assert any(item["event"] == "tool_denied" and item["stage"] == "precheck" for item in audit)
    assert sum(item["event"] == "grant_consumed" for item in audit) == 1


@pytest.mark.parametrize("field,value,code", [("session_id", "other", "SESSION_MISMATCH"), ("turn_id", "other", "TURN_MISMATCH")])
def test_session_and_turn_replay(tmp_path, field, value, code):
    service, agent, _, args = approved(tmp_path)
    setattr(agent, field, value)
    assert service.consume(agent, WriteFile(), args, str(tmp_path)).code == code


def test_changing_agent_trace_id_cannot_remove_plan_gate(tmp_path):
    service, agent, plan, args = approved(tmp_path)
    agent.agent_id = "new-trace-label"
    assert service.current_plan(agent).plan_id == plan.plan_id
    decision = service.consume(agent, WriteFile(), args, str(tmp_path))
    assert decision is not None and not decision.allowed


def test_new_turn_revokes_and_child_does_not_inherit_grants(tmp_path):
    service, agent, plan, args = approved(tmp_path)
    child = actor(agent_id="child", session_id="different")
    service.inherit(agent, child)
    assert child.session_id == "s"
    assert service.current_plan(child).plan_id == plan.plan_id
    assert not service.consume(child, WriteFile(), args, str(tmp_path)).allowed
    service.begin_turn(agent, turn_id="new")
    assert service.get(plan.plan_id).state == PlanState.INVALIDATED
    assert not service.consume(agent, WriteFile(), args, str(tmp_path)).allowed
    assert not service.consume(child, WriteFile(), args, str(tmp_path)).allowed


def test_expiry_is_checked_after_precheck(tmp_path):
    time = [100.0]
    service, agent, plan, args = approved(tmp_path, clock=lambda: time[0], ttl=5)
    assert service.precheck(agent, WriteFile(), args, str(tmp_path)).allowed
    time[0] = 105.0
    assert service.consume(agent, WriteFile(), args, str(tmp_path)).code == "APPROVAL_EXPIRED"
    assert service.get(plan.plan_id).state == PlanState.EXPIRED


def test_plan_file_changed_while_user_reviewed(tmp_path):
    path = tmp_path / "plan.md"
    path.write_text("original", encoding="utf-8")
    service = PlanService(tmp_path)
    plan = service.create("s", "t", "original", plan_path=path)
    plan = service.submit(plan.plan_id)
    path.write_text("changed after display", encoding="utf-8")
    with pytest.raises(PlanError, match="PLAN_CHANGED"):
        service.approve(plan.plan_id, plan.content_hash, session_id="s", source_turn_id="t", execution_turn_id="t", agent_id="a")
    assert service.get(plan.plan_id).state == PlanState.INVALIDATED


def test_plan_change_before_execution_and_scope_hash(tmp_path):
    path = tmp_path / "plan.md"
    path.write_text("Write the approved result", encoding="utf-8")
    service, agent, plan, args = approved(tmp_path, plan_path=path)
    path.write_text("different", encoding="utf-8")
    assert service.consume(agent, WriteFile(), args, str(tmp_path)).code == "PLAN_CHANGED"
    updated = service.update(plan.plan_id, plan.content, [{"tool_name": "WriteFile", "arguments": args, "write_paths": [str(tmp_path)]}])
    assert updated.content_hash != plan.content_hash
    assert updated.version == plan.version + 1
    assert updated.state == PlanState.DRAFT


def test_grant_not_restored_from_snapshot(tmp_path):
    service, agent, plan, args = approved(tmp_path)
    restored = PlanService(tmp_path, clock=lambda: 100.0)
    restored.activate(agent, plan.plan_id, plan.content_hash)
    assert restored.get(plan.plan_id).state == PlanState.APPROVED
    assert not restored.consume(agent, WriteFile(), args, str(tmp_path)).allowed


def test_parallel_calls_cannot_spend_same_grant(tmp_path):
    service, agent, _, args = approved(tmp_path)
    with ThreadPoolExecutor(max_workers=8) as pool:
        decisions = list(pool.map(lambda _: service.consume(agent, WriteFile(), args, str(tmp_path)), range(8)))
    assert sum(decision.allowed for decision in decisions) == 1


def test_plan_default_deny_and_draft_write_only(tmp_path):
    service, agent = PlanService(tmp_path), actor()
    path = tmp_path / "plan.md"
    plan = service.create("s", "t", "draft", plan_path=path)
    service.bind_draft(agent, plan.plan_id)
    assert service.consume(agent, WriteFile(), {"file_path": "plan.md", "content": "plan"}, str(tmp_path)).allowed
    assert not service.consume(agent, WriteFile(), {"file_path": "out.txt", "content": "no"}, str(tmp_path)).allowed
    assert not service.consume(agent, Bash(), {"argv": ["echo", "hi"]}, str(tmp_path)).allowed
    assert service.consume(agent, ReadFile(), {"file_path": "source.py"}, str(tmp_path)).allowed


def test_unsupported_tool_cannot_claim_read_or_spoof_name(tmp_path):
    service, agent, _, _ = approved(tmp_path)
    class Spoof(ReadFile):
        pass
    assert not service.consume(agent, Spoof(), {"file_path": "out.txt"}, str(tmp_path)).allowed
    unknown = SimpleNamespace(name="mcp_external_tool", category="read")
    assert not service.consume(agent, unknown, {}, str(tmp_path)).allowed


def test_path_prefix_and_symlink_change_denied(tmp_path):
    inside, sibling = tmp_path / "src", tmp_path / "src-other"
    inside.mkdir()
    sibling.mkdir()
    with pytest.raises(ValueError, match="OUTSIDE_SCOPE"):
        normalize_action({"tool_name": "WriteFile", "arguments": {"file_path": "src-other/out", "content": "x"}, "write_paths": ["src"]}, str(tmp_path))
    link = tmp_path / "link"
    try:
        link.symlink_to(inside, target_is_directory=True)
    except OSError:
        pytest.skip("Symlinks unavailable")
    args = {"file_path": "link/out", "content": "x"}
    service, agent, _, _ = approved(tmp_path, actions=[{"tool_name": "WriteFile", "arguments": args}])
    assert service.precheck(agent, WriteFile(), args, str(tmp_path)).allowed
    link.unlink()
    link.symlink_to(sibling, target_is_directory=True)
    assert not service.consume(agent, WriteFile(), args, str(tmp_path)).allowed


def test_control_records_cannot_be_written_without_active_plan(tmp_path):
    service, agent = PlanService(tmp_path), actor()
    service.bind_agent(agent)
    assert service.consume(agent, WriteFile(), {"file_path": ".eviforge/plans/fake.json", "content": "{}"}, str(tmp_path)).code == "CONTROL_PATH_DENIED"
    assert service.consume(agent, WriteFile(), {"file_path": "ordinary.txt", "content": "ok"}, str(tmp_path)) is None
    for name in ("governance.sqlite3", "governance.sqlite3-wal", "governance.sqlite3-shm", "dag/journal.sqlite3", "dag/evidence/hash"):
        assert service.consume(agent, WriteFile(), {"file_path": ".eviforge/" + name, "content": "tamper"}, str(tmp_path)).code == "CONTROL_PATH_DENIED"


def test_snapshot_id_traversal_and_tampering_rejected(tmp_path):
    service, _, plan, _ = approved(tmp_path)
    with pytest.raises(PlanError, match="INVALID_PLAN_ID"):
        service.get("../../outside")
    path = tmp_path / ".eviforge" / "plans" / f"{plan.plan_id}.json"
    raw = json.loads(path.read_text())
    raw["content"] = "tampered"
    path.write_text(json.dumps(raw))
    with pytest.raises(PlanError, match="hash"):
        PlanService(tmp_path).get(plan.plan_id)


def test_argv_exact_and_legacy_rule_mapping(tmp_path):
    args = {"argv": [sys.executable, "-c", "print('a b')"]}
    service, agent, _, _ = approved(tmp_path, actions=[{"tool_name": "Bash", "arguments": args}])
    assert service.precheck(agent, Bash(), args, str(tmp_path)).allowed
    assert not service.consume(agent, Bash(), {"argv": args["argv"] + ["extra"]}, str(tmp_path)).allowed
    assert not service.consume(agent, Bash(), {"command": "echo unscoped"}, str(tmp_path)).allowed
    assert extract_content("Bash", {"argv": ["git", "push", "--force"]}) == "git push --force"
    with pytest.raises(ValidationError):
        BashParams(command="echo x", argv=["echo", "y"])


@pytest.mark.asyncio
async def test_actual_argv_does_not_invoke_shell(tmp_path):
    with tool_working_directory(tmp_path):
        result = await Bash().execute(BashParams(argv=[sys.executable, "-c", "import sys; print(sys.argv[1])", "$(touch injected); echo danger"]))
    assert not result.is_error
    assert "$(touch injected); echo danger" in result.output
    assert not (tmp_path / "injected").exists()


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group verification")
async def test_bash_cancellation_kills_and_reaps_process_tree(tmp_path):
    pidfile = tmp_path / "pids"
    code = "import os,subprocess,sys,time,pathlib; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); pathlib.Path(sys.argv[1]).write_text(str(os.getpid())+' '+str(p.pid)); time.sleep(60)"
    task = asyncio.create_task(Bash().execute(BashParams(argv=[sys.executable, "-c", code, str(pidfile)])))
    for _ in range(200):
        if pidfile.exists():
            break
        await asyncio.sleep(0.01)
    assert pidfile.exists()
    parent, child = map(int, pidfile.read_text().split())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not Path(f"/proc/{parent}").exists(), "direct child must be reaped"
    status = Path(f"/proc/{child}/stat")
    assert not status.exists() or status.read_text().split()[2] == "Z", "descendant must not remain running"


@pytest.mark.asyncio
async def test_http_redirect_is_checked_before_second_request():
    seen = []
    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(302, headers={"Location": "https://blocked.example/private"})
    tool = HttpRequest(transport=httpx.MockTransport(handler))
    result = await tool.execute(HttpRequestParams(url="https://allowed.example/start", allowed_hosts=["allowed.example"]))
    assert result.is_error and "NETWORK_SCOPE_DENIED" in result.output
    assert seen == ["https://allowed.example/start"]


@pytest.mark.asyncio
async def test_http_allowed_cross_origin_strips_credentials_and_limits_body():
    seen = []
    def handler(request):
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(302, headers={"Location": "https://second.example/end", "Set-Cookie": "secret=cookie; Domain=first.example"})
        return httpx.Response(200, content=b"0123456789")
    tool = HttpRequest(transport=httpx.MockTransport(handler))
    result = await tool.execute(HttpRequestParams(url="https://first.example/start", allowed_hosts=["first.example", "second.example"], headers={"Authorization": "secret", "Cookie": "token=secret"}, max_bytes=4))
    assert not result.is_error
    assert "authorization" not in seen[1].headers and "cookie" not in seen[1].headers
    output = json.loads(result.output)
    assert output["body"] == "0123" and output["truncated"]


def test_http_plan_host_scope_cannot_be_widened(tmp_path):
    args = {"url": "https://example.com/", "allowed_hosts": ["example.com"]}
    service, agent, _, _ = approved(tmp_path, actions=[{"tool_name": "HttpRequest", "arguments": args}])
    assert service.precheck(agent, HttpRequest(), args, str(tmp_path)).allowed
    assert not service.consume(agent, HttpRequest(), args | {"allowed_hosts": ["example.com", "evil.example"]}, str(tmp_path)).allowed
    with pytest.raises(ValidationError):
        HttpRequestParams(url="https://example.com", allowed_hosts=["*.example.com"])
    with pytest.raises(ValidationError):
        HttpRequestParams(url="https://example.com", allowed_hosts=["example.com"], headers={"Host": "evil.example"})


@pytest.mark.asyncio
async def test_draft_explore_delegation_uses_real_child_and_stays_readonly(tmp_path, monkeypatch):
    from eviforge.client import LLMClient
    from eviforge.config import AppConfig, ProviderConfig
    from eviforge.permissions import PermissionMode
    from eviforge.runtime import RuntimeServices
    from eviforge.tools.base import StreamEnd, TextDelta, ToolCallComplete

    home, project = tmp_path / "home", tmp_path / "project"
    home.mkdir()
    project.mkdir()
    (project / "source.txt").write_text("read-only evidence", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    class ExplorerClient(LLMClient):
        async def stream(self, conversation, system="", tools=None):
            results = [result for message in conversation.history for result in message.tool_results]
            if not results:
                yield ToolCallComplete("read", "ReadFile", {"file_path": "source.txt"})
            elif len(results) == 1:
                assert "read-only evidence" in results[0].content
                yield ToolCallComplete("write", "WriteFile", {"file_path": "forbidden.txt", "content": "not authorized"})
            else:
                assert results[-1].is_error
                yield TextDelta("exploration complete; write refused")
            yield StreamEnd("end_turn")

    provider = ProviderConfig("offline", "anthropic", "http://unused.invalid", "offline", api_key="test-only", context_window=32000)
    runtime = RuntimeServices.create(AppConfig(providers=[provider]), provider, client=ExplorerClient(),
        work_dir=str(project), permission_mode=PermissionMode.PLAN)
    try:
        runtime.begin_turn()
        tool = runtime.registry.get("Agent")
        args = {"prompt": "Read the source and try an unauthorized write", "description": "read-only exploration", "subagent_type": "Explore", "model": "inherit"}
        assert runtime.plan_service.precheck(runtime.agent, tool, args, str(project)).allowed
        for change in ({"team_name": "team"}, {"isolation": "worktree"}, {"subagent_type": None}, {"subagent_type": "general-purpose"}):
            assert not runtime.plan_service.precheck(runtime.agent, tool, args | change, str(project)).allowed
        result = await runtime.agent._execute_tool_noninteractive(ToolCallComplete("delegate", "Agent", args))
        assert not result.is_error, result.output
        assert "write refused" in result.output
        assert len(runtime.agent.children) == 1
        child = runtime.agent.children[0]
        assert child.permission_mode == PermissionMode.PLAN
        assert runtime.plan_service.current_plan(child).plan_id == runtime.plan_service.current_plan(runtime.agent).plan_id
        assert not runtime.plan_service.consume(child, WriteFile(), {"file_path": str(runtime.agent._get_plan_path()), "content": "child may not alter parent plan"}, str(project)).allowed
        assert not (project / "forbidden.txt").exists()
        assert runtime.plan_service._grants == []
    finally:
        await runtime.close()

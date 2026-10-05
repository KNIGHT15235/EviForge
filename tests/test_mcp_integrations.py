from __future__ import annotations

import base64
import hashlib
import json
from unittest.mock import AsyncMock

import pytest
from mcp import types
from pydantic import ValidationError

from eviforge.config import MCPServerConfig, ConfigError
from eviforge.mcp.client import MCPClient
from eviforge.mcp.manager import MCPManager
from eviforge.mcp.policy import resources, MCPPolicyError
from eviforge.mcp.recovery import reconcile, unresolved_writes
from eviforge.mcp.tool_wrapper import MCPToolWrapper, _build_params_model, provider_name
from eviforge.permissions.capabilities import normalize_action, execution_intent
from eviforge.tools import ToolRegistry
from eviforge.tools.work_dir import tool_working_directory
from eviforge.validator import validate_mcp_servers


def test_schema_nested_constraints_and_lossless_null():
    schema = {"type": "object", "properties": {"payload": {"type": "object", "properties": {"state": {"enum": ["open", "closed"]}, "count": {"type": "integer", "minimum": 1}}, "required": ["state", "count"], "additionalProperties": False}, "nullable": {"type": ["null", "string"]}}, "required": ["payload"], "additionalProperties": True}
    model = _build_params_model("nested", schema)
    args = {"payload": {"state": "open", "count": 3}, "nullable": None, "extra": [1, {"nested": True}]}
    assert model.model_validate(args).model_dump(mode="json") == args
    assert "nullable" not in model(payload=args["payload"]).model_dump()
    for invalid in ({"state": "bad", "count": 1}, {"state": "open", "count": "3"}, {"state": "open", "count": 0}, {"state": "open", "count": 1, "extra": 1}):
        with pytest.raises(ValidationError):
            model(payload=invalid)


def test_schema_combinations_and_local_refs():
    model = _build_params_model("combined", {"$defs": {"value": {"oneOf": [{"type": "string", "minLength": 2}, {"type": "integer"}]}}, "type": "object", "properties": {"value": {"$ref": "#/$defs/value"}}, "required": ["value"]})
    assert model(value=4).value == 4
    with pytest.raises(ValidationError):
        model(value=True)
    with pytest.raises(ValueError, match="External"):
        _build_params_model("remote", {"$ref": "https://example.invalid/schema"})


@pytest.mark.parametrize("extra", [{"args": "npx"}, {"headers": {"Token": 42}}, {"enabled": "false"}, {"call_timeout_seconds": float("inf")}, {"policy": {"allow_writes": "false"}}, {"policy": {"max_artifact_bytes": -1}}, {"policy": {"auth_store": 42}}])
def test_invalid_config_types(extra):
    with pytest.raises(ConfigError):
        validate_mcp_servers([{"name": "test", "command": "python", **extra}])


def test_provider_name_collision_resistance():
    a, b = provider_name("a", "x.y"), provider_name("a", "x_y")
    assert a != b and len(a) <= 64
    assert provider_name("x" * 70, "y") != provider_name("x" * 69, "y")


def test_github_scopes_and_draft_only(tmp_path):
    config = MCPServerConfig("github", integration="github", policy={"repositories": ["owner/test"], "allow_writes": True})
    assert resources(config, "get_file_contents", {"owner": "owner", "repo": "test"}, str(tmp_path)) == ("github:repo:owner/test",)
    for name, args in [("search_issues", {"query": "repo:owner/test OR repo:private/secret"}), ("get_file_contents", {"owner": "private", "repo": "secret"}), ("create_pull_request", {"owner": "owner", "repo": "test", "draft": False}), ("merge_pull_request", {"owner": "owner", "repo": "test"})]:
        with pytest.raises(MCPPolicyError):
            resources(config, name, args, str(tmp_path))


def test_feishu_document_table_chat_scopes(tmp_path):
    config = MCPServerConfig("feishu", integration="feishu", policy={"documents": ["document"], "tables": ["app/table"], "chats": ["chat"], "allow_messages": True, "allow_writes": True})
    assert resources(config, "docx_v1_document_rawContent", {"path": {"document_id": "document"}, "useUAT": True}, str(tmp_path)) == ("feishu:document:document",)
    assert resources(config, "im_v1_message_create", {"data": {"receive_id": "chat"}, "params": {"receive_id_type": "chat_id"}, "useUAT": True}, str(tmp_path)) == ("feishu:chat:chat",)
    for tool, args in [("bitable_v1_appTableRecord_create", {"path": {"app_token": "app", "table_id": "other"}}), ("im_v1_message_create", {"data": {"receive_id": "someone"}, "params": {"receive_id_type": "open_id"}}), ("docx_builtin_search", {"query": "secret"})]:
        with pytest.raises(MCPPolicyError):
            resources(config, tool, args, str(tmp_path))


def test_serena_project_and_playwright_origin(tmp_path):
    serena = MCPServerConfig("serena", integration="serena", cwd=str(tmp_path), policy={"project": str(tmp_path)})
    with pytest.raises(MCPPolicyError):
        resources(serena, "find_symbol", {"relative_path": "../outside.py"}, str(tmp_path))
    with pytest.raises(MCPPolicyError):
        resources(serena, "execute_shell_command", {}, str(tmp_path))
    playwright = MCPServerConfig("browser", integration="playwright", policy={"origins": ["http://127.0.0.1:8080"]})
    assert resources(playwright, "browser_navigate", {"url": "http://127.0.0.1:8080/test"}, str(tmp_path))
    with pytest.raises(MCPPolicyError):
        resources(playwright, "browser_navigate", {"url": "https://private.example/"}, str(tmp_path))
    with pytest.raises(MCPPolicyError):
        resources(playwright, "browser_take_screenshot", {"filename": "../../secret"}, str(tmp_path))


def make_wrapper(config, name, schema=None):
    client = AsyncMock()
    client.is_alive = True
    client.generation = 0
    return MCPToolWrapper(config.name, types.Tool(name=name, inputSchema=schema or {"type": "object"}), client, config=config), client


@pytest.mark.asyncio
async def test_real_image_bytes_and_structured_content(tmp_path):
    wrapper, client = make_wrapper(MCPServerConfig("browser", integration="playwright", policy={"origins": ["http://localhost:8080"]}), "browser_take_screenshot")
    payload = b"actual screenshot pixels"
    client.call_tool.return_value = types.CallToolResult(content=[types.ImageContent(type="image", data=base64.b64encode(payload).decode(), mimeType="image/png")], structuredContent={"verified": True})
    with tool_working_directory(tmp_path):
        result = await wrapper.execute(wrapper.params_model())
    assert result.structured_content == {"verified": True}
    artifact = result.artifacts[0]
    from pathlib import Path
    assert Path(artifact["path"]).read_bytes() == payload
    assert artifact["sha256"] == hashlib.sha256(payload).hexdigest()


@pytest.mark.asyncio
async def test_ambiguous_write_is_not_replayed_and_requires_receipt(tmp_path):
    config = MCPServerConfig("github", integration="github", policy={"repositories": ["owner/test"], "allow_writes": True})
    wrapper, client = make_wrapper(config, "issue_write")
    client.call_tool.side_effect = TimeoutError("Bearer secret must not leak")
    params = wrapper.params_model(owner="owner", repo="test", method="create", title="test")
    with tool_working_directory(tmp_path):
        first = await wrapper.execute(params)
        second = await wrapper.execute(params)
    assert first.execution_status == "ambiguous" and "secret" not in first.output
    assert second.execution_status == "rejected"
    assert client.call_tool.await_count == 1
    directory = tmp_path / ".eviforge/mcp"
    call = unresolved_writes(directory)[0]
    reconcile(directory, call, "not_executed", "operator checked remote Issue list")
    assert not unresolved_writes(directory)


def test_plan_binds_remote_resource_schema_and_config(tmp_path):
    config = MCPServerConfig("github", integration="github", policy={"repositories": ["owner/test"], "allow_writes": True})
    wrapper, _ = make_wrapper(config, "issue_write")
    args = {"owner": "owner", "repo": "test", "method": "create", "title": "test"}
    action = normalize_action({"tool_name": wrapper.name, "arguments": args}, str(tmp_path), lambda _: wrapper)
    assert action.matches(execution_intent(wrapper, args, str(tmp_path)))
    assert not action.matches(execution_intent(wrapper, {**args, "title": "changed"}, str(tmp_path)))
    replacement, _ = make_wrapper(MCPServerConfig("github", integration="github", policy={"repositories": ["owner/test", "owner/other"], "allow_writes": True}), "issue_write")
    assert not action.matches(execution_intent(replacement, args, str(tmp_path)))


@pytest.mark.asyncio
async def test_disabled_server_does_not_connect(monkeypatch):
    constructor = AsyncMock()
    monkeypatch.setattr("eviforge.mcp.manager.MCPClient", constructor)
    manager = MCPManager()
    manager.load_configs([MCPServerConfig("disabled", command="python", enabled=False)])
    assert await manager.register_all_tools(ToolRegistry()) == []
    assert manager.status()[0]["state"] == "disabled"
    constructor.assert_not_called()


@pytest.mark.asyncio
async def test_client_pagination_and_duplicate_cursor():
    client = MCPClient(MCPServerConfig("pages", command="python"))
    client._session = AsyncMock()
    client._session.list_tools.side_effect = [types.ListToolsResult(tools=[types.Tool(name="a", inputSchema={})], nextCursor="next"), types.ListToolsResult(tools=[types.Tool(name="b", inputSchema={})])]
    assert [tool.name for tool in await client.list_tools()] == ["a", "b"]
    client._session.list_tools.side_effect = [types.ListToolsResult(tools=[], nextCursor="same"), types.ListToolsResult(tools=[], nextCursor="same")]
    with pytest.raises(ValueError, match="Repeated"):
        await client.list_tools()


@pytest.mark.asyncio
async def test_playwright_guard_and_transport_share_resolved_cwd(tmp_path, monkeypatch):
    from contextlib import AsyncExitStack, asynccontextmanager
    from pathlib import Path
    monkeypatch.setenv('MCP_BROWSER_PROJECT', str(tmp_path))
    seen = {}
    def guard(config, args, cwd):
        seen['guard_cwd'] = cwd
        return args
    @asynccontextmanager
    async def stdio(params, errlog):
        seen['transport_cwd'] = params.cwd
        yield None, None
    monkeypatch.setattr('eviforge.mcp.browser_guard.guarded_args', guard)
    monkeypatch.setattr('eviforge.mcp.client.stdio_client', stdio)
    client = MCPClient(MCPServerConfig('browser', integration='playwright', command='node', cwd='${MCP_BROWSER_PROJECT}'))
    async with AsyncExitStack() as stack:
        client._stack = stack
        await client._connect_stdio()
    assert seen['guard_cwd'] == tmp_path
    assert Path(seen['transport_cwd']) == tmp_path


@pytest.mark.asyncio
async def test_feishu_business_error_is_not_success(tmp_path):
    wrapper, client = make_wrapper(MCPServerConfig('feishu', integration='feishu', policy={'documents':['doc']}), 'docx.v1.document.rawContent')
    client.call_tool.return_value = types.CallToolResult(content=[types.TextContent(type='text', text='{"code":99991672,"msg":"permission denied"}')])
    with tool_working_directory(tmp_path):
        result = await wrapper.execute(wrapper.validate_arguments({'useUAT':True,'path':{'document_id':'doc'}}))
    assert result.is_error and result.execution_status == 'failed'


def test_feishu_identity_cannot_switch_to_tenant(tmp_path):
    config = MCPServerConfig('feishu', integration='feishu', policy={'documents':['doc']})
    for identity in (None, False):
        with pytest.raises(MCPPolicyError, match='user identity'):
            resources(config, 'docx.v1.document.rawContent', {'path':{'document_id':'doc'},'useUAT':identity}, str(tmp_path))


@pytest.mark.asyncio
async def test_oauth_store_change_revokes_capability(tmp_path):
    store = tmp_path/'encrypted-store'; store.write_bytes(b'old encrypted identity')
    config = MCPServerConfig('feishu', integration='feishu', policy={'documents':['doc'],'auth_store':str(store)})
    wrapper, client = make_wrapper(config,'docx.v1.document.rawContent')
    store.write_bytes(b'new encrypted identity')
    with tool_working_directory(tmp_path):
        result = await wrapper.execute(wrapper.validate_arguments({'useUAT':True,'path':{'document_id':'doc'}}))
    assert result.execution_status == 'rejected'
    client.call_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_environment_endpoint_change_revokes_capability(tmp_path, monkeypatch):
    monkeypatch.setenv('MCP_TEST_ENDPOINT', 'https://original.example/mcp')
    wrapper, client = make_wrapper(MCPServerConfig('github', integration='github', url='${MCP_TEST_ENDPOINT}', policy={'repositories':['owner/test']}), 'get_file_contents')
    monkeypatch.setenv('MCP_TEST_ENDPOINT', 'https://replacement.example/mcp')
    with tool_working_directory(tmp_path):
        result = await wrapper.execute(wrapper.validate_arguments({'owner':'owner', 'repo':'test'}))
    assert result.execution_status == 'rejected'
    client.call_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_structured_output_redacts_declared_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv('TEST_MCP_TOKEN','secret-value-123456')
    wrapper, client = make_wrapper(MCPServerConfig('custom', env={'TOKEN':'${TEST_MCP_TOKEN}'}),'echo')
    client.call_tool.return_value = types.CallToolResult(content=[], structuredContent={'nested':{'token':'secret-value-123456'}})
    with tool_working_directory(tmp_path): result = await wrapper.execute(wrapper.validate_arguments({}))
    assert result.structured_content['nested']['token'] == '[REDACTED]'
    assert 'secret-value' not in result.output


@pytest.mark.asyncio
async def test_receipt_disk_failure_stops_replay(tmp_path, monkeypatch):
    config = MCPServerConfig('github',integration='github',policy={'repositories':['owner/test'],'allow_writes':True})
    wrapper,client=make_wrapper(config,'issue_write')
    client.call_tool.return_value=types.CallToolResult(content=[types.TextContent(type='text',text='created')])
    original=wrapper._event
    def event(directory,event,call_id,**fields):
        if event=='call_finished': raise OSError('disk full')
        return original(directory,event,call_id,**fields)
    monkeypatch.setattr(wrapper,'_event',event)
    args=wrapper.validate_arguments({'owner':'owner','repo':'test','method':'create'})
    with tool_working_directory(tmp_path):
        first=await wrapper.execute(args); second=await wrapper.execute(args)
    assert first.execution_status=='completed_unarchived' and second.execution_status=='rejected'
    assert client.call_tool.await_count==1


@pytest.mark.asyncio
async def test_agent_does_not_continue_after_ambiguous_remote_write(tmp_path):
    from tests.test_document_runtime import ScriptedClient, turn
    from eviforge.agent import Agent
    from eviforge.conversation import ConversationManager
    from eviforge.tools.base import ToolCallComplete, TextDelta
    config=MCPServerConfig('github',integration='github',policy={'repositories':['owner/test'],'allow_writes':True})
    wrapper,remote=make_wrapper(config,'issue_write')
    remote.call_tool.side_effect=TimeoutError()
    registry=ToolRegistry();registry.register(wrapper)
    client=ScriptedClient([turn(ToolCallComplete('call',wrapper.name,{'owner':'owner','repo':'test','method':'create'})),turn(TextDelta('must never retry'))])
    agent=Agent(client,registry,'anthropic',work_dir=str(tmp_path))
    await agent.run_to_completion('create the authorized test Issue',ConversationManager())
    assert agent.last_run_status=='ambiguous' and len(client.requests)==1
    assert remote.call_tool.await_count==1


@pytest.mark.asyncio
async def test_removed_server_unregisters_and_closes():
    manager=MCPManager(); registry=ToolRegistry()
    wrapper,client=make_wrapper(MCPServerConfig('old'),'read')
    registry.register(wrapper)
    manager._registered={'old':[wrapper.name]};manager._clients={'old':client}
    manager.load_configs([])
    await manager.register_all_tools(registry)
    assert registry.get(wrapper.name) is None
    client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_feishu_import_job_failure_is_not_success(tmp_path):
    wrapper, client = make_wrapper(MCPServerConfig('feishu', integration='feishu', policy={'allow_writes':True, 'allow_create_documents':True}), 'docx.builtin.import')
    client.call_tool.return_value = types.CallToolResult(content=[types.TextContent(type='text', text='{"result":{"job_status":3}}')])
    with tool_working_directory(tmp_path):
        result = await wrapper.execute(wrapper.validate_arguments({'useUAT':True, 'data':{'markdown':'test'}}))
    assert result.is_error and result.execution_status == 'ambiguous'


def test_serena_and_cli_resolve_project_environment(tmp_path, monkeypatch):
    from eviforge.mcp.cli import load_servers
    monkeypatch.setenv('MCP_TEST_PROJECT', str(tmp_path))
    config_file = tmp_path/'mcp.yaml'
    config_file.write_text('mcp_servers:\n  - name: serena\n    integration: serena\n    command: serena\n    cwd: ${MCP_TEST_PROJECT}\n    policy:\n      project: ${MCP_TEST_PROJECT}\n', encoding='utf-8')
    config = load_servers(str(config_file), str(tmp_path))[0]
    assert config.cwd == str(tmp_path.resolve())
    assert resources(config, 'read_file', {'relative_path':'test.py'}, str(tmp_path))


@pytest.mark.asyncio
async def test_public_profiles_are_disabled_without_credentials():
    from eviforge.mcp.cli import load_servers
    from pathlib import Path
    profiles = load_servers(str(Path(__file__).resolve().parents[1]/'integrations/mcp.example.yaml'))
    assert {p.integration for p in profiles} == {'github','context7','playwright','feishu','serena'}
    assert all(not p.enabled for p in profiles)
    manager = MCPManager(); manager.load_configs(profiles)
    try:
        assert await manager.register_all_tools(ToolRegistry()) == []
        assert not manager.required_failures
    finally: await manager.shutdown()


@pytest.mark.asyncio
async def test_plan_cli_binds_real_wrapper_without_provider(tmp_path, monkeypatch, capsys):
    from argparse import Namespace
    from eviforge.planning.cli import handle_connected
    from eviforge.planning import PlanService
    config = MCPServerConfig('github', integration='github', command='unused', policy={'repositories':['owner/test'], 'allow_writes':True})
    wrapper, remote = make_wrapper(config, 'issue_write')
    async def register(manager, registry):
        registry.register(wrapper)
        return []
    monkeypatch.setattr(MCPManager, 'register_all_tools', register)
    monkeypatch.setattr('eviforge.mcp.cli.load_servers', lambda *a:[config])
    content = tmp_path/'plan.md'; content.write_text('Create the explicitly selected test Issue', encoding='utf-8')
    actions = tmp_path/'actions.json'; actions.write_text(json.dumps([{'tool_name':wrapper.name, 'arguments':{'owner':'owner','repo':'test','method':'create'}}]), encoding='utf-8')
    args = Namespace(work_dir=str(tmp_path), mcp_config=None, plan_command='create', session_id='s', turn_id='t', content_file=str(content), actions_file=str(actions))
    assert await handle_connected(args) == 0
    submitted = json.loads(capsys.readouterr().out)
    plan = PlanService(tmp_path).get(submitted['plan_id'])
    assert plan.actions[0].capability_fingerprint == wrapper.capability_fingerprint
    config.policy['repositories'] = ['owner/other']
    args = Namespace(work_dir=str(tmp_path), mcp_config=None, plan_command='approve', plan_id=plan.plan_id, content_hash=plan.content_hash, session_id='s', source_turn_id='t', execution_turn_id='next', agent_id='agent', ttl_seconds=300)
    assert await handle_connected(args) == 2
    remote.call_tool.assert_not_awaited()

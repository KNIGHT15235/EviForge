"""Opt-in live Provider -> deferred MCP discovery -> tools -> local report acceptance."""
from __future__ import annotations
import argparse
import asyncio
import hashlib
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from eviforge.client import create_client
from eviforge.config import AppConfig, MCPServerConfig, ProviderConfig
from eviforge.permissions import PermissionMode
from eviforge.runtime import RuntimeServices

async def verify(args):
    credentials = json.loads(Path(args.credentials).read_text(encoding='utf-8'))
    for name, value in credentials.items():
        if name.startswith('EVIFORGE_') and isinstance(value, str): os.environ[name] = value
    settings = json.loads(Path(args.provider).read_text(encoding='utf-8'))
    provider = ProviderConfig(name=settings['name'], protocol=settings.get('protocol', settings.get('type','openai')), base_url=settings['base_url'], model=settings['model'], api_key=credentials['EVIFORGE_MODEL_API_KEY'], max_output_tokens=4096)
    browser_enabled = bool(args.node and args.playwright_cli)
    project = Path('.eviforge/integration/agent-browser-fixture' if browser_enabled else '.eviforge/integration/agent-fixture').resolve()
    project.mkdir(parents=True, exist_ok=True)
    site = None
    report = {'status':'running', 'model':provider.model, 'checks':[]}
    configs = [
        MCPServerConfig('github', integration='github', required=True, url='https://api.githubcopilot.com/mcp/', headers={'Authorization':'Bearer ${EVIFORGE_GITHUB_TOKEN}'}, allowed_tools=['get_file_contents'], policy={'repositories':[args.repository]}),
        MCPServerConfig('context7', integration='context7', required=True, url='https://mcp.context7.com/mcp', allowed_tools=['resolve-library-id','query-docs']),
    ]
    browser_prompt = ''
    if browser_enabled:
        class Site(BaseHTTPRequestHandler):
            def do_GET(self):
                demo = project/'demo.html'
                if self.path not in ('/', '/demo.html') or not demo.is_file():
                    self.send_response(404); self.end_headers(); return
                self.send_response(200); self.send_header('Content-Type', 'text/html; charset=utf-8'); self.end_headers()
                self.wfile.write(demo.read_bytes())
            def log_message(self, *args): pass
        site = ThreadingHTTPServer(('127.0.0.1', 0), Site)
        threading.Thread(target=site.serve_forever, daemon=True).start()
        url = f'http://127.0.0.1:{site.server_port}'
        configs.append(MCPServerConfig('playwright', integration='playwright', required=True,
            command=args.node, args=[args.playwright_cli, '--headless', '--browser', args.browser],
            cwd=str(project), startup_timeout_seconds=90, call_timeout_seconds=60,
            allowed_tools=['browser_navigate','browser_snapshot','browser_fill_form','browser_click','browser_take_screenshot','browser_close'],
            policy={'origins':[url]}))
        browser_prompt = f'''
Also use WriteFile to create demo.html: a self-contained HTML page with a labeled
Library textbox, a Save button, and an element whose text becomes "Saved " plus
the textbox value when Save is clicked. Do not include external resources.
Use ToolSearch to discover Playwright MCP tools. Navigate to {url}/demo.html,
take a browser_snapshot, use browser_fill_form with the actual resolved Pydantic
library ID, click Save, and take another snapshot to verify Saved /pydantic/pydantic.
Use browser_take_screenshot with type png and scale css, without a filename.
The fill field and click schema use target for the snapshot reference in this version;
read the actual schema. Add the verified browser result to integration_report.md.
Close the browser after capturing evidence. Do not claim success without all these calls.
'''
    config = AppConfig(providers=[provider], mcp_servers=configs)
    runtime = RuntimeServices.create(config, provider, client=create_client(provider), work_dir=str(project), permission_mode=PermissionMode.ACCEPT_EDITS)
    try:
        errors = await runtime.start_mcp()
        if errors: raise RuntimeError('; '.join(errors))
        for tool in ['Agent','TeamCreate','TeamDelete','Bash','HttpRequest','WebFetch','WebSearch','EnterWorktree','ExitWorktree']:
            runtime.registry.disable(tool)
        runtime.begin_turn()
        prompt = f'''Complete this real MCP acceptance workflow. First use ToolSearch to discover the deferred MCP tools.
Use GitHub MCP get_file_contents to read README.md from {args.repository} (owner/repo separately).
Use Context7 MCP resolve-library-id for Pydantic, then query-docs for model_validate in Pydantic v2.
Use WriteFile to create integration_report.md inside the working directory, containing the repository name,
the actual resolved library ID, and one valid model_validate example grounded in the retrieved docs.
Do not use network tools outside MCP or spawn agents. Finish only after the file is written.
{browser_prompt}'''
        async with asyncio.timeout(420 if browser_enabled else 240):
            output = await runtime.agent.run_to_completion(prompt, runtime.conversation)
        calls = [call for message in runtime.conversation.history for call in message.tool_uses]
        called = {call.tool_name for call in calls}
        report['called_tools'] = sorted(called)
        report['agent_status'] = runtime.agent.last_run_status
        required = ['mcp_github_get_file_contents','mcp_context7_resolve-library-id','mcp_context7_query-docs','WriteFile']
        if browser_enabled:
            required += ['mcp_playwright_'+name for name in ('browser_navigate','browser_snapshot','browser_fill_form','browser_click','browser_take_screenshot','browser_close')]
        for name in required:
            assert name in called, f'Provider did not call {name}'
            report['checks'].append({'tool':name, 'passed':True})
        content = (project/'integration_report.md').read_text(encoding='utf-8')
        assert args.repository in content and 'model_validate' in content and '/pydantic/pydantic' in content
        assert runtime.agent.last_run_status == 'success', runtime.agent.last_run_status
        if browser_enabled:
            results = '\n'.join(item.content for message in runtime.conversation.history for item in message.tool_results)
            assert 'Saved /pydantic/pydantic' in results
            events = [json.loads(line) for line in (project/'.eviforge/mcp/events.jsonl').read_text(encoding='utf-8').splitlines()]
            artifacts = [artifact for event in events for artifact in event.get('artifacts', [])]
            assert artifacts, 'No browser evidence archived'
            for artifact in artifacts:
                data = Path(artifact['path']).read_bytes()
                assert data.startswith(b'\x89PNG\r\n\x1a\n')
                assert hashlib.sha256(data).hexdigest() == artifact['sha256']
            report['artifacts'] = artifacts
            report['browser_verified'] = True
        report.update(status='passed', tool_calls=len(calls), report_file=str(project/'integration_report.md'), output_characters=len(output),
                      input_tokens=runtime.agent.total_input_tokens, output_tokens=runtime.agent.total_output_tokens)
    except Exception as exc:
        # Do not print HTTP exception bodies or credentials.
        import traceback
        report.update(status='failed', error=type(exc).__name__, locations=[{'file':Path(frame.filename).name,'line':frame.lineno,'function':frame.name} for frame in traceback.extract_tb(exc.__traceback__)])
    finally:
        await runtime.close()
        if site:
            site.shutdown(); site.server_close()
    target = Path('.eviforge/integration/agent-browser-acceptance.json' if browser_enabled else '.eviforge/integration/agent-acceptance.json')
    target.write_text(json.dumps(report, indent=2), encoding='utf-8'); print(json.dumps(report))
    return report['status'] != 'passed'

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--credentials', required=True)
    parser.add_argument('--provider', required=True)
    parser.add_argument('--repository', required=True)
    parser.add_argument('--node')
    parser.add_argument('--playwright-cli')
    parser.add_argument('--browser', default='msedge')
    raise SystemExit(asyncio.run(verify(parser.parse_args())))

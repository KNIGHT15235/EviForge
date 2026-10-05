"""Opt-in real isolated browser acceptance, including redirect egress denial."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from eviforge.config import MCPServerConfig
from eviforge.mcp.manager import MCPManager
from eviforge.tools import ToolRegistry
from eviforge.tools.work_dir import tool_working_directory


async def verify(args):
    hits = []
    class External(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path); self.send_response(200); self.end_headers()
        def log_message(self, *args): pass
    external = ThreadingHTTPServer(('127.0.0.1', 0), External)
    external_url = f'http://127.0.0.1:{external.server_port}'
    class Site(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == '/redirect':
                self.send_response(302); self.send_header('Location', external_url + '/escaped'); self.end_headers(); return
            self.send_response(200); self.send_header('Content-Type', 'text/html'); self.end_headers()
            self.wfile.write(f'''<!doctype html><html><title>EviForge MCP test</title><body>
<h1>MCP browser acceptance</h1><label>Name<input id="name"></label>
<button onclick="document.querySelector('#result').textContent='Saved '+document.querySelector('#name').value">Save</button>
<p id="result">Pending</p><a href="/redirect">Redirect test</a>
<img src="{external_url}/blocked-image"><script>fetch('{external_url}/blocked-fetch').catch(()=>{{}})</script>
</body></html>'''.encode())
        def log_message(self, *args): pass
    site = ThreadingHTTPServer(('127.0.0.1', 0), Site)
    for server in (external, site): threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f'http://127.0.0.1:{site.server_port}'
    project = Path('.eviforge/integration/browser-fixture').resolve(); project.mkdir(parents=True, exist_ok=True)
    manager, registry = MCPManager(), ToolRegistry()
    names = ['browser_navigate', 'browser_snapshot', 'browser_fill_form', 'browser_click', 'browser_take_screenshot', 'browser_close']
    manager.load_configs([MCPServerConfig('playwright', integration='playwright', required=True, command=args.node, args=[args.mcp_cli, '--headless', '--browser', args.browser], cwd=str(project), allowed_tools=names, policy={'origins':[url]}, startup_timeout_seconds=90, call_timeout_seconds=45)])
    report = {'service':'playwright', 'checks':[], 'status':'running'}
    try:
        errors = await manager.register_all_tools(registry)
        if errors: raise RuntimeError('; '.join(errors))
        report['server_info'] = manager.status()[0]['server_info']
        tools = {t.mcp_tool_name:t for t in registry.list_tools()}
        async def call(name, arguments, allow_error=False):
            tool = tools[name]; result = await tool.execute(tool.validate_arguments(arguments))
            if result.is_error and not allow_error: raise RuntimeError(result.output)
            return result
        with tool_working_directory(project):
            await call('browser_navigate', {'url':url})
            snapshot = await call('browser_snapshot', {})
            input_ref = re.search(r'textbox[^\n]*\[ref=([^\]]+)\]', snapshot.output).group(1)
            save_ref = re.search(r'button "Save"[^\n]*\[ref=([^\]]+)\]', snapshot.output).group(1)
            field_properties = tools['browser_fill_form'].get_schema()['input_schema']['properties']['fields']['items']['properties']
            field_selector = 'target' if 'target' in field_properties else 'ref'
            click_selector = 'target' if 'target' in tools['browser_click'].get_schema()['input_schema']['properties'] else 'ref'
            await call('browser_fill_form', {'fields':[{'name':'Name', 'type':'textbox', field_selector:input_ref, 'value':'EviForge'}]})
            await call('browser_click', {'element':'Save button', click_selector:save_ref})
            snapshot = await call('browser_snapshot', {})
            assert 'Saved EviForge' in snapshot.output
            report['checks'].append({'name':'navigate_snapshot_fill_click', 'passed':True})
            screenshot_arguments = {'type':'png'}
            if 'scale' in tools['browser_take_screenshot'].get_schema()['input_schema'].get('required', []):
                screenshot_arguments['scale'] = 'css'
            screenshot = await call('browser_take_screenshot', screenshot_arguments)
            assert screenshot.artifacts
            for artifact in screenshot.artifacts:
                data = Path(artifact['path']).read_bytes()
                assert hashlib.sha256(data).hexdigest() == artifact['sha256']
                assert data.startswith(b'\x89PNG\r\n\x1a\n')
            report['checks'].append({'name':'real_png_sha256', 'passed':True, 'artifacts':list(screenshot.artifacts)})
            redirect = await call('browser_navigate', {'url':url+'/redirect'}, allow_error=True)
            assert redirect.is_error, 'Redirect must be refused'
            assert hits == [], 'Browser contacted an unapproved origin'
            report['checks'].append({'name':'cross_origin_images_fetch_redirect_blocked', 'passed':True, 'external_requests':len(hits)})
            await call('browser_close', {})
        report['status'] = 'passed'
    except Exception as exc:
        report.update(status='failed', error=f'{type(exc).__name__}: {exc}')
    finally:
        await manager.shutdown()
        for server in (site, external): server.shutdown(); server.server_close()
    target = Path('.eviforge/integration/playwright-acceptance.json')
    target.write_text(json.dumps(report, indent=2), encoding='utf-8'); print(json.dumps(report))
    return report['status'] != 'passed'


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--node', required=True)
    parser.add_argument('--mcp-cli', required=True)
    parser.add_argument('--browser', default='msedge')
    raise SystemExit(asyncio.run(verify(parser.parse_args())))

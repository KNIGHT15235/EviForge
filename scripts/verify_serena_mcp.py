"""Real Python symbol/reference acceptance against an isolated fixture project."""
from __future__ import annotations
import argparse
import asyncio
import json
from pathlib import Path

from eviforge.config import MCPServerConfig
from eviforge.mcp.manager import MCPManager
from eviforge.tools import ToolRegistry
from eviforge.tools.work_dir import tool_working_directory

async def verify(args):
    project = Path('.eviforge/integration/serena-fixture').resolve(); project.mkdir(parents=True, exist_ok=True)
    (project/'sample.py').write_text('def add(left: int, right: int) -> int:\n    return left + right\n', encoding='utf-8')
    (project/'usage.py').write_text('from sample import add\n\nresult = add(1, 2)\n', encoding='utf-8')
    settings = project/'.serena'; settings.mkdir(exist_ok=True)
    (settings/'project.yml').write_text('project_name: eviforge_mcp_fixture\nlanguages: [python]\nignore_all_files_in_gitignore: false\nread_only: true\n', encoding='utf-8')
    manager, registry = MCPManager(), ToolRegistry()
    names = ['find_symbol', 'find_referencing_symbols', 'get_symbols_overview', 'search_for_pattern', 'read_file']
    manager.load_configs([MCPServerConfig('serena', integration='serena', required=True, command=args.command, cwd=str(project), args=['start-mcp-server', '--project', str(project), '--context', 'desktop-app', '--mode', 'planning', '--enable-web-dashboard', 'false', '--enable-gui-log-window', 'false', '--open-web-dashboard', 'false'], startup_timeout_seconds=180, call_timeout_seconds=90, allowed_tools=names, policy={'project':str(project)})])
    report = {'service':'serena', 'checks':[], 'status':'running'}
    try:
        errors = await manager.register_all_tools(registry)
        if errors: raise RuntimeError('; '.join(errors))
        report['server_info'] = manager.status()[0]['server_info']
        tools = {t.mcp_tool_name:t for t in registry.list_tools()}
        with tool_working_directory(project):
            for name, arguments, expected in [
                ('get_symbols_overview', {'relative_path':'sample.py'}, 'add'),
                ('find_symbol', {'name_path_pattern':'add', 'relative_path':'sample.py', 'include_body':True}, 'return left + right'),
                ('find_referencing_symbols', {'name_path':'add', 'relative_path':'sample.py'}, 'usage.py'),
                ('search_for_pattern', {'substring_pattern':'add', 'relative_path':'usage.py'}, 'add'),
                ('read_file', {'relative_path':'usage.py'}, 'result = add(1, 2)'),
            ]:
                tool = tools[name]
                # Serena 1.7 renamed its reference selector to name_path_pattern.
                properties = tool.get_schema()['input_schema'].get('properties', {})
                if 'name_path' in arguments and 'name_path' not in properties and 'name_path_pattern' in properties:
                    arguments['name_path_pattern'] = arguments.pop('name_path')
                result = await tool.execute(tool.validate_arguments(arguments))
                if result.is_error or expected not in result.output: raise RuntimeError(f'{name}: {result.output}')
                report['checks'].append({'tool':name, 'passed':True})
        report['status'] = 'passed'
    except Exception as exc:
        report.update(status='failed', error=f'{type(exc).__name__}: {exc}')
    finally: await manager.shutdown()
    target = Path('.eviforge/integration/serena-acceptance.json')
    target.write_text(json.dumps(report, indent=2), encoding='utf-8'); print(json.dumps(report))
    return report['status'] != 'passed'

if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--command', required=True)
    raise SystemExit(asyncio.run(verify(parser.parse_args())))

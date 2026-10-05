"""Opt-in acceptance on explicitly designated Feishu resources, using production MCP.

Private resources/receipts stay in the ignored integration directory. Existing
receipts avoid creating duplicate test records, tasks and imported documents.
Message and Wiki acceptance are explicitly deferred, and this script never sends.
"""
from __future__ import annotations
import argparse
import asyncio
import json
import os
import time
import uuid
from pathlib import Path

from eviforge.config import MCPServerConfig
from eviforge.mcp.manager import MCPManager
from eviforge.tools import ToolRegistry
from eviforge.tools.work_dir import tool_working_directory


async def verify(args):
    if not args.allow_write:
        raise ValueError('--allow-write is required for designated test resources')
    credential_path = Path(args.credentials).resolve()
    for key, value in json.loads(credential_path.read_text(encoding='utf-8')).items():
        if key.startswith('EVIFORGE_') and isinstance(value, str):
            os.environ[key] = value
    selected = json.loads(Path(args.resources).read_text(encoding='utf-8'))
    private_store = credential_path.parent/'feishu-auth'
    names = ['docx.v1.document.rawContent', 'docx.builtin.import',
             'bitable.v1.appTableField.list', 'bitable.v1.appTableRecord.search',
             'bitable.v1.appTableRecord.create', 'bitable.v1.appTableRecord.update',
             'task.v2.task.create', 'task.v2.task.patch']
    policy = {'documents':[selected['document_id']],
              'tables':[selected['app_token']+'/'+selected['table_id']],
              'chats':[], 'tasks':[], 'allow_writes':True,
              'allow_messages':False, 'allow_create_documents':True, 'allow_create_tasks':True,
              'auth_store':str(private_store/'lark-mcp-nodejs'/'Data'/'storage.json')}
    config = MCPServerConfig('feishu', integration='feishu', required=True,
        command=args.node, args=[args.mcp_cli, 'mcp', '--oauth', '--token-mode',
        'user_access_token', '--tool-name-case', 'dot', '--tools', ','.join(names)],
        env={'APP_ID':'${EVIFORGE_FEISHU_APP_ID}', 'APP_SECRET':'${EVIFORGE_FEISHU_APP_SECRET}',
             'LOCALAPPDATA':str(private_store), 'APPDATA':str(private_store)},
        allowed_tools=names, policy=policy, startup_timeout_seconds=90, call_timeout_seconds=90)
    manager, registry = MCPManager(), ToolRegistry()
    report = {'service':'feishu', 'status':'running', 'checks':[],
              'limits':['Message send/history acceptance is deferred by the operator: official 0.5.1 is tenant-only, and the API rejected the user token.',
                        'Wiki live acceptance is deferred by the operator.']}
    evidence = credential_path.parent/'feishu-receipts.json'
    receipts = json.loads(evidence.read_text(encoding='utf-8')) if evidence.exists() else {}
    report['reused_receipts'] = {name:bool(receipts.get(name)) for name in ('record_id', 'task_guid', 'task_completed', 'report_token')}
    marker = receipts.setdefault('marker', 'EviForge MCP '+str(int(time.time())))

    def persist():
        temporary = evidence.with_suffix('.tmp')
        temporary.write_text(json.dumps(receipts, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(evidence)

    async def discover():
        manager.load_configs([config])
        errors = await manager.register_all_tools(registry)
        report['discovery'] = manager.status()
        report['discovery_errors'] = errors
        if errors: raise RuntimeError('Feishu MCP discovery failed')
        report['server_info'] = manager.status()[0]['server_info']

    async def call(name, arguments):
        tool = next(t for t in registry.list_tools() if t.mcp_tool_name == name)
        result = await tool.execute(tool.validate_arguments({**arguments, 'useUAT':True}))
        if result.is_error:
            # Keep diagnostic bodies private, and preserve ambiguous call IDs for reconciliation.
            receipts['failure'] = {'tool':name, 'status':result.execution_status, 'details':result.output}
            persist()
            raise RuntimeError(name+': '+result.execution_status)
        report['checks'].append({'tool':name, 'passed':True})
        body = json.loads(result.output)
        # The official SDK handler unwraps successful response.data; builtin import
        # already returns its ticket/result object. Preserve both real shapes.
        return body if name == 'docx.builtin.import' or 'data' in body else {'code':0, 'data':body}

    try:
        await discover()
        with tool_working_directory(Path.cwd()):
            doc = await call('docx.v1.document.rawContent', {'path':{'document_id':selected['document_id']}})
            assert doc.get('code') in (0, '0') and isinstance(doc['data']['content'], str)
            report['document_characters'] = len(doc['data']['content'])
            table_path = {'app_token':selected['app_token'], 'table_id':selected['table_id']}
            fields = await call('bitable.v1.appTableField.list', {'path':table_path})
            text_field = next(f['field_name'] for f in fields['data']['items'] if f['type'] == 1)
            if 'record_id' not in receipts:
                created = await call('bitable.v1.appTableRecord.create', {'path':table_path, 'data':{'fields':{text_field:marker}}})
                receipts['record_id'] = created['data']['record']['record_id']; persist()
            updated_value = marker+' verified'
            await call('bitable.v1.appTableRecord.update', {'path':{**table_path, 'record_id':receipts['record_id']}, 'data':{'fields':{text_field:updated_value}}})
            if not args.defer_table_readback:
                records = await call('bitable.v1.appTableRecord.search', {'path':table_path,
                    'data':{'filter':{'conjunction':'and','conditions':[{'field_name':text_field,'operator':'is','value':[updated_value]}]}},
                    'params':{'page_size':20}})
                assert any(r['record_id'] == receipts['record_id'] for r in records['data']['items'])
            if 'task_guid' not in receipts:
                task = await call('task.v2.task.create', {'data':{'summary':marker, 'description':'Disposable EviForge MCP acceptance task.', 'client_token':str(uuid.uuid4())}})
                receipts['task_guid'] = task['data']['task']['guid']; persist()
            policy['tasks'] = [receipts['task_guid']]
            await discover()  # New concrete task selector requires a fresh capability snapshot.
            if not receipts.get('task_completed'):
                task = await call('task.v2.task.patch', {'path':{'task_guid':receipts['task_guid']},
                    'data':{'task':{'summary':marker+' verified', 'completed_at':str(int(time.time()*1000))}, 'update_fields':['summary','completed_at']}})
                assert task['data']['task']['summary'] == marker+' verified'
                receipts['task_completed'] = True; persist()
            if 'report_token' not in receipts:
                imported = await call('docx.builtin.import', {'data':{'file_name':'EviForge MCP acceptance',
                    'markdown':'# EviForge MCP acceptance\n\n'+marker+'\n\nDocument read, Bitable update and task completion verified through EviForge.'}})
                assert imported['result']['job_status'] == 0
                receipts['report_token'] = imported['result']['token']; persist()
            policy['documents'].append(receipts['report_token'])
            await discover()
            imported_doc = await call('docx.v1.document.rawContent', {'path':{'document_id':receipts['report_token']}})
            assert marker in imported_doc['data']['content']
            report['message_sent'] = bool(receipts.get('message_id'))
            report['status'] = 'partial' if args.defer_table_readback else 'passed'
    except Exception as exc:
        report.update(status='failed', error_type=type(exc).__name__)
    finally:
        await manager.shutdown()
    output = credential_path.parent/'feishu-acceptance.json'
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))
    return report['status'] != 'passed'


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    for name in ('node', 'mcp-cli', 'credentials', 'resources'): parser.add_argument('--'+name, required=True)
    parser.add_argument('--allow-write', action='store_true')
    parser.add_argument('--defer-table-readback', action='store_true', help='Diagnose independent capabilities; result stays partial')
    raise SystemExit(asyncio.run(verify(parser.parse_args())))

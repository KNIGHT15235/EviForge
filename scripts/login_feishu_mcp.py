"""Run official lark-mcp login (which opens the system browser), without secret argv/logs."""
from __future__ import annotations
import argparse
import json
import subprocess
from pathlib import Path
from eviforge.config import MCPServerConfig
from eviforge.mcp.client import MCPClient

def main(args):
    credentials_path = Path(args.credentials).resolve()
    values = json.loads(credentials_path.read_text(encoding='utf-8'))
    private_store = str(credentials_path.parent/'feishu-auth')
    env = MCPClient(MCPServerConfig('feishu-login'))._child_env()
    env.update(APP_ID=values['EVIFORGE_FEISHU_APP_ID'], APP_SECRET=values['EVIFORGE_FEISHU_APP_SECRET'], LOCALAPPDATA=private_store, APPDATA=private_store)
    cli = str(Path(args.package).resolve()/'dist/cli.js')
    child = subprocess.Popen([args.node, cli, 'login', '--scope', args.scopes], env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, encoding='utf-8', errors='replace')
    try:
        for line in child.stdout:
            value = line.strip()
            if value.startswith('http://localhost:3000/authorize?'):
                print(json.dumps({'state':'authorization_required','url':value}),flush=True)
            elif 'Successfully logged in' in value:
                print(json.dumps({'state':'authenticated'}),flush=True)
        return child.wait()
    finally:
        if child.poll() is None:
            child.terminate(); child.wait(timeout=10)

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--credentials', required=True)
    parser.add_argument('--package', required=True)
    parser.add_argument('--node', required=True)
    parser.add_argument('--scopes', required=True)
    raise SystemExit(main(parser.parse_args()))

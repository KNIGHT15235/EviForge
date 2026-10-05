"""Local-only, CSRF-protected credential form. Never displays stored secrets.

Use this instead of pasting App Secrets into chat or command arguments.
"""
from __future__ import annotations

import argparse
import html
import json
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs


def serve(destination: Path, app_id: str = "") -> None:
    destination = destination.resolve()
    private_root = (Path.cwd() / ".eviforge").resolve()
    if not destination.is_relative_to(private_root):
        raise ValueError("Credential destination must be inside the project's ignored .eviforge directory")
    nonce = secrets.token_urlsafe(32)
    form = f"""<!doctype html><html lang=zh><meta charset=utf-8><title>EviForge 本地凭据配置</title>
<h1>EviForge 本地凭据配置</h1><p>仅监听本机。保存到 Git 忽略目录；不会回显密钥。</p>
<form method=post action=/save><input type=hidden name=csrf value='{nonce}'>
<p><label>App ID <input name=EVIFORGE_FEISHU_APP_ID value='{html.escape(app_id, quote=True)}' autocomplete=off></label></p>
<p><label>App Secret <input type=password name=EVIFORGE_FEISHU_APP_SECRET autocomplete=off></label></p>
<p><label>模型 API Key（可选） <input type=password name=EVIFORGE_MODEL_API_KEY autocomplete=off></label></p>
<button type=submit>保存到本地</button></form></html>""".encode()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def respond(self, code, body):
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'none'; form-action 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self.respond(200 if self.path == "/" else 404, form if self.path == "/" else b"Not found")

        def do_POST(self):
            expected = f"http://127.0.0.1:{self.server.server_port}"
            if self.path != "/save" or self.headers.get("Origin") != expected or self.headers.get("Host") != expected.removeprefix("http://"):
                self.respond(403, b"Forbidden")
                return
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 16_384:
                self.respond(413, b"Request too large")
                return
            values = parse_qs(self.rfile.read(length).decode())
            if not secrets.compare_digest(values.get("csrf", [""])[0], nonce):
                self.respond(403, b"Forbidden")
                return
            current = json.loads(destination.read_text(encoding="utf-8")) if destination.exists() else {}
            for key in ("EVIFORGE_FEISHU_APP_ID", "EVIFORGE_FEISHU_APP_SECRET", "EVIFORGE_MODEL_API_KEY"):
                value = values.get(key, [""])[0]
                if value:
                    current[key] = value
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_suffix(".tmp")
            temporary.write_text(json.dumps(current), encoding="utf-8")
            temporary.replace(destination)
            self.respond(200, "<meta charset=utf-8><h1>已保存本地凭据</h1><p>密钥未显示，请返回对话继续。</p>".encode())
            print(json.dumps({"credentials_saved": True, "configured_fields": sorted(current)}), flush=True)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    print(json.dumps({"local_form": f"http://127.0.0.1:{server.server_port}/", "secret_storage": "ignored .eviforge directory"}), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", default=".eviforge/integration/credentials.json")
    parser.add_argument("--app-id", default="")
    args = parser.parse_args()
    serve(Path(args.file), args.app_id)

"""Live GitHub acceptance: official MCP discovery/read/write/read-back.

Only --repository explicitly supplied by the operator is used. Fixture branch
creation and cleanup use REST; the capabilities under test use EviForge MCP.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import time
from pathlib import Path

import httpx

from eviforge.config import MCPServerConfig
from eviforge.mcp.manager import MCPManager
from eviforge.tools import ToolRegistry
from eviforge.tools.work_dir import tool_working_directory


async def verify(args):
    if not args.allow_write:
        raise ValueError("This acceptance creates an Issue and draft PR; --allow-write is required")
    if args.credentials:
        token = json.loads(Path(args.credentials).read_text(encoding="utf-8"))["EVIFORGE_GITHUB_TOKEN"]
        os.environ["EVIFORGE_GITHUB_TOKEN"] = token
    token = os.environ["EVIFORGE_GITHUB_TOKEN"]
    owner, repo = args.repository.split("/", 1)
    report = {"service": "github", "repository": args.repository, "checks": [], "status": "running"}
    manager, registry = MCPManager(), ToolRegistry()
    config = MCPServerConfig("github", integration="github", required=True, url="https://api.githubcopilot.com/mcp/", headers={"Authorization": "Bearer ${EVIFORGE_GITHUB_TOKEN}", "X-MCP-Toolsets": "repos,issues,pull_requests"}, policy={"repositories": [args.repository], "allow_writes": True})
    manager.load_configs([config])
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    async def invoke(name, arguments):
        tool = next(tool for tool in registry.list_tools() if tool.mcp_tool_name == name)
        with tool_working_directory(Path.cwd()):
            result = await tool.execute(tool.validate_arguments(arguments))
        if result.is_error:
            raise RuntimeError(f"{name}: {result.execution_status}: {result.output}")
        report["checks"].append({"tool": name, "passed": True})
        if result.structured_content:
            return result.structured_content
        try:
            return json.loads(result.output)
        except ValueError:
            return {"text": result.output}

    try:
        errors = await manager.register_all_tools(registry)
        if errors:
            raise RuntimeError("; ".join(errors))
        report["discovery"] = manager.status()
        await invoke("get_file_contents", {"owner": owner, "repo": repo, "path": ""})
        await invoke("search_issues", {"query": f"repo:{args.repository} is:issue"})
        async with httpx.AsyncClient(base_url="https://api.github.com", headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "User-Agent": "EviForge-MCP-Acceptance"}, timeout=45) as rest:
            async def api(method, path, body=None):
                response = await rest.request(method, path, json=body)
                if not response.is_success:
                    raise RuntimeError(f"GitHub fixture API returned HTTP {response.status_code}")
                return response.json() if response.content else {}
            metadata = await api("GET", f"/repos/{args.repository}")
            branch_base = metadata["default_branch"]
            base = await api("GET", f"/repos/{args.repository}/git/ref/heads/{branch_base}")
            marker = str(int(time.time()))
            branch = f"eviforge-mcp-acceptance-{marker}"
            await api("POST", f"/repos/{args.repository}/git/refs", {"ref": f"refs/heads/{branch}", "sha": base["object"]["sha"]})
            await api("PUT", f"/repos/{args.repository}/contents/eviforge-mcp-acceptance/{marker}.md", {"message": "Add disposable EviForge MCP acceptance fixture", "branch": branch, "content": base64.b64encode(b"EviForge official GitHub MCP integration acceptance fixture.\n").decode()})
            issue = await invoke("issue_write", {"owner": owner, "repo": repo, "method": "create", "title": f"EviForge MCP acceptance {marker}", "body": "Disposable integration test: created through EviForge and the official GitHub MCP server. The designated repository owner authorized this test."})
            number = issue.get("number")
            if not number:
                # Some remote versions return a minimal URL receipt.
                receipt = issue.get("url", issue.get("html_url", ""))
                number = int(receipt.rstrip("/").split("/")[-1])
            await invoke("issue_read", {"owner": owner, "repo": repo, "method": "get", "issue_number": number})
            report["issue"] = f"https://github.com/{args.repository}/issues/{number}"
            pr = await invoke("create_pull_request", {"owner": owner, "repo": repo, "title": f"EviForge MCP draft acceptance {marker}", "body": "Disposable draft PR created through EviForge's MCP wrapper. No merge is performed.", "head": branch, "base": branch_base, "draft": True})
            pr_number = pr.get("number") or int(pr.get("url", pr.get("html_url", "")).rstrip("/").split("/")[-1])
            verified_pr = await invoke("pull_request_read", {"owner": owner, "repo": repo, "method": "get", "pullNumber": pr_number})
            # REST independently verifies remote state; never use a mocked receipt.
            remote_pr = await api("GET", f"/repos/{args.repository}/pulls/{pr_number}")
            if not remote_pr.get("draft") or remote_pr["head"]["ref"] != branch:
                raise RuntimeError("Remote draft PR read-back does not match the fixture")
            report["pull_request"] = remote_pr["html_url"]
            await api("PATCH", f"/repos/{args.repository}/issues/{number}", {"state": "closed"})
            await api("PATCH", f"/repos/{args.repository}/pulls/{pr_number}", {"state": "closed"})
            report["cleanup"] = "Test Issue and draft PR closed; branch/content retained for audit"
        report["status"] = "passed"
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = f"{type(exc).__name__}: {exc}".replace(token, "[REDACTED]")
    finally:
        await manager.shutdown()
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "discovery"}, ensure_ascii=False))
    return report["status"] != "passed"


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--credentials")
    parser.add_argument("--allow-write", action="store_true")
    parser.add_argument("--output", default=".eviforge/integration/github-acceptance.json")
    raise SystemExit(asyncio.run(verify(parser.parse_args())))

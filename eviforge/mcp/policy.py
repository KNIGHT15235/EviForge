"""Locally reviewed MCP capabilities; remote annotations never grant authority."""
from __future__ import annotations

import re
import hashlib
from pathlib import Path
from urllib.parse import urlsplit
from typing import Any

from eviforge.config import MCPServerConfig
from eviforge.config import resolve_env_vars
from eviforge.permissions.capabilities import content_digest, canonical_path, within


READ_TOOLS = {
    "github": {"get_file_contents", "list_branches", "list_commits", "get_commit", "list_issues", "issue_read", "list_pull_requests", "pull_request_read", "search_repositories", "search_code", "search_issues", "search_pull_requests"},
    "context7": {"resolve-library-id", "query-docs"},
    "playwright": {"browser_snapshot", "browser_take_screenshot"},
    "feishu": {"docx_v1_document_rawContent", "docx_builtin_search", "wiki_v2_space_getNode", "wiki_v1_node_search", "bitable_v1_appTableRecord_search", "bitable_v1_appTable_list", "bitable_v1_appTableField_list", "im_v1_message_list"},
    "serena": {"find_symbol", "find_referencing_symbols", "get_symbols_overview", "search_for_pattern", "list_dir", "find_file", "read_file", "get_current_config", "check_onboarding_performed", "initial_instructions"},
}
WRITE_TOOLS = {
    "github": {"issue_write", "create_pull_request"},
    "context7": set(),
    "playwright": {"browser_navigate", "browser_click", "browser_fill_form", "browser_type", "browser_press_key", "browser_wait_for", "browser_close"},
    "feishu": {"docx_builtin_import", "bitable_v1_appTableRecord_create", "bitable_v1_appTableRecord_update", "bitable_v1_appTableRecord_batchCreate", "bitable_v1_appTableRecord_batchUpdate", "task_v2_task_create", "task_v2_task_patch", "im_v1_message_create"},
    "serena": set(),
}


class MCPPolicyError(ValueError):
    pass


def tool_kind(config: MCPServerConfig, name: str) -> str | None:
    if name in config.denied_tools or config.allowed_tools and name not in config.allowed_tools:
        return None
    if config.integration == "custom":
        return "command"  # legacy, never trusted by the Plan adapter
    if config.integration == "feishu":
        name = name.replace(".", "_")
    if name in READ_TOOLS.get(config.integration, set()):
        return "read"
    if name in WRITE_TOOLS.get(config.integration, set()):
        return "write"
    return None


def fingerprint(config: MCPServerConfig, schema: dict[str, Any], name: str) -> str:
    # Hash credentials too, but never expose them. Changing auth/identity invalidates grants.
    def resolved(value: Any) -> Any:
        if isinstance(value, str):
            return resolve_env_vars(value)
        if isinstance(value, dict):
            return {key: resolved(item) for key, item in value.items()}
        if isinstance(value, list):
            return [resolved(item) for item in value]
        return value
    config_fields = resolved(vars(config))
    auth_store = config.policy.get('auth_store')
    if auth_store:
        from eviforge.mcp.diagnostics import resolve_required
        path = Path(resolve_required(auth_store)).expanduser()
        try:
            with path.open('rb') as stream:
                payload = stream.read(4_194_305)
            if len(payload) > 4_194_304:
                raise MCPPolicyError('OAuth store exceeds configured capability snapshot limit')
            config_fields['auth_store_sha256'] = hashlib.sha256(payload).hexdigest()
        except FileNotFoundError:
            config_fields['auth_store_sha256'] = 'missing'
    return content_digest({"config": config_fields, "tool": name, "schema": schema})


def origin(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise MCPPolicyError("Invalid scoped URL")
    default = 443 if parsed.scheme == "https" else 80
    port = parsed.port or default
    return f"{parsed.scheme}://{parsed.hostname.lower()}:{port}"


def _require(value: str, scope: Any, label: str) -> str:
    if not isinstance(scope, list) or not value or value not in scope:
        raise MCPPolicyError(f"MCP_RESOURCE_DENIED: {label} is outside configured scope")
    return value


def resources(config: MCPServerConfig, name: str, args: dict[str, Any], cwd: str) -> tuple[str, ...]:
    """Validate concrete resource selectors and return canonical opaque resource IDs."""
    kind = tool_kind(config, name)
    if kind is None or not config.enabled:
        raise MCPPolicyError("MCP_TOOL_DENIED: tool is disabled or not in the local capability profile")
    policy = config.policy
    integration = config.integration
    if integration == "feishu":
        name = name.replace(".", "_")
    if kind == "write" and integration != "playwright" and not policy.get("allow_writes", False):
        raise MCPPolicyError("MCP_WRITE_DENIED: enable writes explicitly for a designated test resource")
    if integration == "custom":
        return ()
    if integration == "github":
        if name.startswith("search_"):
            query = args.get("query", args.get("q", ""))
            if not isinstance(query, str) or re.search(r"\bOR\b|[()]|(?:^|\s)-repo:", query, re.I):
                raise MCPPolicyError("GitHub search requires a simple repository-scoped query")
            repos = re.findall(r"(?:^|\s)repo:([^\s]+)", query)
            if len(repos) != 1:
                raise MCPPolicyError("GitHub search requires exactly one repo:owner/name qualifier")
            repo = repos[0]
        else:
            repo = f"{args.get('owner', '')}/{args.get('repo', '')}"
        _require(repo, policy.get("repositories"), "repository")
        if name == "create_pull_request" and args.get("draft") is not True:
            raise MCPPolicyError("Only draft pull requests are enabled")
        if name == "issue_write" and args.get("method") != "create":
            raise MCPPolicyError("Only Issue creation is enabled in the initial profile")
        if name == "pull_request_read" and args.get("method") not in {"get", "get_diff", "get_files", "get_comments", "get_review_comments", "get_reviews", "get_status"}:
            raise MCPPolicyError("Unsupported PR read method")
        if name == "issue_read" and args.get("method") not in {"get", "get_comments", "get_sub_issues", "get_labels"}:
            raise MCPPolicyError("Unsupported Issue read method")
        return (f"github:repo:{repo}",)
    if integration == "context7":
        return ("context7:documentation",)
    if integration == "playwright":
        origins = policy.get("origins", [])
        if not origins:
            raise MCPPolicyError("Playwright requires explicit test origins")
        if name == "browser_navigate":
            _require(origin(args.get("url", "")), [origin(x) for x in origins], "browser origin")
        if name == "browser_take_screenshot" and args.get("filename"):
            # Explicit paths may escape the MCP server output directory. Use returned pixels.
            raise MCPPolicyError("Screenshot filenames are disabled; archive returned image content")
        return tuple(sorted(f"playwright:origin:{origin(x)}" for x in origins))
    if integration == "serena":
        from eviforge.mcp.diagnostics import resolve_required
        project = canonical_path(resolve_required(policy.get("project", config.cwd or cwd)), cwd)
        if canonical_path(cwd, cwd) != project:
            raise MCPPolicyError("Serena instance is bound to another project/worktree")
        for key in ("relative_path", "path"):
            if key in args and args[key]:
                target = canonical_path(args[key], project)
                if not within(target, [project]):
                    raise MCPPolicyError("Serena path escapes its project")
        return (f"serena:project:{project}",)
    if integration == "feishu":
        if args.get('useUAT') is not True:
            raise MCPPolicyError('Feishu managed tools require explicit user identity (useUAT=true)')
        path = args.get("path", {})
        body = args.get("data", args.get("body", {}))
        query = args.get("params", args.get("query", {}))
        if not all(isinstance(x, dict) for x in (path, body, query)):
            raise MCPPolicyError("Feishu path/data/params must be objects")
        if name.startswith("bitable_"):
            table = f"{path.get('app_token', '')}/{path.get('table_id', '')}"
            _require(table, policy.get("tables"), "Bitable table")
            return (f"feishu:table:{table}",)
        if name == "docx_v1_document_rawContent":
            token = path.get("document_id", "")
            _require(token, policy.get("documents"), "document")
            return (f"feishu:document:{token}",)
        if name == "wiki_v2_space_getNode":
            token = query.get("token", "")
            _require(token, policy.get("wiki_nodes"), "wiki node")
            return (f"feishu:wiki:{token}",)
        if name in {"docx_builtin_search", "wiki_v1_node_search"}:
            if policy.get("allow_discovery") is not True:
                raise MCPPolicyError("Feishu account-wide search must be explicitly enabled")
            return ("feishu:discovery",)
        if name == "docx_builtin_import":
            if policy.get("allow_create_documents") is not True:
                raise MCPPolicyError("Feishu report import is not authorized")
            return ("feishu:create_document",)
        if name.startswith("task_"):
            if name.endswith("_create"):
                if policy.get("allow_create_tasks") is not True:
                    raise MCPPolicyError("Feishu task creation is not authorized")
                return ("feishu:create_task",)
            guid = path.get("task_guid", "")
            _require(guid, policy.get("tasks"), "task")
            return (f"feishu:task:{guid}",)
        if name.startswith("im_"):
            if name.endswith("_create"):
                if query.get("receive_id_type") != "chat_id" or policy.get("allow_messages") is not True:
                    raise MCPPolicyError("Message sending requires explicit chat authorization")
                chat = body.get("receive_id", "")
            else:
                if query.get("container_id_type") != "chat":
                    raise MCPPolicyError("Only scoped chat history is enabled")
                chat = query.get("container_id", "")
            _require(chat, policy.get("chats"), "chat")
            return (f"feishu:chat:{chat}",)
    raise MCPPolicyError("No trusted resource adapter for tool")

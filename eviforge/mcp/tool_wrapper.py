from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import time
import uuid
from pathlib import Path
from typing import Any

from jsonschema.validators import validator_for
from mcp import types as mcp_types
from pydantic import BaseModel, ConfigDict, model_validator

from eviforge.config import MCPServerConfig
from eviforge.mcp.client import MCPClient
from eviforge.mcp.diagnostics import safe_error, redact, redact_data
from eviforge.mcp.policy import resources, tool_kind, fingerprint, MCPPolicyError
from eviforge.permissions.capabilities import content_digest
from eviforge.tools.base import Tool, ToolResult
from eviforge.tools.work_dir import get_tool_work_dir


def _build_params_model(tool_name: str, input_schema: dict[str, Any]) -> type[BaseModel]:
    def check_refs(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {'$ref', '$dynamicRef', '$recursiveRef'} and (not isinstance(item, str) or not item.startswith('#')):
                    raise ValueError('External JSON Schema references are disabled')
                check_refs(item)
        elif isinstance(value, list):
            for item in value:
                check_refs(item)
    check_refs(input_schema)
    validator_type = validator_for(input_schema)
    validator_type.check_schema(input_schema)
    validator = validator_type(input_schema)

    class Params(BaseModel):
        model_config = ConfigDict(extra='allow')

        @model_validator(mode='before')
        @classmethod
        def validate_schema(cls, value: Any) -> Any:
            if not isinstance(value, dict):
                raise ValueError('MCP arguments must be a JSON object')
            json.dumps(value, allow_nan=False)
            error = next(validator.iter_errors(value), None)
            if error is not None:
                raise ValueError(f"JSON Schema validation failed at /{'/'.join(map(str, error.absolute_path))}: {error.validator}")
            return value

    Params.__name__ = f'{tool_name}Params'
    return Params


def _json_type_to_python(json_type: str) -> type:
    return {'string': str, 'integer': int, 'number': float, 'boolean': bool, 'object': dict, 'array': list}.get(json_type, str)


def _extract_text(content: list[Any]) -> str:
    parts = []
    for block in content:
        if isinstance(block, mcp_types.TextContent):
            parts.append(block.text)
        elif isinstance(block, mcp_types.ImageContent):
            parts.append(f'[image: {block.mimeType}]')
        elif isinstance(block, mcp_types.EmbeddedResource):
            parts.append(getattr(block.resource, 'text', f'[binary resource: {block.resource.uri}]'))
        elif isinstance(block, mcp_types.ResourceLink):
            parts.append(f'[resource: {block.name} ({block.uri})]')
    return '\n'.join(parts) if parts else '(no output)'


def provider_name(server: str, tool: str) -> str:
    original = f'mcp_{server}_{tool}'
    if re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', original):
        return original
    prefix = re.sub(r'[^a-zA-Z0-9_-]', '_', original)[:47]
    return f'{prefix}_{hashlib.sha256(original.encode()).hexdigest()[:16]}'


class MCPToolWrapper(Tool):
    def __init__(self, server_name: str, tool_def: mcp_types.Tool, client: MCPClient, *, config: MCPServerConfig | None = None) -> None:
        self._server_name, self._tool_def, self._client = server_name, tool_def, client
        self.config = config or (client.config if isinstance(getattr(client, 'config', None), MCPServerConfig) else MCPServerConfig(server_name))
        self.name = provider_name(server_name, tool_def.name)
        self.description = f"[{server_name}/{tool_def.name}] " + (tool_def.description or tool_def.name) if config is not None else (tool_def.description or tool_def.name)
        self.category = tool_kind(self.config, tool_def.name) or 'command'
        self.is_concurrency_safe = self.category == 'read' and self.config.integration not in {'playwright', 'serena'}
        self.should_defer = True
        self.params_model = _build_params_model(tool_def.name, tool_def.inputSchema)
        self.capability_fingerprint = fingerprint(self.config, tool_def.inputSchema, tool_def.name)
        self._generation = client.generation if isinstance(getattr(client, 'generation', None), int) else 0

    @property
    def server_name(self) -> str:
        return self._server_name

    @property
    def mcp_tool_name(self) -> str:
        return self._tool_def.name

    @property
    def may_delegate(self) -> bool:
        return self.config.integration == 'custom' or self.category == 'read'

    def get_schema(self) -> dict[str, Any]:
        return {'name': self.name, 'description': self.description, 'input_schema': self._tool_def.inputSchema}

    def resource_ids(self, arguments: dict[str, Any], cwd: str) -> tuple[str, ...]:
        self.validate_arguments(arguments)
        return resources(self.config, self.mcp_tool_name, arguments, cwd)

    def _event(self, directory: Path, event: str, call_id: str, **fields: Any) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / 'events.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'schema_version': 1, 'event': event, 'timestamp': time.time(), 'call_id': call_id, 'server': self.server_name, 'tool': self.mcp_tool_name, 'capability_fingerprint': self.capability_fingerprint, **fields}, ensure_ascii=False) + '\n')

    def _finish(self, directory: Path, call_id: str, **fields: Any) -> bool:
        try:
            self._event(directory, 'call_finished', call_id, **fields)
            return True
        except OSError:
            return False  # call_started remains unresolved; never repeat the remote write.

    async def execute(self, params: BaseModel) -> ToolResult:
        args = params.model_dump(mode='json')
        cwd = get_tool_work_dir()
        try:
            selectors = self.resource_ids(args, str(cwd))
            if fingerprint(self.config, self._tool_def.inputSchema, self.mcp_tool_name) != self.capability_fingerprint:
                raise MCPPolicyError('MCP_CAPABILITY_CHANGED: reconnect and approve the current configuration')
        except ValueError as exc:
            return ToolResult(str(exc), True, execution_status='rejected')
        if not self._client.is_alive:
            if self.config.integration == 'playwright':
                return ToolResult('MCP_BROWSER_CONTEXT_LOST: rediscover the isolated browser and obtain fresh element references', True, execution_status='not_started')
            try:
                await self._client.connect()
            except Exception as exc:
                return ToolResult(f"MCP server '{self.server_name}': {safe_error(exc, self.config)}", True, execution_status='not_started')
        directory = cwd / '.eviforge' / 'mcp'
        generation = getattr(self._client, 'generation', self._generation)
        if isinstance(generation, int) and generation != self._generation:
            try:
                definitions = await self._client.list_tools()
                current = next((item for item in definitions if item.name == self.mcp_tool_name), None)
                if current is None or fingerprint(self.config, current.inputSchema, current.name) != self.capability_fingerprint:
                    return ToolResult('MCP_CAPABILITY_CHANGED: rediscover tools and approve the new schema', True, execution_status='rejected')
                self._generation = generation
            except Exception as exc:
                return ToolResult(f'MCP rediscovery: {safe_error(exc)}', True, execution_status='not_started')
        call_id = uuid.uuid4().hex
        is_write = self.category != 'read'
        if is_write:
            from eviforge.mcp.recovery import unresolved_writes
            try:
                pending = unresolved_writes(directory, self.server_name)
            except (OSError, ValueError):
                return ToolResult('MCP_RECOVERY_REQUIRED: journal cannot be verified', True, execution_status='rejected')
            if pending:
                return ToolResult('MCP_RECOVERY_REQUIRED: reconcile unresolved writes before another write: ' + ', '.join(pending), True, execution_status='rejected')
        try:
            self._event(directory, 'call_started', call_id, arguments_hash=content_digest(args), resources=selectors, write=is_write)
        except OSError:
            return ToolResult('MCP_NOT_STARTED: cannot persist call journal', True, execution_status='not_started')
        try:
            result = await self._client.call_tool(self.mcp_tool_name, args)
        except (Exception, asyncio.CancelledError) as exc:
            self._client._alive = False
            status = 'ambiguous' if is_write else 'failed'
            self._finish(directory, call_id, status=status, error=safe_error(exc, self.config))
            if isinstance(exc, asyncio.CancelledError) and not is_write:
                raise
            return ToolResult(f'MCP call {call_id}: {safe_error(exc, self.config)}; status={status}. Remote writes are never automatically retried.', True, execution_status=status)
        artifacts = []
        try:
            maximum = min(int(self.config.policy.get('max_artifact_bytes', 8_388_608)), 33_554_432)
            for block in result.content:
                payload = None
                mime = None
                if isinstance(block, mcp_types.ImageContent):
                    payload, mime = base64.b64decode(block.data, validate=True), block.mimeType
                elif isinstance(block, mcp_types.EmbeddedResource) and hasattr(block.resource, 'blob'):
                    payload, mime = base64.b64decode(block.resource.blob, validate=True), block.resource.mimeType
                if payload is not None:
                    if len(payload) > maximum:
                        raise ValueError('MCP artifact exceeds configured size limit')
                    digest = hashlib.sha256(payload).hexdigest()
                    artifact_dir = directory / 'artifacts'
                    artifact_dir.mkdir(parents=True, exist_ok=True)
                    path = artifact_dir / f'{call_id}-{len(artifacts)}-{digest}.bin'
                    with path.open('xb') as stream:
                        stream.write(payload)
                    artifacts.append({'path': str(path), 'sha256': digest, 'bytes': len(payload), 'mime_type': mime, 'call_id': call_id})
            structured = redact_data(getattr(result, 'structuredContent', None), self.config)
            output = _extract_text(result.content)
            if structured is not None:
                try:
                    plain = json.loads(output)
                except ValueError:
                    plain = None
                if plain != structured and not (isinstance(structured, dict) and set(structured) == {'result'} and structured['result'] == plain):
                    output += '\n' + json.dumps(structured, ensure_ascii=False)
            if artifacts:
                output += '\nArtifacts: ' + json.dumps(artifacts, ensure_ascii=False)
            output = redact(output, self.config)
            limit = min(int(self.config.policy.get('max_output_chars', 64_000)), 1_000_000)
            if len(output) > limit:
                output = output[:limit] + '\n[output truncated]'
            business_error = False
            if self.config.integration == 'feishu':
                # Official OpenAPI wrappers may return isError=False for code!=0.
                payloads = [structured]
                for block in result.content:
                    if isinstance(block, mcp_types.TextContent):
                        try:
                            payloads.append(json.loads(block.text))
                        except ValueError:
                            pass
                business_error = any(isinstance(item, dict) and 'code' in item and item['code'] not in (0, '0') for item in payloads)
                if self.mcp_tool_name.replace('.', '_') == 'docx_builtin_import':
                    # The upstream builtin sometimes returns job failure without isError/code.
                    business_error = business_error or not any(
                        isinstance(item, dict) and isinstance(item.get('result'), dict)
                        and item['result'].get('job_status') == 0 for item in payloads
                    )
            failed = bool(result.isError) or business_error
            status = 'failed' if failed else 'completed'
            if failed and is_write and self.config.integration in {'github', 'feishu'}:
                status = 'ambiguous'  # A nested HTTP error is not proof that a write had no effect.
            if not self._finish(directory, call_id, status=status, artifacts=artifacts):
                return ToolResult(output + '\nMCP receipt could not be persisted; do not replay this write.', True, structured, tuple(artifacts), 'completed_unarchived')
            return ToolResult(output, failed, structured, tuple(artifacts), status)
        except Exception as exc:
            self._finish(directory, call_id, status='completed_unarchived', error=safe_error(exc))
            return ToolResult(f'MCP returned a result, but archival failed ({type(exc).__name__}); do not replay this write.', True, execution_status='completed_unarchived')

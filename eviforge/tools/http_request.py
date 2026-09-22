"""HTTP with explicit origin scopes, checked before every request/redirect.

This is an application tool boundary, not a subprocess or DNS sandbox.
"""
from __future__ import annotations

import json
from urllib.parse import urljoin, urlsplit

import httpx
from pydantic import BaseModel, Field, field_validator

from eviforge.tools.base import Tool, ToolResult


def network_origin(url: str) -> str:
    """Canonical scheme/IDNA host/effective port; credentials are forbidden."""
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Only absolute HTTP(S) URLs are supported")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URL credentials are not supported")
    host = parsed.hostname.rstrip(".").encode("idna").decode("ascii").lower()
    if any(char in host for char in ("*", "\\", "%", " ", "\t", "\r", "\n")):
        raise ValueError("Invalid network host")
    port = parsed.port if parsed.port is not None else (443 if parsed.scheme.lower() == "https" else 80)
    if not 1 <= port <= 65535:
        raise ValueError("Network port must be between 1 and 65535")
    if ":" in host:
        host = f"[{host}]"
    return f"{parsed.scheme.lower()}://{host}:{port}"


def normalize_host_scope(value: str) -> str:
    value = value if "://" in value else f"https://{value}"
    parsed = urlsplit(value)
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("Network scope must be an origin, without a path/query/fragment")
    return network_origin(value)


class HttpRequestParams(BaseModel):
    url: str
    allowed_hosts: list[str] = Field(min_length=1, description="Exact approved origins, e.g. https://example.com; every redirect is checked")
    method: str = "GET"
    headers: dict[str, str] = Field(default_factory=dict)
    body: str | None = Field(default=None, max_length=1_000_000)
    timeout: float = Field(default=30, gt=0, le=120)
    max_redirects: int = Field(default=5, ge=0, le=10)
    max_bytes: int = Field(default=100_000, gt=0, le=1_000_000)

    @field_validator("url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        network_origin(value)
        return value

    @field_validator("allowed_hosts")
    @classmethod
    def valid_hosts(cls, values: list[str]) -> list[str]:
        return sorted({normalize_host_scope(value) for value in values})

    @field_validator("method")
    @classmethod
    def valid_method(cls, value: str) -> str:
        value = value.upper()
        if value not in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}:
            raise ValueError("Unsupported HTTP method")
        return value

    @field_validator("headers")
    @classmethod
    def safe_headers(cls, value: dict[str, str]) -> dict[str, str]:
        if any(name.lower() in {"host", "proxy-authorization", "connection", "transfer-encoding", "content-length"} for name in value):
            raise ValueError("Transport/Host override headers are not allowed")
        return value


class HttpRequest(Tool):
    name = "HttpRequest"
    description = "Make an HTTP request only to explicit allowed origins; redirects are checked and cross-origin credentials removed."
    params_model = HttpRequestParams
    category = "command"

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    async def execute(self, params: HttpRequestParams) -> ToolResult:
        url, method, body = params.url, params.method, params.body
        headers = dict(params.headers)
        scopes = set(params.allowed_hosts)
        try:
            async with httpx.AsyncClient(transport=self._transport, follow_redirects=False, trust_env=False, timeout=params.timeout) as client:
                for hop in range(params.max_redirects + 1):
                    origin = network_origin(url)
                    if origin not in scopes:
                        return ToolResult(f"NETWORK_SCOPE_DENIED: {origin}", is_error=True)
                    async with client.stream(method, url, headers=headers, content=body) as response:
                        if response.status_code in {301, 302, 303, 307, 308} and "location" in response.headers:
                            if hop >= params.max_redirects:
                                return ToolResult("REDIRECT_LIMIT: too many redirects", is_error=True)
                            target = urljoin(url, response.headers["location"])
                            target_origin = network_origin(target)
                            if target_origin not in scopes:
                                return ToolResult(f"NETWORK_SCOPE_DENIED: redirect to {target_origin}", is_error=True)
                            if target_origin != origin:
                                headers = {key: value for key, value in headers.items() if key.lower() not in {"authorization", "cookie"}}
                                client.cookies.clear()
                            if response.status_code == 303 and method != "HEAD" or response.status_code in {301, 302} and method == "POST":
                                method, body = "GET", None
                            url = target
                            continue
                        chunks = bytearray()
                        truncated = False
                        async for chunk in response.aiter_bytes():
                            remaining = params.max_bytes - len(chunks)
                            chunks.extend(chunk[:remaining])
                            if len(chunk) > remaining:
                                truncated = True
                                break
                        return ToolResult(json.dumps({"url": str(response.url), "status": response.status_code, "body": chunks.decode("utf-8", errors="replace"), "truncated": truncated}, ensure_ascii=False), is_error=response.status_code >= 400)
        except (ValueError, httpx.HTTPError) as exc:
            return ToolResult(f"HTTP_REQUEST_FAILED: {exc}", is_error=True)
        return ToolResult("HTTP_REQUEST_FAILED: no response", is_error=True)

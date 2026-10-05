"""Network guard for the isolated official Playwright MCP child.

The upstream --allowed-origins option alone does not protect redirects. This
adapter intercepts context requests, refuses redirects, blocks workers and
websockets, and refuses flags that attach a personal/remote browser context.
It is a browser request boundary, not an OS/process sandbox.
"""
from __future__ import annotations

import json
from pathlib import Path

from eviforge.config import MCPServerConfig, ConfigError
from eviforge.mcp.policy import origin
from eviforge.permissions.capabilities import content_digest


def guarded_args(config: MCPServerConfig, args: list[str], cwd: Path) -> list[str]:
    blocked = {"--extension", "--cdp-endpoint", "--endpoint", "--storage-state", "--user-data-dir", "--init-page", "--init-script", "--ignore-https-errors", "--no-sandbox", "--allow-unrestricted-file-access", "--shared-browser-context", "--proxy-server", "--proxy-bypass", "--config", "--grant-permissions", "--secrets"}
    if any(argument.split("=", 1)[0] in blocked for argument in args):
        raise ConfigError("Playwright configuration bypasses the isolated guarded context")
    origins = config.policy.get("origins", [])
    if not origins:
        raise ConfigError("Playwright requires explicit test origins")
    for value in origins:
        origin(value)
    guard = """'use strict';
module.exports.default = async ({page}) => {
  const context = page.context();
  if (context.__eviforgeGuardInstalled) return;
  context.__eviforgeGuardInstalled = true;
  const allowed = new Set(ORIGINS.map(value => new URL(value).origin));
  await context.route('**/*', async route => {
    try {
      const url = new URL(route.request().url());
      if (!allowed.has(url.origin) || !['http:', 'https:'].includes(url.protocol)) {
        await route.abort('blockedbyclient'); return;
      }
      const response = await route.fetch({maxRedirects:0, maxRetries:0, timeout:TIMEOUT});
      if (response.status() >= 300 && response.status() < 400) {
        await response.dispose(); await route.abort('blockedbyclient'); return;
      }
      await route.fulfill({response}); await response.dispose();
    } catch (error) { try { await route.abort('failed'); } catch (_) {} }
  });
  if (typeof context.routeWebSocket !== 'function') throw new Error('WebSocket guard unavailable');
  await context.routeWebSocket('**/*', socket => socket.close());
};
""".replace("ORIGINS", json.dumps(origins)).replace("TIMEOUT", str(int(config.call_timeout_seconds * 1000)))
    directory = cwd.resolve() / ".eviforge/mcp/browser-guards"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{content_digest({'origins': origins, 'guard': guard})}.cjs"
    path.write_text(guard, encoding="utf-8")
    return [*args, "--isolated", "--block-service-workers", "--no-webmcp", "--init-page", str(path)]

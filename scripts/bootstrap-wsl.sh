#!/usr/bin/env bash
set -euo pipefail

if ! grep -qi microsoft /proc/sys/kernel/osrelease 2>/dev/null; then
  echo "error: this bootstrap script must run inside WSL" >&2
  exit 2
fi

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
environment_path="${UV_PROJECT_ENVIRONMENT:-${HOME}/.cache/eviforge/venv}"

if command -v uv >/dev/null 2>&1; then
  uv_bin="$(command -v uv)"
elif [[ -x "${HOME}/.local/bin/uv" ]]; then
  uv_bin="${HOME}/.local/bin/uv"
else
  echo "error: uv is not installed in WSL" >&2
  echo "see docs/WSL_USAGE.md for the official installation commands" >&2
  exit 3
fi

cd -- "${project_root}"
echo "project: ${project_root}"
echo "uv: $(${uv_bin} --version)"
echo "environment: ${environment_path}"

UV_PROJECT_ENVIRONMENT="${environment_path}" \
  "${uv_bin}" sync --locked --group dev

export VIRTUAL_ENV="${environment_path}"
export PATH="${environment_path}/bin:${PATH}"
"${environment_path}/bin/python" -c \
  'import mewcode, anthropic, openai, textual, pydantic, mcp; print("imports: ok")'
"${environment_path}/bin/eviforge" --help >/dev/null
echo "bootstrap: ok"

#!/usr/bin/env bash
set -euo pipefail

if ! grep -qi microsoft /proc/sys/kernel/osrelease 2>/dev/null; then
  echo "error: this verification script must run inside WSL" >&2
  exit 2
fi

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
environment_path="${UV_PROJECT_ENVIRONMENT:-${HOME}/.cache/eviforge/venv}"
python_bin="${environment_path}/bin/python"

if [[ ! -x "${python_bin}" ]]; then
  echo "error: Linux environment not found at ${environment_path}" >&2
  echo "run: bash scripts/bootstrap-wsl.sh" >&2
  exit 3
fi

cd -- "${project_root}"
export UV_PROJECT_ENVIRONMENT="${environment_path}"
export VIRTUAL_ENV="${environment_path}"
export PATH="${environment_path}/bin:${PATH}"
"${python_bin}" -m compileall -q mewcode tests evals
"${python_bin}" -m pytest -q -p no:cacheprovider
echo "WSL verification: ok"

"""Trusted, shell-free execution of pre-declared verifier argv vectors."""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from .models import CommandEvidence, CommandStatus, VerifierSpec
from .store import EvidenceStore


class TrustedArgvRunner:
    """Execute verifier specs with ``create_subprocess_exec`` (never a shell)."""

    def __init__(self, store: EvidenceStore, *, terminate_grace_seconds: float = 2.0) -> None:
        self.store = store
        self.terminate_grace_seconds = terminate_grace_seconds

    async def run(
        self,
        spec: VerifierSpec,
        *,
        repo_root: str | Path,
        workspace_diff_sha256: str,
    ) -> CommandEvidence:
        root = Path(repo_root).expanduser().resolve(strict=True)
        started_at = datetime.now(timezone.utc)
        started_clock = time.perf_counter()
        stdout = b""
        stderr = b""
        exit_code: int | None = None
        timed_out = False
        error: str | None = None

        try:
            candidate = Path(spec.cwd)
            cwd = candidate.resolve(strict=True) if candidate.is_absolute() else (root / candidate).resolve(strict=True)
            cwd.relative_to(root)
            if not cwd.is_dir():
                raise ValueError("verifier cwd is not a directory")
        except (OSError, ValueError) as exc:
            cwd = root
            error = f"blocked verifier cwd {spec.cwd!r}: {exc}"
            status = CommandStatus.BLOCKED
        else:
            env = {**os.environ, **spec.env}
            try:
                process = await asyncio.create_subprocess_exec(
                    *spec.argv,
                    cwd=str(cwd),
                    env=env,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                communication = asyncio.create_task(process.communicate())
                try:
                    stdout, stderr = await asyncio.wait_for(
                        asyncio.shield(communication), timeout=spec.timeout_seconds
                    )
                except TimeoutError:
                    timed_out = True
                    try:
                        process.terminate()
                    except ProcessLookupError:
                        pass
                    try:
                        await asyncio.wait_for(process.wait(), timeout=self.terminate_grace_seconds)
                    except TimeoutError:
                        process.kill()
                        await process.wait()
                    stdout, stderr = await communication
                exit_code = process.returncode
                status = (
                    CommandStatus.PASS
                    if not timed_out and exit_code in spec.expected_exit_codes
                    else CommandStatus.FAIL
                )
                if timed_out:
                    error = f"verifier exceeded {spec.timeout_seconds:g}s timeout"
            except (OSError, ValueError) as exc:
                status = CommandStatus.BLOCKED
                error = f"unable to start verifier: {exc}"

        finished_at = datetime.now(timezone.utc)
        stdout_ref = self.store.put_artifact(stdout, media_type="text/plain; charset=utf-8")
        stderr_ref = self.store.put_artifact(stderr, media_type="text/plain; charset=utf-8")
        return CommandEvidence(
            verifier_id=spec.verifier_id,
            status=status,
            argv=spec.argv,
            cwd=str(cwd),
            expected_exit_codes=spec.expected_exit_codes,
            exit_code=exit_code,
            timed_out=timed_out,
            error=error,
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=(time.perf_counter() - started_clock) * 1000,
            workspace_diff_sha256=workspace_diff_sha256,
            stdout=stdout_ref,
            stderr=stderr_ref,
            stdout_preview=stdout.decode("utf-8", errors="replace")[:4000],
            stderr_preview=stderr.decode("utf-8", errors="replace")[:4000],
        )

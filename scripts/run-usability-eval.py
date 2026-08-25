#!/usr/bin/env python3
"""Run EviForge's zero-cost, deterministic release acceptance evaluation.

This runner intentionally does not contact a live model endpoint.  It records
each real command, exit status, raw stream and elapsed time so that reported
results can be audited without confusing fixture success with Provider
performance.
"""

from __future__ import annotations

import argparse
import atexit
import hashlib
import json
import os
import platform
import re
import statistics
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Sequence


DEFAULT_REPETITIONS = 5
SCHEMA_VERSION = 1
BASELINE_SOURCE = "docs/USABILITY_OPTIMIZATION_PLAN.md#92-核心指标"


@dataclass(frozen=True)
class Check:
    check_id: str
    argv_factory: Callable[[int], list[str]]
    timeout_seconds: float = 120.0


class _FakeSSEServer:
    """Loopback-only Chat Completions fixture with credential-header checks."""

    def __init__(self) -> None:
        self._requests: list[dict[str, object]] = []
        self._lock = threading.Lock()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        if self._httpd is None:
            raise RuntimeError("fake SSE server has not started")
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}/v1"

    @property
    def requests(self) -> tuple[dict[str, object], ...]:
        with self._lock:
            return tuple(dict(item) for item in self._requests)

    def start(self) -> None:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, _format: str, *_args: object) -> None:
                return

            def _json_error(self, status: int, message: str) -> None:
                body = json.dumps(
                    {"error": {"message": message, "type": "invalid_request_error"}}
                ).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)
                self.close_connection = True

            def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                try:
                    content_length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    content_length = -1
                body = self.rfile.read(content_length) if 0 <= content_length <= 1_000_000 else b""
                try:
                    payload = json.loads(body)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    payload = None
                authorization_present = bool(self.headers.get("Authorization"))
                x_api_key_present = bool(self.headers.get("X-Api-Key"))
                protocol_valid = (
                    self.path == "/v1/chat/completions"
                    and isinstance(payload, dict)
                    and payload.get("model") == "deterministic-fixture-model"
                    and payload.get("stream") is True
                    and payload.get("stream_options") == {"include_usage": True}
                )
                request_record = {
                    "sequence": len(owner.requests) + 1,
                    "path": self.path,
                    "method": "POST",
                    "body_sha256": hashlib.sha256(body).hexdigest(),
                    "content_type": self.headers.get("Content-Type"),
                    "authorization_present": authorization_present,
                    "x_api_key_present": x_api_key_present,
                    "protocol_valid": protocol_valid,
                }
                with owner._lock:
                    owner._requests.append(request_record)
                if authorization_present or x_api_key_present:
                    self._json_error(400, "credential header forbidden for auth:none fixture")
                    return
                if not protocol_valid:
                    self._json_error(400, "unexpected Chat Completions request")
                    return

                chunks = (
                    {
                        "id": "chatcmpl-fixture",
                        "object": "chat.completion.chunk",
                        "created": 0,
                        "model": "deterministic-fixture-model",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"role": "assistant", "content": "OK"},
                                "logprobs": None,
                                "finish_reason": None,
                            }
                        ],
                    },
                    {
                        "id": "chatcmpl-fixture",
                        "object": "chat.completion.chunk",
                        "created": 0,
                        "model": "deterministic-fixture-model",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {},
                                "logprobs": None,
                                "finish_reason": "stop",
                            }
                        ],
                    },
                    {
                        "id": "chatcmpl-fixture",
                        "object": "chat.completion.chunk",
                        "created": 0,
                        "model": "deterministic-fixture-model",
                        "choices": [],
                        "usage": {
                            "prompt_tokens": 4,
                            "completion_tokens": 1,
                            "total_tokens": 5,
                        },
                    },
                )
                payload_bytes = b"".join(
                    f"data: {json.dumps(chunk, separators=(',', ':'))}\n\n".encode("utf-8")
                    for chunk in chunks
                ) + b"data: [DONE]\n\n"
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Content-Length", str(len(payload_bytes)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(payload_bytes)
                self.wfile.flush()
                self.close_connection = True

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            name="eviforge-fake-sse",
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        httpd, thread = self._httpd, self._thread
        if httpd is None:
            return
        self._httpd = None
        self._thread = None
        httpd.shutdown()
        httpd.server_close()
        if thread is not None:
            thread.join(timeout=5)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _json_dump(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _run_metadata_command(
    argv: Sequence[str], *, cwd: Path
) -> tuple[int, str, str]:
    try:
        completed = subprocess.run(
            list(argv),
            cwd=cwd,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, "", f"{type(exc).__name__}: {exc}"
    return completed.returncode, completed.stdout.strip(), completed.stderr.strip()


def _git_value(repo_root: Path, *args: str) -> str | None:
    code, stdout, _ = _run_metadata_command(["git", *args], cwd=repo_root)
    return stdout if code == 0 and stdout else None


def _git_status(repo_root: Path) -> tuple[bool, tuple[str, ...]]:
    code, stdout, _ = _run_metadata_command(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=repo_root,
    )
    lines = tuple(line for line in stdout.splitlines() if line)
    return code == 0, lines


def _safe_revision(value: str) -> str:
    result = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.")
    return result[:80] or "unversioned"


def _absolute_executable(value: str) -> str:
    """Return an absolute command path without dereferencing venv symlinks."""

    return str(Path(value).expanduser().absolute())


def _write_fixtures(
    fixtures_dir: Path, *, provider_base_url: str
) -> tuple[Path, Path, dict[str, str]]:
    fixtures_dir.mkdir(parents=True, exist_ok=False)
    config = {
        "providers": [
            {
                "name": "offline-fixture",
                "protocol": "openai-compat",
                "base_url": provider_base_url,
                "model": "deterministic-fixture-model",
                "auth": "none",
            }
        ],
        "permission_mode": "plan",
        "mcp_servers": [],
        "hooks": [],
    }
    graph = {
        "nodes": [
            {
                "node_id": "offline-inspect",
                "role": "explorer",
                "objective": "Validate a deterministic graph without execution",
                "acceptance_criteria": [
                    {
                        "criterion_id": "fixture-ok",
                        "description": "The fixture verifier is deterministic",
                        "verifier": "deterministic",
                        "verifier_argv": [
                            sys.executable,
                            "-c",
                            "raise SystemExit(0)",
                        ],
                        "timeout_seconds": 10,
                        "blocking": True,
                    }
                ],
                "artifact_contract": {
                    "required_inputs": [],
                    "required_outputs": ["validation-report"],
                },
                "token_budget": 16,
                "timeout_seconds": 30,
                "predicted_write_set": [],
            }
        ]
    }
    config_path = fixtures_dir / "config.yaml"
    graph_path = fixtures_dir / "task-graph.json"
    config_path.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    graph_path.write_text(
        json.dumps(graph, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    digests = {
        "config.yaml": hashlib.sha256(config_path.read_bytes()).hexdigest(),
        "task-graph.json": hashlib.sha256(graph_path.read_bytes()).hexdigest(),
    }
    return config_path, graph_path, digests


def _parse_json_object(stdout: str) -> dict[str, object] | None:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _validate_result(
    check_id: str,
    *,
    exit_code: int,
    stdout: str,
    junit_path: Path | None,
) -> tuple[bool, str]:
    if exit_code != 0:
        return False, f"process exited with {exit_code}"
    if check_id == "cli_help":
        ok = "usage:" in stdout.casefold() and "eviforge" in stdout.casefold()
        return ok, "help contains usage and command name" if ok else "invalid help output"
    if check_id == "cli_version":
        ok = re.search(r"\bEviForge\s+\d+\.\d+\.\d+", stdout) is not None
        return ok, "version follows the public semantic-version form" if ok else "invalid version output"
    if check_id in {"config_check", "doctor"}:
        payload = _parse_json_object(stdout)
        ok = payload is not None and payload.get("ok") is True
        return ok, "diagnostic JSON reports ok=true" if ok else "invalid or failing diagnostic JSON"
    if check_id == "dag_validate":
        payload = _parse_json_object(stdout)
        ok = payload is not None and payload.get("valid") is True
        return ok, "DAG JSON reports valid=true" if ok else "invalid DAG validation JSON"
    if check_id == "provider_fake_sse":
        payload = _parse_json_object(stdout)
        ok = (
            payload is not None
            and payload.get("status") == "ok"
            and payload.get("provider") == "offline-fixture"
            and payload.get("model") == "deterministic-fixture-model"
            and payload.get("response_preview") == "OK"
            and payload.get("usage") == {"input_tokens": 4, "output_tokens": 1}
        )
        return ok, "local fake SSE passed SDK request/stream validation" if ok else "invalid fake SSE result"
    if check_id == "release_contract_tests":
        if junit_path is None or not junit_path.is_file():
            return False, "pytest did not produce JUnit XML"
        try:
            root = ET.parse(junit_path).getroot()
        except ET.ParseError as exc:
            return False, f"invalid JUnit XML: {exc}"
        tests = int(root.attrib.get("tests", "0"))
        if root.tag == "testsuites" and tests == 0:
            tests = sum(int(suite.attrib.get("tests", "0")) for suite in root.findall("testsuite"))
        ok = tests > 0
        return ok, f"pytest emitted {tests} passing release-contract test case(s)" if ok else "JUnit XML has no tests"
    return True, "exit code is zero"


def _run_check(
    check: Check,
    *,
    repetition: int,
    sequence: int,
    repo_root: Path,
    run_dir: Path,
    environment: dict[str, str],
) -> tuple[dict[str, object], Path | None]:
    argv = check.argv_factory(repetition)
    stdout_path = run_dir / "stdout" / f"r{repetition:02d}-{check.check_id}.txt"
    stderr_path = run_dir / "stderr" / f"r{repetition:02d}-{check.check_id}.txt"
    junit_path = (
        run_dir / "stdout" / f"r{repetition:02d}-release-contract.junit.xml"
        if check.check_id == "release_contract_tests"
        else None
    )
    started = _utc_now()
    start_ns = time.perf_counter_ns()
    timed_out = False
    try:
        completed = subprocess.run(
            argv,
            cwd=repo_root,
            env=environment,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=check.timeout_seconds,
            check=False,
        )
        exit_code = completed.returncode
        stdout = completed.stdout
        stderr = completed.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        exit_code = 124
        stdout = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        stderr += f"\nTIMEOUT after {check.timeout_seconds:.1f}s\n"
    except OSError as exc:
        exit_code = 127
        stdout = ""
        stderr = f"{type(exc).__name__}: {exc}\n"
    duration_ms = round((time.perf_counter_ns() - start_ns) / 1_000_000, 3)
    stdout_path.write_text(stdout, encoding="utf-8")
    stderr_path.write_text(stderr, encoding="utf-8")
    success, assertion = _validate_result(
        check.check_id,
        exit_code=exit_code,
        stdout=stdout,
        junit_path=junit_path,
    )
    record: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "sequence": sequence,
        "repetition": repetition,
        "check_id": check.check_id,
        "argv": argv,
        "cwd": str(repo_root),
        "started_at": started.isoformat().replace("+00:00", "Z"),
        "duration_ms": duration_ms,
        "timeout_seconds": check.timeout_seconds,
        "timed_out": timed_out,
        "exit_code": exit_code,
        "success": success,
        "assertion": assertion,
        "stdout": stdout_path.relative_to(run_dir).as_posix(),
        "stderr": stderr_path.relative_to(run_dir).as_posix(),
    }
    return record, junit_path


def _append_command(path: Path, record: dict[str, object]) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def _merge_junit(paths: Sequence[Path], destination: Path) -> None:
    merged = ET.Element("testsuites", {"name": "eviforge-usability-eval"})
    totals: dict[str, float] = {
        "tests": 0,
        "failures": 0,
        "errors": 0,
        "skipped": 0,
        "time": 0.0,
    }
    for repetition, path in enumerate(paths, start=1):
        if not path.is_file():
            suite = ET.Element(
                "testsuite",
                {"name": f"repeat-{repetition:02d}", "tests": "1", "failures": "1"},
            )
            case = ET.SubElement(suite, "testcase", {"name": "junit-artifact-present"})
            ET.SubElement(case, "failure", {"message": "JUnit artifact missing"})
            suites = [suite]
        else:
            root = ET.parse(path).getroot()
            suites = list(root.findall("testsuite")) if root.tag == "testsuites" else [root]
        for suite in suites:
            suite.set("name", f"repeat-{repetition:02d}:{suite.get('name', 'pytest')}")
            for field in ("tests", "failures", "errors", "skipped"):
                totals[field] += int(suite.get(field, "0"))
            totals["time"] += float(suite.get("time", "0") or 0)
            merged.append(suite)
    for field, value in totals.items():
        merged.set(field, f"{value:.6f}" if field == "time" else str(int(value)))
    ET.indent(merged, space="  ")
    ET.ElementTree(merged).write(destination, encoding="utf-8", xml_declaration=True)


def _filter_generated_status(
    status: Sequence[str], *, repo_root: Path, output_root: Path
) -> tuple[str, ...]:
    try:
        relative = output_root.resolve().relative_to(repo_root.resolve()).as_posix().rstrip("/")
    except ValueError:
        return tuple(status)
    prefixes = (relative, f"{relative}/")
    result: list[str] = []
    for line in status:
        path_part = line[3:].strip().strip('"').replace("\\", "/") if len(line) > 3 else line
        if path_part == prefixes[0] or path_part.startswith(prefixes[1]):
            continue
        result.append(line)
    return tuple(result)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run five or more repetitions of EviForge's deterministic, offline "
            "release checks and preserve auditable raw evidence."
        )
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path("eval-results"),
        help="Artifact root (default: eval-results)",
    )
    parser.add_argument(
        "--repetitions",
        type=int,
        default=DEFAULT_REPETITIONS,
        help="Number of repetitions; must be at least 5",
    )
    parser.add_argument(
        "--python",
        dest="python_executable",
        default=sys.executable,
        help="Python executable from the locked project environment",
    )
    parser.add_argument(
        "--revision",
        default=None,
        help="Override the revision directory when Git metadata is unavailable",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.repetitions < DEFAULT_REPETITIONS:
        raise SystemExit("--repetitions must be at least 5")

    repo_root = Path(__file__).resolve().parents[1]
    git_available_before, status_before = _git_status(repo_root)
    git_sha = _git_value(repo_root, "rev-parse", "HEAD")
    revision = _safe_revision(args.revision or git_sha or "unversioned")
    created_at = _utc_now()
    timestamp_dir = created_at.strftime("%Y%m%dT%H%M%S.%fZ")
    output_root = args.output_root
    if not output_root.is_absolute():
        output_root = repo_root / output_root
    run_dir = output_root / revision / timestamp_dir
    (run_dir / "stdout").mkdir(parents=True, exist_ok=False)
    (run_dir / "stderr").mkdir(parents=True, exist_ok=False)
    fake_sse = _FakeSSEServer()
    fake_sse.start()
    atexit.register(fake_sse.close)
    config_path, graph_path, fixture_digests = _write_fixtures(
        run_dir / "fixtures", provider_base_url=fake_sse.base_url
    )

    # On POSIX, ``.venv/bin/python`` is commonly a symlink to the base
    # interpreter.  Resolving it discards the virtual-environment path, so
    # child commands can no longer import the locked dependencies.
    python = _absolute_executable(args.python_executable)
    command_log = run_dir / "commands.jsonl"
    command_log.touch()
    environment = dict(os.environ)
    for credential_name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        environment.pop(credential_name, None)
    environment.update(
        {
            "EVIFORGE_EVAL_MODE": "offline-fixture",
            "LOCALAPPDATA": str(run_dir / "control-plane"),
            "NO_COLOR": "1",
            "PYTHONUTF8": "1",
        }
    )

    junit_for_repetition = {
        repetition: run_dir / "stdout" / f"r{repetition:02d}-release-contract.junit.xml"
        for repetition in range(1, args.repetitions + 1)
    }
    checks = (
        Check("cli_help", lambda _r: [python, "-m", "mewcode", "--help"]),
        Check("cli_version", lambda _r: [python, "-m", "mewcode", "--version"]),
        Check(
            "config_check",
            lambda _r: [
                python,
                "-m",
                "mewcode",
                "--config",
                str(config_path),
                "config",
                "check",
                "--json",
            ],
        ),
        Check(
            "doctor",
            lambda _r: [
                python,
                "-m",
                "mewcode",
                "--config",
                str(config_path),
                "doctor",
                "--json",
            ],
        ),
        Check(
            "dag_validate",
            lambda _r: [
                python,
                "-m",
                "mewcode",
                "dag",
                "validate",
                str(graph_path),
                "--json",
            ],
        ),
        Check(
            "provider_fake_sse",
            lambda _r: [
                python,
                "-m",
                "mewcode",
                "--config",
                str(config_path),
                "provider",
                "test",
                "offline-fixture",
                "--timeout",
                "10",
                "--json",
            ],
            timeout_seconds=30.0,
        ),
        Check(
            "release_contract_tests",
            lambda repetition: [
                python,
                "-m",
                "pytest",
                "-q",
                "tests/test_release_artifacts.py",
                "tests/test_cli_usability.py",
                "tests/test_provider_resilience.py",
                f"--junitxml={junit_for_repetition[repetition]}",
            ],
            timeout_seconds=300.0,
        ),
    )

    environment_record = {
        "schema_version": SCHEMA_VERSION,
        "timestamp": created_at.isoformat().replace("+00:00", "Z"),
        "git_sha": git_sha,
        "git_revision_directory": revision,
        "git_remote_origin": _git_value(repo_root, "remote", "get-url", "origin"),
        "git_metadata_available": git_available_before,
        "git_dirty_before": bool(status_before),
        "git_status_before": list(status_before),
        "platform": platform.platform(),
        "system": platform.system(),
        "machine": platform.machine(),
        "python_version": platform.python_version(),
        "python_executable": python,
        "uv_version": _run_metadata_command(["uv", "--version"], cwd=repo_root)[1] or None,
        "working_directory": str(repo_root),
        "repetitions": args.repetitions,
        "network_required": False,
        "live_provider_executed": False,
        "local_fake_sse_executed": True,
        "local_fake_sse_base_url": fake_sse.base_url,
        "provider": None,
        "model": None,
        "fixture_sha256": fixture_digests,
    }
    _json_dump(run_dir / "environment.json", environment_record)

    records: list[dict[str, object]] = []
    sequence = 0
    for repetition in range(1, args.repetitions + 1):
        for check in checks:
            sequence += 1
            record, _ = _run_check(
                check,
                repetition=repetition,
                sequence=sequence,
                repo_root=repo_root,
                run_dir=run_dir,
                environment=environment,
            )
            records.append(record)
            _append_command(command_log, record)

    fake_sse.close()
    fake_requests = fake_sse.requests
    with (run_dir / "provider-requests.jsonl").open(
        "w", encoding="utf-8", newline="\n"
    ) as handle:
        for request in fake_requests:
            handle.write(json.dumps(request, ensure_ascii=False, sort_keys=True) + "\n")

    _merge_junit(
        [junit_for_repetition[index] for index in range(1, args.repetitions + 1)],
        run_dir / "junit.xml",
    )
    git_available_after, status_after = _git_status(repo_root)
    comparable_before = _filter_generated_status(
        status_before, repo_root=repo_root, output_root=output_root
    )
    comparable_after = _filter_generated_status(
        status_after, repo_root=repo_root, output_root=output_root
    )
    workspace_unchanged = (
        git_available_before
        and git_available_after
        and comparable_before == comparable_after
    )

    by_check: list[dict[str, object]] = []
    for check in checks:
        samples = [record for record in records if record["check_id"] == check.check_id]
        successful = sum(bool(record["success"]) for record in samples)
        durations = [float(record["duration_ms"]) for record in samples]
        by_check.append(
            {
                "check_id": check.check_id,
                "sample_size": len(samples),
                "successful_samples": successful,
                "success_rate_percent": round(successful / len(samples) * 100, 3),
                "median_duration_ms": round(statistics.median(durations), 3),
                "fixture_or_live": "fixture",
            }
        )
    successful_repetitions = sum(
        all(
            bool(record["success"])
            for record in records
            if record["repetition"] == repetition
        )
        for repetition in range(1, args.repetitions + 1)
    )
    all_commands_passed = all(bool(record["success"]) for record in records)
    fake_sse_integrity = (
        len(fake_requests) == args.repetitions
        and all(bool(item["protocol_valid"]) for item in fake_requests)
        and not any(
            bool(item["authorization_present"] or item["x_api_key_present"])
            for item in fake_requests
        )
    )
    overall_passed = all_commands_passed and workspace_unchanged and fake_sse_integrity
    metrics = {
        "schema_version": SCHEMA_VERSION,
        "metric_id": "deterministic_release_acceptance_success",
        "baseline_or_after": "after",
        "sample_size": args.repetitions,
        "value": successful_repetitions,
        "unit": "successful_repetitions",
        "fixture_or_live": "fixture",
        "provider/model": None,
        "git_sha": git_sha,
        "timestamp": created_at.isoformat().replace("+00:00", "Z"),
        "total_command_samples": len(records),
        "successful_command_samples": sum(bool(record["success"]) for record in records),
        "workspace_unchanged": workspace_unchanged,
        "fake_sse_integrity": fake_sse_integrity,
        "workspace_status_before": list(comparable_before),
        "workspace_status_after": list(comparable_after),
        "passed": overall_passed,
        "checks": by_check,
        "baseline": {
            "value": None,
            "status": "not_measured",
            "source": BASELINE_SOURCE,
            "note": (
                "The optimization plan records clean-clone Quick Start as pending measurement; "
                "no before-value or improvement percentage is inferred."
            ),
        },
        "live_provider": {
            "executed": False,
            "provider": None,
            "model": None,
            "usage": None,
            "note": "Live Provider smoke is opt-in and is not part of this zero-cost evaluation.",
        },
        "local_fake_sse": {
            "executed": True,
            "protocol": "openai-compat",
            "model": "deterministic-fixture-model",
            "request_samples": len(fake_requests),
            "valid_request_samples": sum(bool(item["protocol_valid"]) for item in fake_requests),
            "credential_header_leaks": sum(
                bool(item["authorization_present"] or item["x_api_key_present"])
                for item in fake_requests
            ),
            "raw_requests": "provider-requests.jsonl",
        },
    }
    _json_dump(run_dir / "metrics.json", metrics)

    failed = [
        f"r{record['repetition']:02d}/{record['check_id']}: {record['assertion']}"
        for record in records
        if not record["success"]
    ]
    status_label = "PASS" if overall_passed else "FAIL"
    summary_lines = [
        "# EviForge deterministic usability evaluation",
        "",
        f"- Result: **{status_label}**",
        f"- Revision: `{git_sha or revision}`",
        f"- Repetitions: {args.repetitions}",
        f"- Successful repetitions: {successful_repetitions}/{args.repetitions}",
        f"- Successful command samples: {metrics['successful_command_samples']}/{len(records)}",
        f"- Workspace unchanged outside the artifact directory: {workspace_unchanged}",
        f"- Fake SSE request integrity: {fake_sse_integrity}",
        "- Evaluation type: deterministic fixture with local fake SSE; zero live Provider calls",
        "",
        "## Measured checks",
        "",
        "| Check | Samples passed | Median duration (ms) |",
        "|---|---:|---:|",
    ]
    summary_lines.extend(
        f"| `{item['check_id']}` | {item['successful_samples']}/{item['sample_size']} | {item['median_duration_ms']} |"
        for item in by_check
    )
    summary_lines.extend(
        [
            "",
            "## Baseline and claim boundary",
            "",
            (
                "The source optimization plan marks the clean-clone Quick Start baseline as "
                "unmeasured. This report therefore gives raw after-results only and computes no "
                "before/after improvement percentage."
            ),
            "",
            (
                "The loopback fake SSE validates the real OpenAI-compatible SDK request and stream "
                "parser, including the absence of credential headers for `auth:none`. It is a fixture, "
                "not a live Provider result."
            ),
            "",
            (
                "No live Provider, model latency, token usage, cost, or response-quality result was "
                "collected. A live smoke result must be stored separately with an explicit Provider, "
                "model, date, budget and usage record."
            ),
            "",
            "## Reproduce",
            "",
            "```bash",
            "uv run python scripts/run-usability-eval.py",
            "```",
        ]
    )
    if failed:
        summary_lines.extend(["", "## Failures", ""])
        summary_lines.extend(f"- {item}" for item in failed)
    if not workspace_unchanged:
        summary_lines.extend(
            [
                "",
                "## Workspace delta",
                "",
                "The Git status changed outside the generated artifact directory; compare the raw status arrays in `metrics.json`.",
            ]
        )
    if not fake_sse_integrity:
        summary_lines.extend(
            [
                "",
                "## Fake SSE integrity failure",
                "",
                "Expected exactly one valid, credential-free loopback request per repetition; inspect `provider-requests.jsonl`.",
            ]
        )
    (run_dir / "summary.md").write_text(
        "\n".join(summary_lines) + "\n", encoding="utf-8"
    )
    if not overall_passed:
        print(
            json.dumps(
                {
                    "failed_checks": failed,
                    "workspace_status_before": list(comparable_before),
                    "workspace_status_after": list(comparable_after),
                    "fake_sse_request_samples": len(fake_requests),
                    "fake_sse_integrity": fake_sse_integrity,
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
    print(
        json.dumps(
            {
                "passed": overall_passed,
                "artifact_dir": str(run_dir),
                "successful_repetitions": successful_repetitions,
                "repetitions": args.repetitions,
            },
            ensure_ascii=False,
        )
    )
    return 0 if overall_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

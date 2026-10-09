"""Check an installed wheel outside the checkout, without credentials or network.

Run this script with the Python interpreter in the wheel's environment:
    /path/to/wheel-env/bin/python scripts/check_distribution.py
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import sysconfig
import tempfile


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def check_installed() -> None:
    from importlib.metadata import distribution
    from importlib.resources import files

    import eviforge
    from eviforge.agents.loader import AgentLoader
    from eviforge.skills.directory import load_tool_implementation, parse_tool_json
    from eviforge.skills.loader import SkillLoader

    source_package = Path(__file__).resolve().parents[1] / "eviforge"
    installed_package = Path(eviforge.__file__).resolve().parent
    require(
        not installed_package.is_relative_to(source_package),
        "Imported the source checkout instead of the installed wheel.",
    )
    package = files("eviforge")
    resources = [
        "styles.tcss",
        "hooks/lifecycle_actions.py",
        "schemas/run-result-v1.schema.json",
        "schemas/run-event-v1.schema.json",
        "schemas/dag-graph-v1.schema.json",
        "schemas/dag-output-v1.schema.json",
        "agents/builtins/explore.md",
        "agents/builtins/plan.md",
        "agents/builtins/general-purpose.md",
        "agents/builtins/verification.md",
        "skills/builtins/commit/SKILL.md",
        "skills/builtins/review/SKILL.md",
        "skills/builtins/test/SKILL.md",
        "skills/builtins/backend-interview/SKILL.md",
        "skills/builtins/backend-interview/tool.json",
        "skills/builtins/backend-interview/references/parse_resume.py",
    ]
    for relative_path in resources:
        resource = package.joinpath(*relative_path.split("/"))
        require(resource.is_file(), f"Missing packaged resource: {relative_path}")
        require(bool(resource.read_bytes()), f"Empty packaged resource: {relative_path}")

    agents = AgentLoader(str(Path.cwd()), enable_verification=True).load_all()
    expected_agents = {"Explore", "Plan", "general-purpose", "Verification"}
    require(expected_agents <= agents.keys(), "Packaged Agent definitions did not load.")
    require(
        all(agents[name].source == "builtin" for name in expected_agents),
        "Agent definitions came from external configuration.",
    )
    skills = SkillLoader(str(Path.cwd())).load_all()
    expected_skills = {"commit", "review", "test", "backend-interview"}
    require(expected_skills <= skills.keys(), "Packaged Skill definitions did not load.")
    interview_source = skills["backend-interview"].source_path
    require(interview_source is not None, "The directory Skill has no packaged source.")
    skill_directory = interview_source.parent
    schemas = parse_tool_json(skill_directory / "tool.json")
    require(any(schema.get("name") == "parse_resume" for schema in schemas), "Skill tool.json did not load.")
    require(
        callable(load_tool_implementation(skill_directory / "references", "parse_resume")),
        "The packaged Skill reference implementation did not load.",
    )

    metadata = distribution("eviforge")
    entrypoints = [entry for entry in metadata.entry_points if entry.group == "console_scripts" and entry.name == "eviforge"]
    require(len(entrypoints) == 1, "The wheel must install one eviforge console entry point.")
    require(entrypoints[0].value == "eviforge.__main__:main", "Unexpected console entry point.")
    cli = Path(sysconfig.get_path("scripts")) / ("eviforge.exe" if os.name == "nt" else "eviforge")
    require(cli.is_file(), "The eviforge console command was not installed.")
    for command in ([str(cli), "--help"], [sys.executable, "-I", "-m", "eviforge", "--help"]):
        result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=30)
        require("EviForge" in result.stdout and "usage: eviforge" in result.stdout, "CLI help did not identify EviForge.")
    import json
    result = subprocess.run([str(cli), "schema"], check=True, capture_output=True, text=True, timeout=30)
    require(json.loads(result.stdout)["title"] == "RunResult", "Installed result schema did not load")
    result = subprocess.run([str(cli), "dag", "schema"], check=True, capture_output=True, text=True, timeout=30)
    require(json.loads(result.stdout)["schema_version"] == "1.0", "Installed DAG schema did not load")
    result = subprocess.run([str(cli), "governance", "list"], check=True, capture_output=True, text=True, timeout=30)
    require(json.loads(result.stdout)["ok"], "Installed governance CLI did not work without Provider config")
    result = subprocess.run([str(cli), "mcp", "list"], check=True, capture_output=True, text=True, timeout=30)
    require(json.loads(result.stdout)["servers"] == [], "Installed MCP diagnostics did not work without Provider config")
    import asyncio
    from eviforge.config import AppConfig
    from eviforge.hooks import HookContext, create_hook_engine

    async def check_hooks() -> None:
        engine = create_hook_engine(AppConfig(providers=[]))
        require(engine is not None and len(engine.hooks) == 13, "Packaged default Hook preset did not load")
        context = HookContext(work_dir=str(Path.cwd()), session_id="wheel", turn_id="check", agent_id="main")
        await engine.begin_run(context)
        source = Path("hook-wheel-check.py")
        source.write_text("value = 1\n", encoding="utf-8")
        await engine.run_hooks("session_end", context)
        summary = engine.verification_summary(context)
        require(summary["status"] == "partial" and Path(summary["report_path"]).exists(), "Installed Hook evidence was not generated")
        source.write_text("value = (\n", encoding="utf-8")
        await engine.run_hooks("turn_end", context)
        require(bool(engine.completion_failure(context)), "Installed Hook failed to detect invalid code")

    asyncio.run(check_hooks())
    print(f"EviForge {metadata.version}: installed wheel, resources, loaders and CLI checks passed.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-installed", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.check_installed:
        check_installed()
        return

    with tempfile.TemporaryDirectory(prefix="eviforge-distribution-") as temporary:
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        environment["HOME"] = temporary
        environment["USERPROFILE"] = temporary
        subprocess.run(
            [sys.executable, "-I", str(Path(__file__).resolve()), "--check-installed"],
            cwd=temporary,
            env=environment,
            check=True,
            timeout=60,
        )


if __name__ == "__main__":
    main()

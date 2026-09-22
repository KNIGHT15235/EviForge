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

    import likecc
    from likecc.agents.loader import AgentLoader
    from likecc.skills.directory import load_tool_implementation, parse_tool_json
    from likecc.skills.loader import SkillLoader

    source_package = Path(__file__).resolve().parents[1] / "likecc"
    installed_package = Path(likecc.__file__).resolve().parent
    require(
        not installed_package.is_relative_to(source_package),
        "Imported the source checkout instead of the installed wheel.",
    )
    package = files("likecc")
    resources = [
        "styles.tcss",
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

    metadata = distribution("likecc")
    entrypoints = [entry for entry in metadata.entry_points if entry.group == "console_scripts" and entry.name == "likecc"]
    require(len(entrypoints) == 1, "The wheel must install one likecc console entry point.")
    require(entrypoints[0].value == "likecc.__main__:main", "Unexpected console entry point.")
    cli = Path(sysconfig.get_path("scripts")) / ("likecc.exe" if os.name == "nt" else "likecc")
    require(cli.is_file(), "The likecc console command was not installed.")
    for command in ([str(cli), "--help"], [sys.executable, "-I", "-m", "likecc", "--help"]):
        result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=30)
        require("LikeCC" in result.stdout and "usage: likecc" in result.stdout, "CLI help did not identify LikeCC.")
    print(f"LikeCC {metadata.version}: installed wheel, resources, loaders and CLI checks passed.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-installed", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.check_installed:
        check_installed()
        return

    with tempfile.TemporaryDirectory(prefix="likecc-distribution-") as temporary:
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

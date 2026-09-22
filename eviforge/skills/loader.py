from __future__ import annotations

import importlib.resources
import logging
from pathlib import Path
from typing import Any

from eviforge.skills.parser import SkillDef, SkillParseError, parse_frontmatter, parse_skill_file

log = logging.getLogger(__name__)

PROJECT_SKILLS_DIR = ".eviforge/skills"
USER_SKILLS_DIR = "~/.eviforge/skills"


class SkillLoader:
    def __init__(self, work_dir: str, governance: Any = None) -> None:
        self._work_dir = work_dir
        self._project_dir = Path(work_dir) / PROJECT_SKILLS_DIR
        self._user_dir = Path(USER_SKILLS_DIR).expanduser()
        self._skills: dict[str, SkillDef] = {}
        self._cache: dict[str, SkillDef] = {}
        self.governance = governance


    def load_all(self) -> dict[str, SkillDef]:
        seen: dict[str, SkillDef] = {}

        for skill in self._scan_directory(self._project_dir, "project"):
            if skill.name not in seen:
                seen[skill.name] = skill

        for skill in self._scan_directory(self._user_dir, "user"):
            if skill.name not in seen:
                seen[skill.name] = skill

        for skill in self._load_builtins():
            if skill.name not in seen:
                seen[skill.name] = skill

        self._skills = seen
        self._cache = {k: v for k, v in seen.items()}
        self._sync_governed()
        return self._skills

    def _governed_skill(self, name: str) -> SkillDef | None:
        entry = self.governance.get_published_skill(name)
        if entry is None:
            return None
        meta, body = parse_frontmatter(entry["content"])
        return SkillDef(
            name=name, description=meta["description"],
            prompt_body=(f'<eviforge-governed-skill name="{name}" hash="{entry["content_hash"]}">\n'
                         f'{body}\n</eviforge-governed-skill>\n[end-governed-skill {entry["content_hash"]}]'),
            allowed_tools=meta.get("allowedTools", []), mode=meta.get("mode", "inline"),
            model=meta.get("model"), context=meta.get("context", "full"),
        )

    def _sync_governed(self) -> None:
        if self.governance is None:
            return
        for name in self.governance.managed_skill_names():
            self._cache.pop(name, None)
            skill = self._governed_skill(name)
            if skill is None:
                self._skills.pop(name, None)
            else:
                self._skills[name] = skill

    def refresh_active_skills(self, agent: Any) -> bool:
        if self.governance is None:
            return False
        self._sync_governed()
        changed = False
        managed = self.governance.managed_skill_names()
        recovery = getattr(agent, "recovery_state", None)
        if recovery is not None:
            for name in managed:
                recovery.discard_skill(name)
        for name in list(agent.active_skills):
            if name not in managed:
                continue
            current = self._skills.get(name)
            if current is None:
                del agent.active_skills[name]
                changed = True
            elif not agent.active_skills[name].startswith(current.prompt_body.split("\n", 1)[0] + "\n"):
                agent.active_skills[name] = current.prompt_body
                changed = True
        catalog = self.get_catalog()
        formatted = "Available Skills:\n" + "\n".join(f"- {name}: {description}" for name, description in catalog) if catalog else ""
        if getattr(agent, "_skill_catalog", "") != formatted:
            agent._skill_catalog = formatted
            changed = True
        from eviforge.prompts import build_environment_context
        from eviforge.governance.context import SKILL_BLOCK

        conversations = [getattr(agent, "conversation", None), getattr(agent, "_current_conversation", None)]
        visited = set()
        for conversation in conversations:
            if conversation is None or id(conversation) in visited:
                continue
            visited.add(id(conversation))
            for message in conversation.history:
                if message.role == "user" and not message.tool_uses and not message.tool_results:
                    match = SKILL_BLOCK.fullmatch(message.content)
                    if match and match["name"] in managed:
                        skill = self._skills.get(match["name"])
                        if skill is None:
                            content = f"Governed skill '{match['name']}' was revoked. Its procedure is no longer authorized."
                        elif not message.content.startswith(skill.prompt_body.split("\n", 1)[0] + "\n"):
                            content = skill.prompt_body
                        else:
                            content = message.content
                        if content != message.content:
                            message.content = content
                            conversation.last_input_tokens = conversation.baseline_tokens = conversation.anchor_count = 0
                            changed = True
                if (message.role == "user" and not message.tool_uses and not message.tool_results
                        and message.content.startswith("Current working directory: ")
                        and "\nOperating system: " in message.content.split("\nCurrent time: ", 1)[0]):
                    fresh = build_environment_context(
                        agent.work_dir, agent.active_skills, formatted, getattr(agent, "_agent_catalog", ""),
                    )
                    # Keep the original environment timestamp to avoid gratuitous cache invalidation.
                    content = "\n".join(message.content.split("\n")[:3] + fresh.split("\n")[3:])
                    if message.content != content:
                        message.content = content
                        conversation.last_input_tokens = conversation.baseline_tokens = conversation.anchor_count = 0
                        changed = True
        return changed


    def _scan_directory(self, path: Path, source: str) -> list[SkillDef]:
        results: list[SkillDef] = []
        if not path.is_dir():
            return results

        for entry in sorted(path.iterdir()):
            try:
                if entry.is_file() and entry.suffix == ".md":
                    skill = parse_skill_file(entry)
                    skill.source_path = entry
                    results.append(skill)
                elif entry.is_dir():
                    skill_md = entry / "SKILL.md"
                    if skill_md.is_file():
                        skill = parse_skill_file(skill_md)
                        skill.source_path = skill_md
                        skill.is_directory = True
                        results.append(skill)
            except SkillParseError as e:
                log.warning("Skipping %s skill '%s': %s", source, entry.name, e)

        return results

    def _load_builtins(self) -> list[SkillDef]:
        results: list[SkillDef] = []
        builtins_pkg = importlib.resources.files("eviforge.skills.builtins")

        for resource in builtins_pkg.iterdir():
            skill_md = resource / "SKILL.md" if resource.is_dir() else None
            if skill_md is None or not skill_md.is_file():
                continue
            try:
                raw = skill_md.read_text(encoding="utf-8")
                meta, body = parse_frontmatter(raw)
                from eviforge.skills.parser import _validate_meta
                _validate_meta(meta, f"builtin:{resource.name}")
                source = None
                try:
                    source = Path(str(skill_md))
                except Exception:
                    pass
                skill = SkillDef(
                    name=meta["name"],
                    description=meta["description"],
                    prompt_body=body,
                    allowed_tools=meta.get("allowedTools", []),
                    mode=meta.get("mode", "inline"),
                    model=meta.get("model"),
                    context=meta.get("context", "full"),
                    source_path=source,
                    is_directory=True,
                )
                results.append(skill)
            except (SkillParseError, Exception) as e:
                log.warning("Skipping builtin skill '%s': %s", resource.name, e)

        return results


    def get(self, name: str) -> SkillDef | None:
        if self.governance is not None and name in self.governance.managed_skill_names():
            self._sync_governed()
            return self._skills.get(name)
        skill = self._skills.get(name)
        if skill is None:
            return None

        if skill.source_path is not None:
            try:
                fresh = parse_skill_file(skill.source_path)
                fresh.is_directory = skill.is_directory
                self._skills[name] = fresh
                self._cache[name] = fresh
                return fresh
            except SkillParseError as e:
                log.warning(
                    "Hot-reload failed for skill '%s', using cached version: %s",
                    name, e,
                )
                return self._cache.get(name, skill)

        return skill

    def get_catalog(self) -> list[tuple[str, str]]:
        self._sync_governed()
        return [(s.name, s.description) for s in self._skills.values()]

    def reload(self) -> dict[str, SkillDef]:
        return self.load_all()


    def get_source_label(self, name: str) -> str:
        if self.governance is not None and name in self.governance.managed_skill_names():
            entry = self.governance.get_published_skill(name)
            return f"governed:{entry['scope']}:v{entry['version']}" if entry else "revoked"
        skill = self._skills.get(name)
        if skill is None:
            return "unknown"
        if skill.source_path is None:
            return "builtin"
        path_str = str(skill.source_path)
        if path_str.startswith(str(self._project_dir)):
            return "project"
        if path_str.startswith(str(self._user_dir)):
            return "user"
        return "builtin"

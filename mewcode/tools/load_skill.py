from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from mewcode.tools.base import Tool, ToolResult

if TYPE_CHECKING:
    from mewcode.agent import Agent
    from mewcode.skills.directory import register_skill_tools
    from mewcode.skills.loader import SkillLoader


class LoadSkillParams(BaseModel):
    name: str = Field(description="The name of the skill to load")


class LoadSkill(Tool):
    name = "LoadSkill"
    description = (
        "Load and activate a skill by name. "
        "The skill's SOP will be pinned to the environment context "
        "and any specialized tools will be registered."
    )
    params_model = LoadSkillParams
    category = "read"
    is_concurrency_safe = False
    is_system_tool = True


    def __init__(self) -> None:
        self._loader: SkillLoader | None = None
        self._agent: Agent | None = None


    def set_loader(self, loader: SkillLoader) -> None:
        self._loader = loader

    def set_agent(self, agent: Agent) -> None:
        self._agent = agent


    async def execute(self, params: BaseModel) -> ToolResult:
        assert isinstance(params, LoadSkillParams)

        if self._loader is None or self._agent is None:
            return ToolResult(
                output="Error: LoadSkill not properly initialized",
                is_error=True,
            )

        skill = self._loader.get(params.name)
        if skill is None:
            available = ", ".join(n for n, _ in self._loader.get_catalog())
            return ToolResult(
                output=f"Error: unknown skill '{params.name}'. Available skills: {available}",
                is_error=True,
            )

        if skill.mode == "fork":
            return ToolResult(
                output=(
                    f"Error: skill '{skill.name}' uses fork mode and cannot be "
                    "activated with LoadSkill. Run it through its slash command "
                    f"instead: /{skill.name} [arguments]. This preserves the "
                    "isolated child-Agent execution boundary."
                ),
                is_error=True,
            )

        self._agent.activate_skill(
            skill.name,
            skill.prompt_body,
            allowed_tools=skill.allowed_tools,
        )

        tool_count = 0
        if skill.is_directory and skill.source_path is not None:
            context = getattr(self._agent, "execution_context", None)
            if context is not None and getattr(context, "plan_hash", "unplanned") != "unplanned":
                return ToolResult(
                    output=(
                        f"Skill '{skill.name}' SOP activated, but dynamic Python tools "
                        "were not loaded: reviewed Plans forbid import-time code that "
                        "is not represented in the approved execution manifest."
                    ),
                    is_error=True,
                )
            from mewcode.skills.directory import register_skill_tools
            skill_dir = skill.source_path.parent
            tool_count = register_skill_tools(skill_dir, self._agent.registry)

        parts = [f"Skill '{skill.name}' activated. SOP pinned to environment context."]
        if tool_count > 0:
            parts.append(f"{tool_count} specialized tool(s) registered.")
        return ToolResult(output=" ".join(parts))

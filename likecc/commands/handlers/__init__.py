
from __future__ import annotations

from likecc.commands.handlers.clear import CLEAR_COMMAND
from likecc.commands.handlers.compact import COMPACT_COMMAND
from likecc.commands.handlers.do import DO_COMMAND
from likecc.commands.handlers.help import HELP_COMMAND
from likecc.commands.handlers.mcp import MCP_COMMAND
from likecc.commands.handlers.memory import MEMORY_COMMAND
from likecc.commands.handlers.permission import PERMISSION_COMMAND
from likecc.commands.handlers.plan import PLAN_COMMAND
from likecc.commands.handlers.session import SESSION_COMMAND
from likecc.commands.handlers.skill import SKILL_COMMAND
from likecc.commands.handlers.rewind import REWIND_COMMAND
from likecc.commands.handlers.status import STATUS_COMMAND
from likecc.commands.registry import CommandRegistry


ALL_COMMANDS = [
    HELP_COMMAND,
    COMPACT_COMMAND,
    CLEAR_COMMAND,
    PLAN_COMMAND,
    DO_COMMAND,
    SESSION_COMMAND,
    MCP_COMMAND,
    MEMORY_COMMAND,
    PERMISSION_COMMAND,
    REWIND_COMMAND,
    STATUS_COMMAND,
    SKILL_COMMAND,
]


def register_all_commands(registry: CommandRegistry) -> None:
    for cmd in ALL_COMMANDS:
        registry.register_sync(cmd)


from __future__ import annotations

from eviforge.commands.registry import Command, CommandContext, CommandType


async def handle_memory(ctx: CommandContext) -> None:
    mm = ctx.memory_manager
    if mm is None:
        ctx.ui.add_system_message("记忆管理器未初始化")
        return


    parts = ctx.args.split(None, 1)
    sub = parts[0] if parts else ""

    if sub == "":
        display = mm.get_display_text()
        ctx.ui.add_system_message(display)

    elif sub == "list":
        display = mm.get_display_text()
        ctx.ui.add_system_message(display)

    elif sub == "clear":
        mm.clear()
        if getattr(mm, "governance", None) is not None:
            if ctx.conversation is not None:
                mm.refresh_context(ctx.conversation)
            ctx.ui.add_system_message("已撤销所有记忆；当前生效记忆已清空，审计记录保留。")
        else:
            ctx.ui.add_system_message("所有自动记忆已清空。")

    elif sub == "edit":
        governance = getattr(mm, "governance", None)
        if governance is not None:
            ctx.ui.add_system_message(
                "记忆采用版本审核。编辑候选 Markdown 后，在项目目录运行 import 创建新版本，"
                "再依次 verify、confirm、publish；候选不会立即进入上下文。\n"
                f"项目目录: {governance.work_dir}\n"
                "eviforge governance --scope project import --kind memory --name NAME "
                "--file CANDIDATE.md --source-task TASK --source-trace TRACE\n"
                "用 --scope user 管理跨项目记忆；用 governance show ID 查看内容哈希和审计。\n"
                f"用户数据库: {governance.paths['user']}\n"
                f"项目数据库: {governance.paths['project']}"
            )
        else:
            ctx.ui.add_system_message(
                f"编辑记忆文件：\n"
                f"  用户级: {mm.user_path}\n"
                f"  项目级: {mm.project_path}"
            )

    else:
        ctx.ui.add_system_message(
            "用法: /memory [list | clear | edit]"
        )


MEMORY_COMMAND = Command(
    name="memory",
    description="记忆管理",
    usage="/memory [list | clear | edit]",
    type=CommandType.LOCAL,
    handler=handle_memory,
)

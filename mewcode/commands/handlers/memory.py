
from __future__ import annotations

from mewcode.commands.registry import Command, CommandContext, CommandType


async def handle_memory(ctx: CommandContext) -> None:
    mm = ctx.memory_manager
    if mm is None:
        ctx.ui.add_system_message("记忆管理器未初始化")
        return


    parts = ctx.args.split()
    sub = parts[0] if parts else ""

    if sub == "":
        display = mm.get_display_text()
        ctx.ui.add_system_message(display)

    elif sub == "list":
        if len(parts) > 2 or (len(parts) == 2 and parts[1] not in {"user", "project"}):
            ctx.ui.add_system_message("用法: /memory list [user|project]")
            return
        try:
            display = mm.get_inventory_text(scope=parts[1] if len(parts) == 2 else None)
            ctx.ui.add_system_message(display)
        except (OSError, ValueError) as exc:
            ctx.ui.add_system_message(f"[MEMORY_LIST_FAILED] {exc}")

    elif sub == "show":
        if len(parts) != 2:
            ctx.ui.add_system_message("用法: /memory show <record_id>")
            return
        record = mm.show(parts[1])
        if record is None:
            ctx.ui.add_system_message(f"记忆不存在或 ID 无效: {parts[1]}")
            return
        ctx.ui.add_system_message(
            f"{record.record_id}\n"
            f"scope={record.scope}  type={record.type}  status={record.status}\n"
            f"source={record.source}\n"
            f"created_at={record.created_at.isoformat()}\n"
            f"updated_at={record.updated_at.isoformat()}\n"
            f"content_hash={record.content_hash}\n\n"
            f"{record.content}"
        )

    elif sub == "export":
        if (
            len(parts) not in {2, 3}
            or parts[1] not in {"user", "project", "all"}
            or (len(parts) == 3 and parts[2] not in {"json", "markdown"})
        ):
            ctx.ui.add_system_message(
                "用法: /memory export <user|project|all> [json|markdown]"
            )
            return
        try:
            result = mm.export(
                scope=None if parts[1] == "all" else parts[1],
                format=parts[2] if len(parts) == 3 else "json",
            )
            ctx.ui.add_system_message(
                f"已导出 {result.record_count} 条记忆: {result.path}\n"
                f"content_hash={result.content_hash}"
            )
        except (OSError, ValueError) as exc:
            ctx.ui.add_system_message(f"[MEMORY_EXPORT_FAILED] {exc}")

    elif sub == "forget":
        if (
            len(parts) != 4
            or parts[2] not in {"user", "project"}
            or parts[3] != "confirm"
        ):
            ctx.ui.add_system_message(
                "为避免跨作用域误删，请使用 "
                "/memory forget <record_id> <user|project> confirm"
            )
            return
        try:
            changed = mm.forget(parts[1], scope=parts[2], confirm=True)
            ctx.ui.add_system_message(
                ("已遗忘记忆: " if changed else "记忆不存在或作用域不匹配: ")
                + parts[1]
            )
        except (OSError, PermissionError, ValueError) as exc:
            ctx.ui.add_system_message(f"[MEMORY_FORGET_FAILED] {exc}")

    elif sub == "clear":
        if len(parts) != 3 or parts[1] not in {"user", "project"} or parts[2] != "confirm":
            ctx.ui.add_system_message(
                "为避免跨作用域误删，请使用 /memory clear <user|project> confirm"
            )
            return
        try:
            changed = mm.clear(parts[1], confirm=True, include_directory=True)
            ctx.ui.add_system_message(
                f"已清空 {parts[1]} 作用域的 {changed} 个记忆存储单元。"
            )
        except (OSError, PermissionError, ValueError) as exc:
            ctx.ui.add_system_message(f"[MEMORY_CLEAR_FAILED] {exc}")

    elif sub == "pending":
        candidates = mm.list_candidates()
        if not candidates:
            ctx.ui.add_system_message("没有待审核的记忆候选。")
            return
        ctx.ui.add_system_message(
            "待审核记忆：\n" + "\n".join(
                f"  {item.candidate_id} scope={item.scope} type={item.type} "
                f"status={item.status} source={item.source} "
                f"hash={item.content_hash[7:19]} {item.size_bytes} bytes"
                for item in candidates
            )
        )

    elif sub == "diagnostics":
        diagnostics = mm.diagnostics()
        if not diagnostics:
            ctx.ui.add_system_message("Memory diagnostics: no recorded degradation.")
            return
        ctx.ui.add_system_message(
            "Memory diagnostics:\n" + "\n".join(
                f"- [{item.code}] {item.message}"
                + (f" ({item.path})" if item.path else "")
                for item in diagnostics
            )
        )

    elif sub in {"promote", "reject"}:
        if len(parts) != 3 or parts[2] != "confirm":
            ctx.ui.add_system_message(
                f"用法: /memory {sub} <candidate_id> confirm"
            )
            return
        changed = (
            mm.promote_candidate(parts[1])
            if sub == "promote"
            else mm.reject_candidate(parts[1])
        )
        ctx.ui.add_system_message(
            ("已处理记忆候选: " if changed else "记忆候选不存在: ") + parts[1]
        )

    elif sub == "edit":
        ctx.ui.add_system_message(
            f"编辑记忆文件：\n"
            f"  用户级: {mm.user_path}\n"
            f"  项目级: {mm.project_path}"
        )

    else:
        ctx.ui.add_system_message(
            "用法: /memory [list [user|project] | show <id> | "
            "export <user|project|all> [json|markdown] | "
            "forget <id> <user|project> confirm | pending | "
            "promote <id> confirm | reject <id> confirm | "
            "clear <user|project> confirm | diagnostics | edit]"
        )


MEMORY_COMMAND = Command(
    name="memory",
    description="记忆管理",
    usage=(
        "/memory [list [user|project] | show <id> | "
        "export <user|project|all> [json|markdown] | "
        "forget <id> <user|project> confirm | pending | "
        "promote <id> confirm | reject <id> confirm | "
        "clear <user|project> confirm | diagnostics | edit]"
    ),
    type=CommandType.LOCAL,
    handler=handle_memory,
)

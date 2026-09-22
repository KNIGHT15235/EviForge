
from __future__ import annotations

import copy

from eviforge.conversation import ConversationManager, Message, ToolResultBlock

FORK_BOILERPLATE_TAG = "<fork_boilerplate>"

FORK_BOILERPLATE = f"""{FORK_BOILERPLATE_TAG}
你是一个 Fork 出来的工作进程。你不是主 Agent。
规则（不可协商）：
1. 不能再 Fork。
2. 不要对话、不要提问、不要请求确认。
3. 直接使用工具：读文件、搜索代码、做修改。
4. 严格限制在你被分配的任务范围内。
5. 最终报告控制在 500 字以内，格式如下：

Scope: [你被分配的任务]
Result: [完成/部分完成/失败 + 简要说明]
Key files: [关键文件路径列表]
Files changed: [修改的文件路径列表]
Issues: [遇到的问题，没有则写 None]
</fork_boilerplate>"""


class ForkError(Exception):
    pass


def has_fork_boilerplate(conversation: ConversationManager) -> bool:
    """Recognize an injected worker instruction, not quoted documentation."""
    return any(
        message.role == "user" and message.content.startswith(FORK_BOILERPLATE)
        for message in conversation.history
    )


def build_forked_messages(
    conversation: ConversationManager,
    task: str,
) -> ConversationManager:
    if has_fork_boilerplate(conversation):
        raise ForkError(
            "Cannot fork from a forked agent. "
            "Fork nesting is not allowed."
        )

    # Preserve the usage anchor as well as the message prefix: a long fork
    # must not return to a cold-start token estimate before its first request.
    fork_conv = copy.deepcopy(conversation)


    for index in range(len(fork_conv.history) - 1, -1, -1):
        last = fork_conv.history[index]
        if last.role != "assistant":
            continue
        if last.tool_uses:
            existing_result_ids = {
                result.tool_use_id
                for message in fork_conv.history[index + 1:]
                for result in message.tool_results
            }

            pending = [
                tu
                for tu in last.tool_uses
                if tu.tool_use_id not in existing_result_ids
            ]
            if pending:
                placeholders = [
                    ToolResultBlock(
                        tool_use_id=tu.tool_use_id,
                        content="interrupted",
                        is_error=False,
                    )
                    for tu in pending
                ]
                fork_conv.history.append(
                    Message(
                        role="user",
                        content="",
                        tool_results=placeholders,
                    )
                )
        break

    fork_conv.add_user_message(f"{FORK_BOILERPLATE}\n\n你的任务：\n{task}")
    return fork_conv

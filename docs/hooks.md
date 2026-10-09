# EviForge 默认 Hook

默认包含 13 条规则：用户预期的六事件、四类动作、10 组映射，以及保留的 3 条证据治理规则。TUI 和 headless 使用同一引擎、同一 Agent 循环。

| 事件 | 默认动作 | 实现范围 |
| --- | --- | --- |
| session_start | 安全检查 | 校验工作目录及 Hook 产物路径，建立内容/Git 基线；拒绝 Hook 目录符号链接和越界路径 |
| turn_start | 安全检查、发送通知 | 再次检查 Hook 产物路径，记录轮次开始并在终端显示本地通知 |
| pre_tool_use | 安全检查 | WriteFile/EditFile 写入前保护凭证、私钥、Git 内部文件、Hook/权限配置；记录写入前哈希 |
| post_tool_use | 安全检查、自动提交 | 主 Agent 的 WriteFile/EditFile 成功后检查变更，满足归属及 Git 条件时生成本地提交 |
| turn_end | 代码验收、记录日志、发送通知 | 执行 Python AST、Git whitespace 和配置的项目检查；写入轮次状态、显示通知 |
| session_end | 最终证据报告、自动提交、发送通知 | 保存验证证据，补交尚未提交的合格文件，显示真实结束状态 |
| pre_send | 注入证据约束提示词 | 要求真实验证，区分通过/失败/未验证；经验候选仍需隔离、验证及人工确认 |

这里延续原项目语义：`session_start/session_end` 对应一次 Agent.run 任务执行，不是整个 TUI 进程；`turn_start/turn_end` 对应一轮 ReAct 迭代。`startup/shutdown` 仍用于进程入口/退出的自定义 Hook。失败、取消、提前退出也会收尾，验证重试不会重复触发 session_end。

## 通知和日志

通知默认发送到本地终端（TUI 通知卡片；headless Hook 事件回调/JSONL），不需要账号或 Webhook。TUI 中通知默认展开，其他审计详情可按 Enter 展开。外部通知仍可通过自定义 `http` 动作显式配置。

本地记录位于目标项目的 `.eviforge/hooks/`：

- `lifecycle.jsonl`：会话/轮次安全检查、轮末日志。
- `notifications.jsonl`：轮次开始、结束及任务结束通知。
- `commits.jsonl`：自动提交的 commit SHA、文件列表或跳过/失败原因。
- `reports/<身份摘要>.json`：最终变更 SHA-256、检查命令/退出码、验证指纹及未验证项。

新增 JSONL 只记录事件、身份、轮次、状态等元数据，不写入用户提示词、工具参数/输出或文件内容。配置的检查命令输出会按已有规则出现在证据报告中，应由项目维护者控制其内容。

## 自动提交

默认开启**本地** checkpoint，绝不自动 push。可独立关闭：

```yaml
hook_policy:
  enabled: true
  auto_commit: false
  checks: []
```

提交必须满足：

1. 任务在 Git 仓库根目录运行，开始时已有 HEAD 且工作区/暂存区干净。运行产物 `.eviforge/` 不参与提交。
2. 文件由当前主 Agent 的成功 WriteFile/EditFile 操作产生，写入前后哈希证明归属；敏感文件、忽略文件、超限文件、子 Agent/Bash/MCP 产生的未归属变更不会纳入提交。
3. 当前所有待提交工作区变更都属于已记录的写入，HEAD 未被外部移动，暂存区没有其他内容，没有冲突/重命名。
4. 内置校验及已配置的项目检查通过；快照超限、内容在校验中变化、外部改写、失败/取消/blocked/ambiguous 结束均禁止新的结束提交。

仅对明确文件执行 Git add/commit，使用 literal pathspec，正常执行仓库 Git hooks 和签名；不覆盖身份配置、不使用 `--no-verify`、不修改远端。提交拒绝或取消时，在 HEAD 和对应暂存内容仍匹配本次操作的前提下恢复本次暂存，保留工作区修改。每次工具后的提交是检查点，后续任务失败不会自动回滚已生成的检查点。

跳过和提交失败都有记录，不把“未提交”伪装成“已提交”。没有项目测试配置时，仍可凭已完成的静态校验生成检查点，但报告明确标为 `partial`，不宣称完整测试通过。开始时有用户改动的任务会整次跳过自动提交。

## 代码验收配置

```yaml
hook_policy:
  enabled: true
  auto_commit: true
  checks:
    - name: targeted_tests
      argv: ["{python}", "-m", "pytest", "-q", "tests/test_math.py"]
      timeout: 30
hooks: []
```

`{python}` 使用当前解释器。检查以 argv 执行，不插入 Shell 字符串；单条及每批检查上限 30 秒。检查内容无变化时复用证据。检查必须只读，若修改源文件，证据会失效。

检查失败会反馈到 Agent 上下文；完成阶段最多再尝试修复两次，持续失败返回 `failed`。权限/敏感文件拒绝会使任务 `blocked`。通知、日志及 Git checkpoint 的失败会报告，普通辅助动作失败不单独变成代码验收失败。

快照排除凭证、符号链接文件、运行产物、依赖和构建目录；最多 2000 个文件，每个 2 MB。超出限制明确记录为未验证。`partial` 与 `no_changes` 均不表示所有功能通过验证。既有变更只作为基线，不冒充任务新增成果。

## 自定义与边界

`hook_policy.enabled: false` 关闭默认规则；`hooks` 仍可追加自定义规则，设置 `hooks: []` 才不加载该配置层的自定义项。原有 command/prompt/http/agent 与 builtin 执行方式保留。

`scope: main` 限制主 Agent；条件支持 session_id、turn_id、agent_id、parent_id、run_status、iteration、work_dir、tool_succeeded 及 args.*。模板支持 `$SESSION_ID`、`$TURN_ID`、`$AGENT_ID`、`$PARENT_ID`、`$WORK_DIR`、`$RUN_STATUS`、`$ITERATION`、`$TOOL_OUTPUT`、`$TOOL_STATUS` 等上下文字段。自定义 Shell 模板中的参数仍需自行正确转义；内置动作不使用该方式。

默认规则保留只读工具并行，写入和提交串行；自定义工具 Hook 保持保守的顺序执行。子 Agent 继承敏感文件保护，默认不重复生成主任务日志、通知或提交。

安全检查是路径保护、工作区基线及代码校验，不是完整漏洞扫描或操作系统沙箱。Bash/MCP 等操作仍依赖既有权限、Plan 与 MCP 策略。Git 操作要求独占修改这些文件；并发外部编辑或外部 Git 操作会尽可能被状态/哈希检查识别，但不提供跨进程事务隔离。

枚举兼容保留 15 个事件名称；`error/compact/permission_request/file_change/command_execute` 的独立事件分发仍未接入，不应将枚举数量作为能力证明。本次补齐的是上表六事件的实际动作，失败通知使用可靠收尾路径；`pre_send/post_receive/startup/shutdown` 的原有自定义接入继续保留。

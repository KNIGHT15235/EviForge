# 计划审批与自动化

## 一份计划对应一次明确的授权

TUI `/plan` 进入只读规划状态，可以阅读代码并修改本轮计划文件。模型用 `ExitPlanMode(actions=[...])` 提交草案。审批界面展示冻结内容、action manifest、session / turn / hash；批准后只允许对应的调用。拒绝、修改内容、切换会话、新任务、过期或耗尽次数都会阻止旧授权使用。

action 的内容包括 `tool_name`、完整 `arguments`、`cwd`、`read_paths`、`write_paths`、`network_hosts` 和 `uses`（默认 1）。默认从内置工具的实际参数推导范围，也可以显式收窄。空 manifest 不授权写入或命令。执行前在共同工具入口重新核验；预检通过之后的过期或文件变化仍会被拒绝。

`Bash` 计划只能使用 `argv` 数组；`HttpRequest` 每次跳转都检查明确的网络 origin（scheme、host、port）。任意 MCP / 动态工具不能仅靠自报类别获得计划权限。规划期允许受限的 Explore / Plan 子 Agent；执行授权不复制给子 Agent。

## 非交互两阶段执行

```bash
eviforge --mode plan -p "规划要做的变更，并提交精确 actions" --output json
```

成功提交会返回 `approval_required`（退出码 3），`metadata.plan` 中包含完整快照。用 `eviforge plan show PLAN_ID` 再次查看，或检查 `.eviforge/plans/` 下的 JSON 和 Markdown。审阅后由操作者显式启动：

```bash
eviforge -p "执行已批准计划" --resume-session SESSION_ID \
  --approved-plan PLAN_ID --plan-hash REVIEWED_HASH --approval-ttl 300 --output json
```

这些参数表示本次启动中的明确批准；必须指定计划所属会话。独立的 `plan approve` 只记录可审计审批，它不会把可执行 grant 存到磁盘供未来进程捡取。进程结束会回收 grant，完成的计划也不能自动重放。

也可由操作者准备计划正文与 action JSON，通过 `plan create --session-id ... --turn-id ... --content-file ... --actions-file ...` 提交。session 必须是实际保存的会话，才能供后续 `--resume-session` 恢复。

## 机器输出

`--output text` 保留面向人的输出；`--output json` 最终输出一个 RunResult；`--output jsonl` 按顺序输出 RunEvent，最后一条的 `type` 为 `run_result`，`data` 为完整 RunResult。默认会在 `.eviforge/runs/` 保存事件日志和最终结果，失败或取消也尽可能落盘。

RunResult 包括 schema_version、run_id、session_id、trace_id、status、exit_code、output、errors、token 计数、elapsed_seconds、events_path 和 metadata。`eviforge schema` 输出对应 JSON Schema；分发包内也包含版本化 Schema 文件。

| 退出码 | 含义 |
| --- | --- |
| 0 | 运行成功 |
| 1 | 运行失败 |
| 2 | 配置或输入无效 |
| 3 | 操作被阻止、等待计划审批，或 DAG 写任务需要显式重试授权 |
| 4 | 部分输出或有副作用的 DAG 任务结果不确定（ambiguous） |
| 130 | 用户取消 |

即使模型最后说“已完成”，出现未获授权的工具请求仍会将当前普通 Agent 运行标为 blocked。DAG 还必须通过角色输出、文件写入和证据验证，不接受自由文本作为完成凭证。

## 重试与清理

`--max-attempts`（默认 3）和 `--provider-timeout`（headless 默认 120 秒）共同限制单次 Provider 请求；计时包括打开连接、读取流和等待。SDK 内建重试关闭，避免叠加。遇到 429、连接问题或可恢复服务错误时，只有尚未收到任何文本、thinking 或工具事件的请求可以重试。Retry-After 不会为迁就预算而缩短。

部分流中断返回 `AmbiguousStreamError` / `ambiguous`，不会执行未完整接收的工具调用，也不会静默再发一次请求。摘要与记忆提取同样核验流是否完整。正常 API 明确返回 `max_tokens` 时保留原有分段续写策略，不等同于无终止事件的断流。

运行时会等待或取消 SubAgent / Team / 内部后台任务，并回收 MCP 连接、Provider client 与会话文件。退出时保留 Worktree 和轨迹以供检查，用户可通过原有 Worktree 命令显式清理。取消不代表已发生的外部副作用被回滚。

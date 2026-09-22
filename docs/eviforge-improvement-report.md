# EviForge 相较 LikeCC 的改进报告

## 范围和结论

本实现以用户提供的最新简历截图为功能依据，以 LikeCC 源代码和 `LikeCC.md` 为兼容基线。新增功能围绕可验证执行、故障恢复和经验治理，不以复现简历实验数值作为验收条件。

代码位于独立的 `Eviforge` 目录，包、CLI、配置目录和项目指令分别为 `eviforge`、`eviforge`、`.eviforge`、`EVIFORGE.md`。原 LikeCC 未被修改。实现与测试结果不包含向 GitHub 发布的步骤。

复制基线为 LikeCC 提交 `4a9b77d281646385bf48fd85b8a03c4d28bbee23`，共 178 个文件，逐文件哈希复核无变化。原有 133 个 Python 模块中，108 个在名称替换后保持一致，25 个做了功能修改，另新增 24 个 Python 模块。约 **81.2% 的原模块只改命名**；修改模块继续复用原有类、工具、消息模型与管理器。详细清单见 [复用证据](reuse-evidence.json)。

## 功能对照

| 简历方向 | LikeCC 基线 | EviForge 实现 | 主要入口 |
| --- | --- | --- | --- |
| Agent 运行内核 | TUI 与非交互运行有独立循环和不同服务装配 | `run_to_completion` 成为共同 ReAct 循环的事件适配器；Memory、Skill、MCP、Session、SubAgent、Team 共用装配；公共生命周期等待、取消、回收 | `agent.py`、`runtime.py`、`lifecycle.py` |
| Plan 与安全审计 | 主要通过权限模式和计划文件控制执行，审批可切到宽松模式 | PlanSession 状态机，session / source turn / execution turn / content hash 绑定；冻结审批界面；精确 argv、参数、cwd、文件与网络范围、有效期、调用次数；最终执行入口再次检查 | `planning/`、`permissions/capabilities.py`、`app.py` |
| 记忆与经验治理 | Markdown 记忆与静态 Skill，无统一的经验发布流程 | SQLite 审计记录、隔离候选、验证记录、人工确认、发布、反馈撤销、版本回滚；用户/项目总预算 16,000 字符；已注入内容随版本撤销刷新 | `governance/`、`memory/auto_memory.py`、`skills/` |
| Typed DAG | 有 SubAgent / Team，但主要靠提示词约定输出 | 四角色强类型输入输出、离线图校验、依赖调度、读写冲突串行、最小工具能力、SHA-256 证据、持久化节点状态与恢复 | `dag/`、`examples/dag-review.json` |
| 自动化与故障恢复 | 主要输出文本，重试与终止判定不统一 | RunResult Schema、JSONL、稳定退出码；关闭 SDK 隐式重试，统一 Retry-After / 次数 / 时间预算；断流不重放、非法工具 JSON 不执行、不完整回答不报成功 | `automation.py`、`reliability.py`、`client.py`、`__main__.py` |

### 统一运行时及原有扩展

原有 Provider 适配、ToolRegistry、六种代码工具、上下文预算、会话格式、Slash Commands、Skill、SubAgent、Team 和 Worktree 持续复用。非交互模式现在拥有与 TUI 相同的服务，而非只有核心工具。后台结果通知在成功状态下可触发继续处理，失败、权限阻止或 ambiguous 状态不会被后台通知覆盖。

新增只读 Hook Agent 执行器，启动时等运行时装配完成，退出 Hook 执行后才关闭模型 client。它使用实际子 Agent 和 ReadFile / Glob / Grep，不递归执行 Hook。原插件来源的 Agent 加载空入口也已补为显式目录注册 API；这不包含插件市场安装器。

MCP stdio 连接由生命周期持有任务创建并关闭，解决异步上下文在不同任务退出的问题。SubAgent、Team 和内部后台任务可统一取消与回收，重复及并发关闭只关闭一次共享 client；退出保留轨迹与 Worktree，便于故障检查。

### PlanSession 与授权边界

计划状态包含草稿、已提交、已批准、执行中、完成、拒绝、失效、过期和失败。正文与 action manifest 一起参与哈希。审批按钮响应绑定被冻结的 request、plan、hash、session 和 turn，不能用旧界面的事件批准新内容。

授权同时约束精确工具参数、argv、cwd、读写范围、网络 origin、有效期和次数。原显式 deny 仍优先。最终工具执行前重新检查工具是否仍注册、是否被替换或禁用，再核验并消费 grant；等待人工回复期间发生的变化不会绕过检查。

子 Agent 继承 Plan 边界，但不复制父授权。规划期可委派受限的 Explore / Plan；子 Agent 不能修改父计划草稿。即使追踪系统调整 Agent 编号，Plan 绑定也不会丢失。磁盘保存快照和审计，进程结束后不恢复可执行 grant。

### 经验成为版本化、可撤销的输入

用户级与项目级记录采用统一数据结构，包含 scope、source task、source trace、content hash、status 和 version。自动提取只创建 quarantine，完整模型回复本身也不等于批准。通过验证、人工确认且哈希一致的版本才能发布；失败验证和负面反馈撤销当前版本。回滚仅能指向满足准入条件的历史版本。

16,000 字符预算包含包装元数据，在用户与项目之间公平分配并重新利用空余预算。撤销不仅清理数据库状态，还更新已经激活的 Skill、已有环境消息、Skill fork 和恢复缓存。压缩摘要排除可重新注入的受治理来源，防止撤销后旧内容又从摘要里出现。

会话保存改为按消息对象身份追踪，避免记忆在历史前部插入或删除导致整数游标错位。自动和手动压缩保存结构化 boundary，恢复不重复近期消息；运行时生成的环境内容不当作用户消息持久化。

### DAG 结果由契约与证据验收

Explorer、Implementer、Verifier、Integrator 各有输入和输出模型。节点必须提交通过 Pydantic 检验的结果，自由文本不能直接作为成功凭证。输出引用必须来自声明的依赖；Verifier 需要消费 Implementer 结果并实际读取或执行检查；Integrator 需要所有实现有通过的验证覆盖。

调度器按依赖并行，写/写和写/读范围重叠时串行。每个节点有独立对话和文件缓存，工具能力取角色、声明范围、父策略和 Plan 门禁的交集。DAG 不向节点暴露任意 MCP、Skill、Hook 或递归 Agent 控制工具。

文件证据由程序读取并计算 SHA-256，归档到内容寻址存储；验证命令也形成带 argv、输出和结果的哈希 receipt。恢复检查图、实际工具能力、角色、权限、输入、证据和已确认的工作区状态。完成节点直接载入结果，不再执行。

可写或可执行命令的失败节点默认 ambiguous，需要操作者检查效果后显式授权重试。审计写入失败、提交记录失败、实际写入后异常都阻止成功结项，并阻止后续自动写入。这里提供保守恢复，不提供横跨文件系统和外部程序的事务回滚。

### 自动化结果与 Provider 恢复

普通运行输出包含状态、退出码、标识、错误、token 数、耗时和日志位置，结构有独立版本。JSONL 最后一条携带最终结果，失败与取消也尽可能保存结果文件。退出码为 0 成功、1 失败、2 配置错误、3 阻止/待批准、4 ambiguous、130 取消。

SDK 内建重试关闭；统一包装层限制次数和总时长，尊重 Retry-After。文本、thinking 或工具流开始后中断，就抛出 typed ambiguous，不执行未完整接收的调用，也不再自动重发。非法或非对象工具 JSON 不会被转换为 `{}`。内容过滤、不完整响应、输出续写预算耗尽也不会按正常完成处理。

## 设计复核及测试中修复的问题

方案先按共同执行入口、权限来源、上下文注入、恢复记录和资源持有关系复核，再做跨模块故障注入。除新增模块自身测试外，修复了以下实证问题：

| 问题 | 最小修复与验证 |
| --- | --- |
| 子 Agent 重编号导致 Plan 绑定丢失 | 在最终编号后继承，并按实例保留边界；真实委派读成功、越权写失败 |
| 审批等待期间工具被禁用/替换仍能执行 | 最后入口检查 registry 状态和对象身份；真实文件保持未写入 |
| SDK 非法工具 JSON 降级为空参数 | 严格 JSON 对象校验；带默认动作工具也不执行 |
| 内容过滤或续写耗尽误成功 | 明确 blocked/failed 终态，不执行残余工具 |
| 受治理 Skill 经摘要、fork 或恢复附件绕回上下文 | 源标记、摘要过滤、版本刷新、恢复缓存撤销；验证下一次真实 Agent 请求 |
| 记忆撤销使会话游标漏存回复 | SessionRecorder 按身份保存；恢复后 user/assistant/tool 链完整 |
| 后台通知把 blocked/ambiguous 变为成功 | 终态限制自动续跑；TUI 与 headless 均有复现与回归 |
| DAG 写入后审计失败、结果提交日志失败误成功 | 未确认副作用与审计故障标记；完成记录持久化前不承认提交 |
| MCP 跨任务关闭和并发 Runtime.close | 连接持有任务、公共取消/回收和关闭锁；本地双 stdio 服务及并发关闭通过 |
| 离线 CLI 提前加载模型 SDK 导致冷启动超时 | DAGRunner 延迟导入；独立进程无 Provider 配置的治理命令通过 |

## 原有能力覆盖

| LikeCC.md 能力组 | 回归入口 |
| --- | --- |
| LLM API、Function Calling、多 Provider、序列化 | `test_document_provider`、`test_serialization`、`test_context_window` |
| 六工具、ReAct、Prompt 与权限 | `test_agent`、`test_document_runtime`、`test_permissions` |
| 上下文压缩、大结果预算、恢复快照 | `test_context`、`test_recovery`、`test_replacement_state` |
| 指令、记忆、Session、Slash Commands | `test_memory`、`test_commands`、`test_session_recorder` |
| Skill、目录工具、inline/fork、Hook | `test_skills`、`test_document_skill`、`test_hooks`、`test_hook_lifecycle` |
| MCP 工具生态与连接 | `test_mcp`、本地 stdio 工作流及双连接测试 |
| SubAgent、Fork、后台与隔离 | `test_subagent`、`test_document_subagent`、`test_document_isolation`、90 项独立脚本 |
| Worktree、Team、Coordinator | `test_worktree`、`test_teams`、`test_document_headless` |
| 新增能力与边界 | `test_plan_*`、`test_governance*`、`test_dag`、`test_eviforge_*`、`test_execution_boundaries` |

完整最终数量、环境、分发验证与可重复命令见 [验证说明](verification.md) 和 [机器验证摘要](validation-results.json)。

## 实际边界

- 本次验收是确定性离线验证：实际运行本地代码、文件、子进程、Git、SQLite、Textual 与 MCP，模型回复由替身或真实 SDK 的本地 SSE 提供；没有付费模型质量实验。
- 普通 Agent 的 `success` 表示正常结束，不证明每个历史工具调用都成功，工具错误仍可由模型继续修正。要求严格交付验收时使用 DAG 的 typed 结果和验证节点。
- argv 约束启动命令，不隔离程序内部的文件或网络行为；类型、哈希、规则与 Worktree 均不等同于操作系统沙箱。
- 人工确认中的 actor 是本地审计标识，验证 evidence 是操作者提供的记录，不是外部身份认证或自动正确性证明。
- macOS iTerm2、不同真实终端、外部 MCP 服务及所有第三方模型端点没有在本机实际验证。相关原有适配与模拟测试保留。
- 未将简历中的历史实验数值作为本次实测结果，也不据这些测试推导零误放率、固定缓存命中率或生产成功率。

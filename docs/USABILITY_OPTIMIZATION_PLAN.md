# EviForge 使用体验不足评估与可执行优化方案

> 审查日期：2026-08-24
> 审查对象：`<repo>` 当前工作副本
> 文档定位：面向开发实施、测试验收和版本排期，不把“建议”写成无法验收的口号
> 结论口径：文中的“当前值”仅使用已执行测试、可复现实验或源码能够直接证明的事实；未采集的数据统一标为“待测”，不虚构前后对比

## 0. 实施状态快照（2026-08-24）

本节是对后文原始评估方案的增量状态说明，不改写当时的基线证据。状态按当前工作树的源码和确定性回归测试判定；本轮已完成 Windows/WSL 全量回归、wheel 安装与五次确定性发行评测，真实 Provider smoke 仍未执行。

状态含义：`已完成（确定性）` 表示核心验收路径已有实现和本地 fixture；`部分完成` 表示主链已落地但仍缺少方案中的边界或外部验收；`未完成` 表示仍没有可执行实现。详细证据、复现命令和诚实边界见 [实施与验证报告](./implementation/README.md)。

| 编号 | 状态 | 当前已落地 | 尚未闭环的验收边界 |
|---|---|---|---|
| U-001 | 已完成（确定性） | `api_key_env/auth`、官方 Key 与自定义端点隔离、跨源跳转 fail-closed、脱敏诊断 | 未执行带真实凭据的 live Provider 验证 |
| U-002 | 已完成（确定性） | 单一 `PlanSession` 状态机、会话/内容哈希绑定、Escape 取消不批准、旧 Plan 失效 | 仍需最终 TUI 全量回归确认无交互回归 |
| U-003 | 部分完成 | `--config`/`EVIFORGE_CONFIG` 单源隔离、显式 false/空集合覆盖、字段来源和继承集成信任门禁 | 默认数据/配置命名空间仍保留 `.mewcode` 兼容路径；尚无正式迁移命令和完整覆盖链历史 |
| U-004 | 已完成（收缩契约） | 各入口声明支持事件，未支持事件/action 配置期拒绝；HTTP/command 超时、结果码、截断与回收已测试 | 只保证公开声明的事件子集，不表示 15 个 Hook 事件在三入口全部可用 |
| U-005 | 部分完成 | `TaskManager` 公共 snapshot/drain/shutdown；headless `wait/cancel`；超时形成 `timed_out` 并回写普通 Agent/in-process Team Trace；TUI/CLI Recovery 查看、确认和安全重试 | 不伪装跨进程 durable detach；外部 pane Team、Hook/MCP 的完整故障矩阵待验 |
| U-006 | 部分完成 | 公共 Quick Start、WSL 脚本、离线配置、Windows/Ubuntu CI 和发行物检查已写入仓库 | 当前工作树尚未以干净提交从远端 fresh clone 跑完 CI，不能宣称公开发行链已最终通过 |
| U-101 | 已完成（离线/Mock） | `init/config check/config explain/doctor/provider test`、Provider 选择、零副作用 help/version 和稳定诊断 | 首用 TTFR 与真实 Provider 成功率仍待用户实验/live smoke |
| U-102 | 部分完成 | TUI/headless 共用受治理 RuntimeBuilder，headless 已接入 Memory/Skill/MCP/Session/多 Agent；`capabilities` 显示差异 | 尚未形成覆盖全部组件的单一 Composition Root；DAG 不注入 MCP/inline Skill，三入口不宣称完全同构 |
| U-103 | 已完成（确定性） | 版本化 RunResult、JSON/JSONL Schema、稳定退出码族、stdout/stderr 分离、精确授权 manifest | live Provider/真实 CI 消费方的兼容性仍需外部验证 |
| U-104 | 部分完成 | 结构化 MCP 归属、并行初始化、总超时、单服失败降级、status/list/test 与清理诊断 | headless `reconnect` 是一次性探测而非驻留运行时热重连；CLI/TUI 尚无完整 disable 管理闭环 |
| U-105 | 已完成（确定性） | 统一记录模型、来源 task/trace、quarantine/promote/reject、按 scope 删除/导出、16,000 字符注入预算和诊断 | 真实长会话的召回质量/收益尚未评测 |
| U-106 | 部分完成 | Candidate→Skill 版本、验证记录门禁、人工确认 promote/rollback、反馈与审计轨迹；TUI `/experience` 闭环 | `validate` 当前接收外部提交的结果记录，不是宿主自动执行 replay；无 headless experience CLI，不能宣称重复犯错率下降 |
| U-107 | 部分完成（主链已通） | DAG validate/plan/run/status/resume、结构化进度、持久化证据、图/能力漂移检查、跳过已验证成功节点 | token 上限是请求前预留与完成后核算，不能在单次模型流中硬切断；DAG 能力仍少于 TUI/headless |
| U-108 | 部分完成 | 单一版本源、轮转脱敏日志、data path/stats/export/prune、支持包安全白名单与敏感数据库显式 opt-in | 尚未强制 Runtime 总配额或自动清理；默认策略仍是可审计的 dry-run |
| U-109 | 已完成（类别级画像） | Session list/inspect/resume/export/delete；Provider/工作区/能力类别漂移在网络探测前阻断；成功或中断路径完整持久化 tool use/result/thinking；task/trace/evidence 与脱敏导出 | capability 画像尚未覆盖 MCP tool catalog、Skill/Agent 定义与健康状态；未知副作用不承诺 exactly-once |
| U-110 | 部分完成 | 可重试错误分类、有限指数退避、`Retry-After`、总时长边界、部分流中断 typed ambiguous、决策事件 | 尚无熔断器与显式 fallback 链；没有真实 Provider 限流实验 |

P2 项目当前状态：U-201 已完成 0.4.0 wheel 构建与隔离安装但缺少 N-1 迁移；U-202 未实现 OS 级隔离；U-203 仅有 `init` 和诊断命令，未形成首次使用向导；U-204 本地 fake SSE 五轮 35/35 样本通过但未执行 live smoke；U-205 已在中文 DrvFS 路径以独立 WSL venv 跑通 1007 项测试，三类 Provider 网络场景仍待验；U-206 已覆盖 `thinking` 协议约束及 OpenAI `max_output_tokens` 请求断言，尚未穷举全部 Provider 字段。

### 本轮验收结果

以下数字均来自同一修改工作副本，不沿用修改前基线：

- Windows：`1006 passed, 1 skipped in 72.74s`；
- WSL2 Ubuntu 24.04：`1007 passed in 62.79s`；
- 五次确定性发行评测：`5/5` 轮、`35/35` 命令样本、Fake SSE 认证头泄漏 `0/5`，详见 [usability-v1](../evals/results/usability-v1/summary.md)；
- 0.4.0 wheel：构建成功，并在独立 Windows venv 带依赖安装后通过 help/version；
- 真实 Provider：未获显式凭据与预算前保持“未执行”，不得改写为“已通过”

## 1. 结论先行

EviForge 当前已经达到“核心代码可运行、主要模块有自动化测试、在 VS Code Remote-WSL 中可以开发”的水平，但还没有达到“陌生用户从公开仓库克隆后，可以安全、自助、稳定地完成真实任务”的产品成熟度。

最值得优先解决的并不是继续增加 Agent 数量，而是以下五条使用闭环：

1. **安全地开始**：第三方兼容 Provider 不得隐式取得官方 API Key；项目也不得在用户无感知时继承旧的全局 Hook/MCP。
2. **明确地审批**：Plan 的进入、退出、取消、批准必须由同一状态机管理，Escape 不能产生批准语义，旧 Plan 不能被下一轮重复使用。
3. **一致地执行**：TUI、headless 和 DAG 应共享一套运行时组装逻辑；若能力不同，必须在启动前显式展示，不能静默缺失 Memory、Skill 或 MCP。
4. **可观测地结束**：后台 Agent、Team、Hook 和 MCP 要有统一的等待、取消、超时与回收协议；崩溃恢复项必须能被用户看到和处理。
5. **可复现地交付**：公开 Quick Start 不能依赖作者电脑绝对路径；干净 clone、配置诊断、真实 Provider smoke test 和结构化结果需要成为发行验收的一部分。

因此，建议将下一阶段目标定义为：

> 把 EviForge 从“功能较丰富的工程原型”推进为“安全边界清楚、首用路径可自助、三种入口行为可解释、失败可以恢复的终端 Coding Agent”。

## 2. 已确认的当前基线

### 2.1 已经成立的能力

| 项目 | 当前结果 | 结论 |
|---|---:|---|
| WSL 2 / Ubuntu 24.04 全量测试 | `793 passed` | Linux 主开发链路已打通 |
| Windows 全量测试 | `792 passed, 1 skipped` | 唯一跳过项为 POSIX 符号链接场景 |
| Python / 依赖 / CLI | Python 3.12.3、`uv 0.12.5`、52 个包可按锁文件同步 | 安装基础可用 |
| 编译与导入 | `compileall`、核心依赖导入、`eviforge --help` 已通过 | 基础打包入口可用 |
| 确定性测试范围 | Config、Hook、Plan、权限、Runtime、DAG、模拟 Provider 等 | 适合继续做回归保护 |
| 真实模型请求 | 未执行 | 没有用户 API Key，因此不能宣称 live Provider 已打通 |

完整的 WSL 验证记录见 [WSL_USAGE.md](./WSL_USAGE.md)。

### 2.2 当前测试通过不代表什么

- 不代表 OpenAI、Anthropic 或第三方兼容服务的真实鉴权、流式工具调用已经验证。
- 不代表 `policy_only` 是容器或 OS 级沙箱。
- 不代表全新用户能从 GitHub 的空目录顺利完成安装。
- 不代表 TUI、`-p` 和 `--dag` 拥有相同能力。
- 不代表崩溃后产生的 `UNCERTAIN` 外部副作用已经能由用户处理。

### 2.3 已执行的小型复现实验

这些结果适合直接转成回归测试：

| 实验 | 当前可复现结果 | 期望结果 |
|---|---|---|
| Plan 对话框按 Escape | 发布 `manual` 选择 | 保持 Plan 或明确取消，不产生批准/执行语义 |
| MCP 工具名匹配 | 注册名 `mcp_demo_search`，两处 `mcp__...__` 前缀判断均为 `False` | 注册、状态展示、提示注入三者集合相等 |
| 三层配置显式降级 | override 为 `default/false` 后仍保留 `acceptEdits` 和 3 个 `true` | 显式值覆盖上层值 |
| Skill full-context fork | 当前会话 1 条，fork 后 0 条 | full 模式完整继承 |
| Hook `agent` action | 返回 `success=True` 和 `not yet implemented` | 配置期拒绝或真实执行，禁止假成功 |

## 3. 用户旅程与主要断点

| 用户旅程 | 当前体验 | 主要风险 | 优先级 |
|---|---|---|---|
| 从 GitHub 首次安装 | 文档命令曾硬编码作者机器上的绝对仓库路径，缺少完整公开 clone 路线 | 陌生用户无法照抄；本机能跑不等于交付可用 | P0 |
| 配置 Provider | 只能手工编辑 YAML；无 `init/check/doctor/provider test` | 错协议、占位模型、错误 Key 来源要到运行时才暴露 | P1 |
| 使用第三方兼容接口 | `openai-compat` 会默认回退到 `OPENAI_API_KEY` | 官方 Key 可能被发送到自定义端点 | P0 |
| 进入 Plan 并审批 | Escape、Shift+Tab、旧 Plan 缓存分属不同路径 | 取消可能产生执行语义，审批对象可能不是本轮 Plan | P0 |
| 用 `-p` 做自动化 | 功能少于 TUI，只输出自然语言，后台任务轮询私有状态 | CI 难解析，后台 Agent 可能未收尾 | P0/P1 |
| 使用 MCP | 初始化可能阻塞首条消息，状态命令使用错误前缀 | 已连接却显示 0 工具；坏服务拖住主聊天 | P1 |
| 使用 Memory/Skill | 两套 Memory 存储，Skill 多入口语义不一致 | 清理范围危险，能力在不同入口静默变化 | P1 |
| 运行 DAG/Contract | 无离线校验与持续进度，结果缺少 bundle/trace 定位 | 长任务像卡死，失败后难以追查 | P1 |
| 崩溃后恢复 | Recovery 已扫描但没有用户入口 | `UNCERTAIN` 副作用不可见，可能被误重试 | P0 |
| 更新与排错 | 日志每次覆盖；无数据统计、导出、清理和版本单一来源 | 问题难复现，磁盘和隐私风险不可控 | P2 |

## 4. 优先级定义

- **P0 — 发布阻断**：可能泄漏凭据、产生未明确批准的副作用、隐藏执行、假成功、丢失/重复外部操作，或使公开交付不可复现。发布给其他用户前必须解决。
- **P1 — 主链缺口**：不会立即导致严重安全事故，但让核心卖点在不同入口中不一致、难自动化或明显不可用。建议在下一到两个里程碑完成。
- **P2 — 产品化增强**：可观测性、数据治理、分发、隔离和高级体验。完成后才能把项目从工程原型提升为长期可维护产品。

## 5. P0：发布阻断项

### U-001：隔离 Provider 凭据与自定义端点

**现状证据**

- `mewcode/config.py:21-25` 将 `openai` 和 `openai-compat` 都映射到 `OPENAI_API_KEY`。
- `mewcode/config.py:47-51` 在 YAML `api_key` 为空时直接读取协议默认环境变量，不检查 `base_url`。
- `mewcode/client.py:350`、`:473` 会把解析出的 Key 交给配置的任意端点。
- 使用文档把 `openai-compat` 定义为本地或第三方服务，因此该行为不是只面向官方主机。

**用户影响**

用户已经导出官方 OpenAI Key，又新增第三方兼容服务且保留 `api_key: ""` 时，可能在无明确授权的情况下把官方凭据发往第三方域名。同样的边界也应覆盖自定义 Anthropic 地址。

**实施方案**

1. 在 `ProviderConfig` 增加 `api_key_env: str | None` 与 `auth: required | none`，弃用含明文值的推荐写法。
2. 协议默认环境变量只与该协议的默认端点绑定；Azure、企业网关或其他自定义端点必须显式指定自己的凭据来源。
3. `openai-compat` 默认不读取 `OPENAI_API_KEY`；本地免鉴权服务必须配置 `auth: none`。
4. 错误、日志和 `config explain` 只显示“变量名与来源文件”，永不显示值。
5. 对旧配置做一次带明确提示的迁移；在过渡版本中告警，下一版本改为 fail-closed。

**验收标准**

- 设置 `OPENAI_API_KEY=official-sentinel`，把兼容端点指向 Mock 第三方且不配置专用变量：客户端构造前失败，Mock 收到 **0 次请求**。
- 配置 `api_key_env=THIRD_PARTY_API_KEY` 后，请求只能携带后者。
- `auth: none` 时请求不包含 `Authorization`。
- OpenAI、Anthropic、自定义 OpenAI-compatible 三类端点均有参数化泄漏回归测试。
- 同源/跨源重定向都纳入测试；跨源跳转不得转发 Authorization，未审核的新 host 必须失败。
- 日志、JSON 输出和异常快照中搜索 sentinel，命中数必须为 0。

**预计工作量**：1.5～2.5 人日。
**涉及文件**：`mewcode/config.py`、`mewcode/validator.py`、`mewcode/client.py`、示例配置与 Provider 测试。

### U-002：重构 Plan 审批为单一状态机

**现状证据**

- `mewcode/plan_dialog.py:28-34,97-99` 将 Escape 绑定为 Cancel，但 `action_cancel()` 发布的是 `PlanChoice.MANUAL`。
- `mewcode/app.py:1498-1501` 在 Plan 模式下遇到任意 `LoopComplete` 就可能弹审批，没有证明本轮成功调用了 `ExitPlanMode`。
- `mewcode/agent.py:461-479` 的 `_plan_path_cache` 生成后没有在新 Plan 会话开始时清空。
- `mewcode/app.py:1145-1155` 的 Shift+Tab 可直接切换 Plan，未统一维护审批前模式。

**用户影响**

取消操作、快捷键切换、模型完成事件和显式批准不是同一个状态机。最坏情况下，用户以为自己取消了审批，系统却进入可执行的 manual 路径；第二次任务还可能展示或批准上一轮 Plan。

**实施方案**

1. 新建 `PlanSession`：包含 `session_id`、`plan_path`、内容哈希、进入前权限模式、状态和时间戳。
2. 状态只允许 `DRAFT -> READY_FOR_REVIEW -> APPROVED -> EXECUTING -> CLOSED`；取消回到 `DRAFT` 或 `CLOSED`，不能映射到执行模式。
3. 只有本轮 `ExitPlanMode` 成功、当前文件哈希与展示哈希一致时才允许批准。
4. 每次进入 Plan 都新建 session/path 并清理上一轮临时审批状态。
5. Shift+Tab、命令 `/plan`、对话框按钮和 Escape 全部调用同一个 transition API。
6. 审批日志记录 session id、plan hash、选择、恢复模式；不记录提示词秘密。

**验收标准**

- TUI Pilot 测试：Escape 后仍为 Plan 或已明确取消，`begin_contract_execution` 调用次数为 0。
- 未调用 `ExitPlanMode` 时，任何 `LoopComplete` 都不能弹出批准框。
- 连续两次 Plan 的 session id 和 plan path 不同；旧文件不能通过新审批。
- 展示后修改 Plan 文件，批准必须失败并要求重新审阅。
- 状态机所有合法边和非法边均有测试；目标是 **0 条非显式批准进入执行的路径**。

**预计工作量**：2～3 人日。
**涉及文件**：`mewcode/plan_dialog.py`、`mewcode/app.py`、`mewcode/agent.py`、Plan/权限/TUI 测试。

### U-003：让配置来源可隔离、可清空、可解释

**现状证据**

- `mewcode/config.py:229-233` 默认叠加 `~/.mewcode/config.yaml`、项目配置和 local 配置。
- `mewcode/config.py:194-218` 只在 override 为非默认值或 `true` 时覆盖；显式 `false/default/[]` 无法清除上层值。
- Worktree 配置没有进入 `_merge_config`，即使高优先级层显式修改，也可能继续使用低层值。
- `mewcode/validator.py:224-225` 要求每个单独配置层都包含完整 `providers`，导致所谓局部覆盖仍要重复主配置。
- 用户级 Hook/MCP 可以被项目无感知继承；CLI 虽然底层 `load_config(path)` 支持单文件，`mewcode/__main__.py:26-79` 却没有 `--config`。
- startup Hook 在 `mewcode/app.py:912-916` 自动运行；command action 使用系统 shell。

**用户影响**

用户无法回答“本次到底加载了哪些配置”；项目层无法关闭用户层的高权限模式、Hook 或 MCP。旧项目遗留的全局配置可能影响新项目，甚至在启动时产生用户没有预期的 shell 或网络副作用。

**实施方案**

1. 默认命名空间迁移为 `~/.eviforge`；旧 `~/.mewcode` 只通过显式迁移命令导入。
2. 增加全局参数 `--config PATH` 和 `EVIFORGE_CONFIG`。指定后只加载该文件，不隐式叠加其他来源。
3. 先合并原始 YAML，再校验最终结构；用“字段是否出现”区分继承和显式 `false/default/[]`。
4. 为集合提供清晰语义：`[]` 清空，按名称的 MCP 可覆盖/禁用，Hook 可通过 id 覆盖或删除。
5. 增加 `eviforge config explain --json`，输出每个字段的最终值、来源文件、覆盖链和脱敏凭据来源。
6. 首次加载用户级 command/http Hook 或外部 MCP 时显示信任摘要并要求确认；CI 可通过审核过的 manifest 非交互授权。

**验收标准**

- global `enable_fork: true` 可被 local `false` 关闭；`acceptEdits` 可被 `default` 降级。
- local `hooks: []`、`mcp_servers: []` 能清空继承项。
- global 只放 Provider、local 只放权限也能通过最终校验。
- Worktree 的标量、列表和嵌套字段都有 precedence 测试，显式覆盖与清空按文档生效。
- `--config project.yaml` 只加载该文件；在旧全局配置里放写 sentinel 的 startup Hook，运行该命令后 sentinel 不存在。
- `config explain` 对所有最终字段给出来源，API Key 只显示变量名。

**预计工作量**：3～5 人日。
**涉及文件**：`mewcode/config.py`、`mewcode/validator.py`、`mewcode/__main__.py`、Hook/MCP 启动逻辑与配置矩阵测试。

### U-004：修正 Hook 的“声明、执行、结果”契约

**现状证据**

- Hook schema 接受 `error/compact/permission_request/file_change/command_execute` 等事件，但代码没有对应触发路径。
- TUI、headless、DAG 触发的事件集合不同；headless 在构造 system prompt 后才运行 turn-start Hook，因此 prompt action 无法进入本轮系统提示。
- `mewcode/hooks/executors.py:128-134` 的 `agent` action 尚未实现，却返回 `success=True`。
- HTTP action 在 `mewcode/hooks/executors.py:113` 固定使用 30 秒，没有使用 `Action.timeout`。

**用户影响**

配置文件“被接受”不等于功能真的发生。用户可能基于假成功继续执行；超时参数也可能与实际等待时长不符。不同入口的 Hook 行为难以预测和排错。

**实施方案**

1. 建立入口无关的 `LifecycleDispatcher`，列出每个正式支持事件的触发前提、同步/异步语义和结果消费方式。
2. 未实现的事件或 action 在配置校验期失败，禁止 silent skip 和假成功。
3. 暂不实现 `agent` action 时直接报 `unsupported_action`；若实现，必须使用宿主提供的 Agent 工厂和同一权限策略。
4. HTTP 使用 `action.timeout`，command/http/agent 都返回统一的 `HookResult`：状态、耗时、截断后的输出、错误码。
5. TUI 以通知展示；headless/DAG 写入 stderr 事件和最终 JSON 报告。

**验收标准**

- 对 TUI/headless/DAG 做参数化事件契约测试：每个声明支持的事件至少触发一次，否则配置期拒绝。
- `agent` action 在未实现状态下必须以配置退出码失败，不能返回成功。
- HTTP 超时实测偏差不超过配置值的 10%；超时/退出后 2 秒内完成回收，残留子进程为 0。
- Hook 失败不会污染 JSON stdout，并可通过稳定错误码定位。

**预计工作量**：3～5 人日。
**涉及文件**：`mewcode/hooks/*`、`mewcode/agent.py`、`mewcode/app.py`、DAG adapter 与 Hook 契约测试。

### U-005：统一后台任务收尾并暴露 Durable Recovery

**现状证据**

- `mewcode/__main__.py:370-391` 通过 `team_manager._teams`、`task_manager._async_tasks`、`_tasks`、`_notify_queue` 等私有字段轮询，最长 90 次 × 2 秒。
- 没有 Team 时会在首个结果后直接返回，普通后台 Agent 可能没有统一等待或取消结果。
- 轮询细节以 `[poll ...]` 写入 stderr，既不是稳定协议，也会污染自动化日志。
- Runtime builder 会执行 recovery scan，但 `startup_recovery` 没有 TUI/CLI 用户入口；`/status` 与 `/trace` 也不展示 durable recovery 项。

**用户影响**

后台任务可能变成“主回答已结束，但子任务状态未知”；程序崩溃后，用户看不到未确认外部副作用，更无法安全决定 ack、重试或放弃。

**实施方案**

1. 为 `TaskManager` 增加公共 `snapshot()`、`drain_events()` 和 `shutdown(policy, timeout)`。
2. headless 增加 `--background-policy wait|cancel|detach` 与 `--background-timeout`；默认使用 `wait` 的有限超时，所有退出路径进入统一 shutdown。
3. Team 与普通 Agent 都使用相同任务生命周期；禁止 CLI 访问私有容器。
4. 增加 `eviforge recovery status|inspect|ack|retry` 和 TUI `/recovery`。
5. 启动时发现 `UNCERTAIN`：TUI 展示阻断式 banner；headless/DAG 在 stderr 和 JSON 中返回 action id、原因、建议操作，默认不自动重复外部副作用。
6. 每个任务最终状态必须是 succeeded/failed/cancelled/timed_out/detached 之一，并写入 trace。

**验收标准**

- 普通后台 Agent 与 Team 在成功、异常、Ctrl+C、超时四条路径上均无孤儿协程/子进程。
- cancel 后 2 秒内退出；stderr 不再出现 `[poll` 调试文本。
- 故障注入一个 `STARTED` 外部操作，重启后 1 秒内展示为 `UNCERTAIN`，包含 action id 和 recommendation。
- 可安全 reconcile 的文件替换自动收敛；不可证明幂等的网络副作用不自动重试。
- 重复外部副作用目标值为 0。

**预计工作量**：4～6 人日。
**涉及文件**：`mewcode/__main__.py`、Task/Team manager、Runtime recovery、状态/命令处理器及故障注入测试。

### U-006：建立真正可克隆的发行状态

**现状证据（审查快照）**

- README 和 WSL 文档的公开命令曾多次使用作者机器上的绝对仓库路径。
- 推荐的“长期方案”仍从作者 D 盘路径 clone，而不是 GitHub URL。
- 当前本地工作树中 WSL 文档、脚本、示例配置和 `.vscode` 仍有未跟踪项，另有多处未提交修改。
- 当前 `.git/config` 没有 remote；因此本地成果与目标 GitHub 仓库之间没有可验证的发布链。

**用户影响**

作者本机测试全绿，但陌生用户 clone 到空目录后可能根本拿不到相同文件，也无法照抄 Quick Start。这是交付问题，不是文档排版问题。

**实施方案**

1. README 首屏只保留公共路径：clone GitHub、进入 `eviforge`、bootstrap、配置检查、首个 smoke。
2. 作者 D 盘路径仅保留在“本次审查环境附录”，不出现在可执行命令中。
3. WSL 推荐 `.vscode/extensions.json` 补充 Remote-WSL；Windows 原生与 WSL 命令分开标注 shell。
4. 配置模板复制使用“不覆盖已有文件”的写法。
5. 普通用户启动使用会检查锁文件一致性的命令；`--frozen --no-sync` 只保留给已确认同步完成的 CI/复现场景，并补充升级后重新同步说明。
6. Starter smoke 默认只启用单 Agent；启用 Verification Agent、Team 或 DAG 前明确显示并发、token 与潜在费用。
7. 提交全部预期交付文件、配置 `origin`、推送后从空目录重新 clone 验收。
8. CI 增加 `git ls-files` 交付清单和 Quick Start smoke，避免本地未跟踪文件被文档引用。

**验收标准**

- 在没有 D 盘挂载的全新 Ubuntu/WSL 中，逐行执行首屏命令可以完成 bootstrap、进入配置步骤，并通过现有的离线配置加载 smoke；U-101 合入后再将该步升级为 `config check`。
- 全部公开可执行命令中不出现作者绝对路径。
- 新 clone 包含 `docs/WSL_USAGE.md`、bootstrap/verify 脚本、示例配置和 VS Code 设置。
- 新 clone 全量测试零失败、无新增非预期 skip，并保留既有 793 项 WSL 基线用例；运行前后 `git status --porcelain` 均为空。
- `git remote get-url origin` 指向预期仓库。

**预计工作量**：1～2 人日。
**涉及文件**：README、WSL 文档、示例配置、`.vscode`、CI 与 Git 发布流程。

## 6. P1：下一阶段主链优化

### U-101：增加 `init / config check / doctor / provider test`

**问题**：当前 CLI 只有执行参数，没有初始化与诊断入口；示例 model 是占位符，用户只有在真正发请求时才知道协议、Key、模型或 URL 是否错误。TUI 可以选择 Provider，但 headless/DAG 固定使用配置列表第一项，CLI 没有 `--provider`。连 `--help` 都会先创建 `.mewcode` 并覆盖 `debug.log`。

**实施步骤**

1. 先解析参数，`--help`、`--version` 和离线检查必须零工作区副作用。
2. `eviforge init` 交互生成项目配置，已有文件默认拒绝覆盖。
3. `eviforge config check` 离线检查 schema、占位模型、未解析变量、URL、重复名称、命令可发现性和配置来源。
4. `eviforge doctor --json` 检查 Python、uv、WSL/Windows 解释器混用、工作目录、权限、Runtime 数据目录、MCP 命令和危险继承项。
5. `eviforge provider test --provider NAME` 在明确联网授权后做小成本模型/流式/工具调用 smoke。
6. 所有执行入口增加 `--provider NAME`；不存在或不适用于当前协议时启动前失败，最终状态/JSON 记录实际 Provider 和 model。

**验收标准**

- clean clone 到可诊断配置不超过 3 条命令；人工首次成功目标中位数小于 10 分钟。
- 离线 doctor 小于 2 秒且网络请求数为 0。
- 每个检查项包含稳定 id、status、remediation；输出不包含秘密值。
- 缺 Key、占位 model、协议错配、非法 URL、有效配置有固定退出码测试。
- 多 Provider 配置中，TUI/headless/DAG 都能选中同一名称，且不会静默回退到第一项。
- 从只读目录运行 `--help`、`--version` 和 `config check` 成功，文件系统变化数为 0。

**工作量**：4～6 人日。

### U-102：共享 TUI/headless/DAG 的 Composition Root

**问题**：TUI 会初始化 `MemoryManager`、`SessionManager`、`SkillLoader`、`MCPManager`；headless 的运行时组装没有这些组件。用户使用同一配置，却会因入口不同得到不同工具和上下文。

**实施步骤**

1. 提取 `RuntimeCompositionBuilder`，统一 Provider、工具、MCP、Memory、Skill、Agent、Hook、Session、Runtime 的组装与关闭顺序。
2. 定义 `CapabilityProfile`；三种入口默认共享 profile，需要差异时必须显式配置。
3. 增加 `eviforge capabilities --json`，展示启用/禁用及原因。
4. 修复 Skill full-context 使用不存在的 `agent._conversation`，统一为正式 conversation accessor。
5. `mode/model/allowedTools` 在 slash、LoadSkill、fork 三条路径保持一致；暂不支持的字段在 schema 层拒绝。

**验收标准**

- 同一 mock prompt 通过 TUI/headless/DAG 时，profile 相同时工具目录、指令、Memory 与 Skill 注入快照一致。
- full/recent/none context 均有测试；full 模式不再出现“当前 1 条、fork 0 条”。
- 坏 Skill/Agent 文件的诊断召回率 100%，不能只写 debug 日志。

**工作量**：6～9 人日。

### U-103：为自动化提供结构化输出、退出码与最小授权

**问题**：`-p` 只打印最终自然语言；Contract 成败没有稳定输出 task id、verdict、bundle ref 和 trace id。非交互权限中，`ask` 被统一拒绝，想运行命令通常只能切到过宽的 `dontAsk`。

**实施步骤**

1. 增加 `--output text|json|jsonl`；stdout 只放机器数据，stderr 放人类进度。
2. 版本化 `RunResult` schema：`schema_version/status/exit_code/provider/task_id/trace_id/result/usage/tools/evidence/recovery/background`。
3. 定义退出码族：配置、鉴权、网络、权限、Evidence Gate、预算、内部错误。
4. 增加范围化授权 manifest：文件 glob、精确 argv、cwd、网络 host、有效期；拒绝参数漂移。
5. Contract PASS/BLOCKED/PARTIAL 都要返回 bundle ref，不能让调用者解析自然语言。

**验收标准**

- JSON stdout 中人类日志数量为 0；所有示例通过 JSON Schema 校验。
- PASS/BLOCKED/provider failure 的退出码和字段快照稳定。
- 授权精确命令通过，参数或 cwd 改变即拒绝；拒绝原因进入结构化结果。
- 100% Contract 运行可定位 task/trace/evidence bundle。

**工作量**：5～8 人日。

### U-104：修复 MCP 可发现性、超时与运维入口

**问题**：真实注册名是 `mcp_<server>_<tool>`，TUI 和 `/mcp` 却按 `mcp__<server>__<tool>` 匹配；一个挂起服务的 connect/list 没有总超时，还可能阻塞首条消息。

**实施步骤**

1. MCP wrapper 保存结构化 `server_name/tool_name`，展示层禁止手拼字符串前缀。
2. 多服务并行初始化，分别设置 connect/initialize/list/call 总超时。
3. 健康服务继续注册，失败服务降级为可见 warning，不阻塞首条聊天。
4. 增加 `/mcp list|test|reconnect|disable` 和 headless `mcp status --json`，不访问 manager 私有字段。

**验收标准**

- Fake server 注册 2 个工具后，registered/listed/prompted/discoverable 集合完全相等。
- 挂起服务在默认时限内降级，健康服务仍可用；Ctrl+C/退出后无 MCP 子进程残留。
- MCP 可发现率目标 100%。

**工作量**：3～5 人日。

### U-105：把 Memory 变成可审阅、可定向删除的数据产品

**问题**：自动提取使用 flat `memories.md`，选择性召回使用 `memory/*.md`，代码明确说明二者不同；`/memory clear` 无确认地同时清空用户级和项目级 flat 文件；提取异常被静默吞掉并以模型返回覆盖完整文件。

**实施步骤**

1. 统一记录模型：id、scope、type、source task/trace、created_at、updated_at、status、内容哈希。
2. 自动提取先进入 quarantine，展示 diff，经规则/人工审核后 promote；保留版本和 rollback。
3. `/memory list|show|promote|forget|clear --scope --confirm|export`，默认不做全局删除。
4. 所有入口使用统一 token budget；召回失败允许主请求降级，但必须产生结构化诊断。

**验收标准**

- 指定 project scope 时用户级数据零变化；全局清除必须二次确认。
- 每条注入 Memory 可追溯到来源；过期、失败和冲突状态可见。
- 自动提取失败可观测率 100%，且不破坏上一版本。

**工作量**：5～8 人日。

### U-106：打通 Trace → Skill → Replay → Promote → Rollback

**问题**：当前 `/skill` 主要提供 list/info/reload，`/evolution` 提供 status/promote/rollback/feedback，但“候选如何产生、如何验证、为什么可注入”仍是分散流程。Skill 的 mode/model/allowedTools 在不同入口语义也不统一。

**实施步骤**

1. 用单一 `ExperienceCandidate` 串联来源 trace、决策、踩坑、适用条件、反例和证据。
2. 候选默认 quarantine；通过确定性 replay 和对抗样例后才可 promote 为不可变 Skill 版本。
3. 检索结果显示命中原因、版本、来源与 token 成本；负反馈可降权，rollback 立即停止注入。
4. 提供 `experience create|validate|review|promote|rollback|stats` 与对应 TUI 命令。

**验收标准**

- 失败 trace fixture 能生成候选但不会自动注入。
- 未通过 replay 的候选无法 promote；promote 后同类任务可命中，rollback 后命中数为 0。
- 每次注入可定位 candidate、Skill version 和 replay report。

**工作量**：7～12 人日。

### U-107：增加 DAG 离线校验、进度和恢复

**问题**：现有 `--dag` 已经会在 Provider 初始化前完成图加载、结构校验、阻断验收项检查和预算构造，但没有独立的 validate-only 命令、调度预览或更完整的静态诊断。Scheduler 也已具备 token 预留、超预算节点拒绝和 overrun 记录；缺口是无法在一次进行中的模型请求触达额度时硬中断，长任务完成前也没有稳定进度。

**实施步骤**

1. 增加 `eviforge dag validate|plan|run|status|resume`。
2. 将已有启动前校验提取为可独立调用的 validate，并补齐环、角色、写冲突、artifact、acceptance 与预算诊断，输出 JSON path 和调度预览。
3. stderr/JSONL 输出 node_started/progress/completed/failed/budget 事件。
4. 按实际 usage 累计预算，超限立即 typed cancel；恢复时不重跑已证明完成节点。

**验收标准**

- 示例图离线 validate 在 2 秒内完成、API 请求 0、工作区写入 0。
- 慢节点开始后 2 秒内出现首个进度事件。
- resume 不重复运行完成节点；token 超限最多只允许当前单次请求的不可避免 overshoot，并明确报告。

**工作量**：5～8 人日。

### U-108：修复版本、日志和运行数据管理

**问题**：`pyproject.toml` 是 `0.3.0`，`/status` 硬编码 `v0.9.0`；`.mewcode/debug.log` 每次启动覆盖，缺少轮转、脱敏、保留策略和用户数据管理命令。

**实施步骤**

1. 版本统一从 `importlib.metadata.version("eviforge")` 读取，增加 `eviforge --version`。
2. 日志移到 Runtime data root，支持 level、rotation、retention 和默认秘密脱敏。
3. 增加 `eviforge data path|stats|export|prune --dry-run`，覆盖 session、trace、evidence、evolution、CAS 与日志。
4. `doctor` 展示实际数据目录、配额、最近错误码和恢复项，不展示敏感内容。

**验收标准**

- 包元数据、CLI、TUI `/status` 版本完全一致。
- API Key、Authorization header、敏感工具参数的日志命中数为 0。
- 日志大小和保留天数有上限；prune 默认 dry-run，导出可在崩溃后读取。

**工作量**：3～5 人日。

### U-109：统一 Session、任务恢复与导出入口

**问题**：Session 的 list/resume/new/delete 主要存在于 TUI；headless 没有 `--resume`，Runtime recovery、内存中的 `/trace` 和历史 Session 也是分散概念。用户很难回答“上一次任务能否继续、继续会恢复哪些上下文、哪些副作用不会重跑”。

**实施步骤**

1. 增加 `eviforge session list|inspect|resume|export|delete`，headless 支持 `--session ID`。
2. Session manifest 记录 conversation 前缀、工作目录、提交/工作树指纹、Provider profile、capability profile 和关联 task/trace。
3. resume 前检查工作树漂移和 `UNCERTAIN` recovery；有冲突时先展示差异，禁止静默继续。
4. export 默认脱敏，支持 JSON/Markdown；删除需要 scope 和确认，不连带删除未选择的 evidence/skill。

**验收标准**

- TUI 创建的 Session 可在 headless 显式恢复，反向亦可。
- 恢复后上下文前缀、工作目录与能力 profile 可验证；工作树漂移会产生稳定告警/退出码。
- Session 删除不会误删共享 evidence；导出中秘密扫描命中数为 0。

**工作量**：4～6 人日。

### U-110：为 Provider 请求增加有边界的韧性策略

**问题**：429、瞬时 DNS/连接错误和短暂 5xx 是真实使用主链中的常见失败，但当前 Agent 循环没有统一 retry/backoff/failover 策略。盲目重试又可能重复计费，或在流式响应已部分到达时产生语义重复。

**实施步骤**

1. 建立 retryable 错误分类，遵循 `Retry-After`，使用指数退避与 jitter，并设置次数、总时长和 token/费用上限。
2. 仅在“请求尚未得到可消费响应”或 Provider 提供幂等保障时自动重试；部分流式响应进入 typed ambiguous 状态，不静默重放。
3. 熔断器按 Provider profile 隔离；fallback 必须由用户显式配置顺序、模型兼容条件和预算，不能自动把敏感上下文切到未知端点。
4. retry/fallback 决策写入结构化事件和最终结果。

**验收标准**

- 429/临时网络错误 fixture 能按 `Retry-After` 有限重试后成功；不可重试错误立即退出。
- 部分流式响应断开不会自动重复整轮，结果明确标记 ambiguous 并给出恢复建议。
- 达到总时长/次数/费用任一上限后立即停止；fallback 端点未经显式配置时请求数为 0。

**工作量**：4～6 人日。

## 7. P2：产品化增强

| 编号 | 优化项 | 可执行内容 | 验收目标 | 估算 |
|---|---|---|---|---:|
| U-201 | 分发与升级 | 发布 wheel，支持 `uv tool install`；配置与数据做版本化迁移 | clean VM 从发行物安装；N-1 升级保留配置/session | 4～6 人日 |
| U-202 | 可选强隔离 | 抽象容器/独立 WSL executor，控制面与执行面分离 | 对抗 fixture 不能读取控制面凭据或越界路径 | 8～15 人日 |
| U-203 | TUI 首用与可访问性 | 首次向导、快捷键速查、统一语言、可复制错误码和诊断面板 | 新用户无需读源码即可发送多行、取消、退出和定位日志 | 3～5 人日 |
| U-204 | Live Provider 测试 | 本地 fake SSE 覆盖协议；真实 smoke 需显式开关、预算和模型记录 | 默认 CI 零费用；opt-in smoke 可追踪模型/日期/usage | 3～5 人日 |
| U-205 | WSL 性能与路径体验 | 明确 WSL 内/Windows 主机服务地址，移除模板中的 Windows venv symlink | Worktree 不链接错误平台 venv；三种网络场景均能自检 | 2～3 人日 |
| U-206 | Provider 字段语义 | 检查各协议对 `max_output_tokens/thinking` 等字段的真实消费；不支持时配置期拒绝 | Mock 请求断言字段生效；0 个被静默忽略的配置字段 | 2～4 人日 |

## 8. 推荐实施路线图

以下估算以一名熟悉代码库的开发者为基准，包含实现、测试和文档，不包含等待真实 Provider 审批或外部基础设施的时间。

### M0：冻结基线与评测夹具（1～2 人日）

- 把第 2.3 节五个小实验正式放入测试集。
- 建立 `tests/usability/`，先覆盖每个 P0：`provider_credential`、`plan_state`、`config_precedence`、`hook_contract`、`lifecycle_recovery`、`clean_clone_quickstart`；MCP discovery 和 JSON schema 作为随后 P1 的首批夹具。
- 记录当前全量测试、命令耗时、git workspace delta。
- 建立“目标值”和“实测值”分栏；没有测量就不写提升百分比。

**完成标志**：每个 P0 都有一个先红后绿的回归测试。

### M1：安全与发布阻断（15～24 人日）

顺序建议：

1. U-001 Provider 凭据隔离；
2. U-002 Plan 状态机；
3. U-003 配置隔离/覆盖；
4. U-004 Hook 契约；
5. U-005 后台收尾与 Recovery 可见；
6. U-006 clean-clone 发布验收。

**完成标志**：P0 回归全绿；公开仓库可从空目录复现；没有隐式凭据跨端点和非显式批准执行路径。

### M2：首用与自动化主链（22～34 人日）

- U-101 init/doctor/provider test。
- U-102 Composition Root 和 capability profile。
- U-103 JSON/JSONL、退出码、最小授权。
- U-104 MCP 可发现性、超时和运维入口。
- U-110 Provider 有边界的重试、熔断与显式 fallback。

**完成标志**：同一 profile 下 TUI/headless/DAG 能力快照一致；CI 不再解析自然语言；新用户能自助定位配置错误。

### M3：记忆、经验与复杂任务闭环（24～39 人日）

- U-105 统一 Memory 数据模型。
- U-106 Trace-to-Skill 经验沉淀闭环。
- U-107 DAG validate/progress/resume。
- U-108 日志、版本和数据治理。
- U-109 Session/任务恢复与导出。

**完成标志**：记忆和经验都可追溯、可审核、可回滚；复杂任务可离线检查、持续观测和断点恢复。

### M4：分发、隔离与真实 Provider 验证（按需 22～38 人日）

- U-201～U-206。
- 在准备公开演示或真实多人使用时优先做发行物、live smoke 与执行隔离。

## 9. 前后优化评测方案

### 9.1 原则

1. **现有测试通过数不是使用体验指标**，只能作为无回归底线。
2. 确定性 fixture 与真实 Provider smoke 分开统计，禁止混成一个“成功率”。
3. 所有百分比必须保存原始 JSON、环境、提交 SHA 和运行命令。
4. 每项至少重复 5 次；涉及人工首用时至少 5 名未接触项目的参与者，样本不足时只报告原始计数和中位数。

### 9.2 核心指标

| 指标 | 当前基线 | 目标 | 测量方法 |
|---|---|---:|---|
| Clean-clone Quick Start 成功率 | 待测；当前文档依赖作者路径 | 5/5 | 全新 WSL/容器逐行执行首屏命令 |
| 首次成功响应时间 TTFR | 待测 | 中位数 <10 分钟 | 从 clone 开始到 mock/live 首个完整响应 |
| Provider 凭据跨端点请求 | 静态证据表明存在风险 | 0/全部矩阵 | sentinel Key + Mock endpoint 抓包 |
| 非显式 Plan 批准执行路径 | Escape 实验失败 | 0 | 状态机/TUI Pilot 分支测试 |
| 配置显式降级正确率 | 4 个 inspected 字段均失败 | 100% | 三层 precedence 参数化矩阵 |
| MCP 工具可发现率 | 样例前缀匹配失败 | 100% | registered/listed/prompted/callable 集合比较 |
| 后台任务孤儿率 | 待测；代码无统一 shutdown | 0% | 成功/异常/取消/超时进程与协程快照 |
| Recovery 可见率 | startup recovery 无用户入口 | 100% | 故障注入后重启检查 TUI/JSON |
| JSON 自动化解析率 | 当前无 headless JSON 结果协议 | 100% | JSON Schema + stdout 污染测试 |
| Capability 一致率 | headless 缺少多项 TUI 组件 | 100% 或差异全显式 | 三入口 golden snapshot |
| Memory 误删跨 scope 数 | `/memory clear` 同时清两级 | 0 | user/project fixture 哈希比较 |
| 日志秘密泄漏数 | 待测 | 0 | sentinel Key/header/argument 扫描 |
| 全量回归 | WSL 793；Windows 792+1 skip | 不下降 | 相同环境、锁文件与命令 |

### 9.3 建议的实验套件

**A. 首用实验**

- 5 个干净 WSL 环境；不挂载 D 盘、不预装项目、不存在 `~/.mewcode`。
- 分别走 OpenAI、Anthropic、local compat 的离线配置检查；真实 smoke 只在显式预算下执行。
- 记录命令数、TTFR、失败点、是否查看源码、是否误覆盖配置。

**B. 安全边界实验**

- 3 种协议 × 官方/第三方/本地 3 种端点 × 有/无专用 Key。
- Plan 的按钮、Escape、Shift+Tab、文件变更、连续两轮任务全路径。
- global Hook/MCP + project 清空 + `--config` 隔离矩阵。

**C. 生命周期与恢复实验**

- 普通 Agent、Team、Hook command、MCP stdio 分别注入成功、挂起、异常和 Ctrl+C。
- 对外部操作在 PREPARED、STARTED、COMMITTED 三个阶段杀进程并重启。
- 统计退出耗时、残留 PID/协程、Recovery 可见性和重复副作用。

**D. 入口一致性实验**

- 使用完全相同的 mock Provider、配置和提示分别运行 TUI/headless/DAG。
- 比较 tool catalog、system instructions、Memory/Skill 注入、Hook 事件、usage 和最终 evidence。
- 允许 UI 表现不同，不允许能力静默不同。

### 9.4 数据产物

每次评测保存：

```text
eval-results/<git-sha>/<timestamp>/
├── environment.json
├── commands.jsonl
├── metrics.json
├── junit.xml
├── stdout/
├── stderr/
└── summary.md
```

`metrics.json` 至少包含 `metric_id`、`baseline_or_after`、`sample_size`、`value`、`unit`、`fixture_or_live`、`provider/model`、`git_sha` 和 `timestamp`。只有该目录中存在原始结果时，才在 README 或简历中写提升数字。

## 10. 每项优化的 Definition of Done

一项任务只有同时满足以下条件才算完成：

- 有一个能在修改前稳定失败、修改后稳定通过的回归测试。
- TUI/headless/DAG 的影响已明确；不适用入口有公开原因。
- 失败路径、取消路径和超时路径已测试，不只测 happy path。
- 错误包含稳定 code 和 remediation，不泄漏凭据、提示词或敏感工具参数。
- 用户文档、示例配置和 `--help` 同步更新。
- Windows 与 WSL 全量回归不下降。
- 运行测试、doctor 和 Quick Start 后工作区没有非预期变化。
- 若包含迁移，提供向后兼容窗口、告警和 rollback 方式。
- 若宣称性能改善，提交原始评测数据与复现实验命令。

## 11. 建议立即进入开发的前十个任务

按依赖和风险排序：

1. 为 Provider 凭据边界补 sentinel 泄漏测试并修复 `api_key_env/auth`。
2. 为 Plan Escape、旧 Plan、无 ExitPlanMode 补 TUI 状态机测试并修复。
3. 重写 raw config merge，支持显式 `false/default/[]`。
4. 增加 `--config` 隔离和 `config explain`，阻止旧全局 Hook/MCP 无感继承。
5. 将未实现 Hook 事件/action 改为配置期拒绝，修正 HTTP timeout。
6. 为 TaskManager 增加统一 shutdown，并让 headless 不再访问私有状态。
7. 暴露 Recovery status/inspect/ack，默认阻止 `UNCERTAIN` 外部副作用自动重试。
8. 修复 MCP 结构化归属、前缀、初始化超时和 `/mcp` 状态。
9. 添加 `doctor --json` 与零副作用 `--help/--version`。
10. 清理公共 Quick Start、提交全部交付文件，并从 GitHub 空目录做一次真实 clone 验收。

完成以上十项后，再开始 Composition Root、结构化运行结果和 Memory/Skill 演进闭环，收益最大，也最不容易在不稳定地基上继续堆功能。

## 12. 不应对外宣称的内容

在对应实验完成前，README、演示和简历中不要写：

- “已完整支持 OpenAI/Anthropic/所有兼容模型”——目前没有 live Provider 证据。
- “Plan 绝对安全”——在状态机修复和分支验证前不成立。
- “策略沙箱可运行不可信代码”——`policy_only` 不是 OS 隔离。
- “TUI、CLI、DAG 能力完全一致”——当前 composition 明确不同。
- “经验沉淀显著降低重复犯错率”——需要固定任务集、基线组、启用组和原始结果。
- “性能提升 X%”——本方案中的目标值不是实测结果。

更准确的当前表述是：

> EviForge 已完成 WSL/Windows 确定性工程链路验证，并具备 Plan、权限、Runtime、DAG、Memory、Skill、MCP 与多 Agent 的代码基础；下一阶段重点是凭据/审批边界、入口一致性、可恢复性和公开交付闭环。

---

## 附录 A：主要源码证据索引

| 主题 | 位置 |
|---|---|
| Provider 默认 Key 映射 | `mewcode/config.py:21-25,47-51` |
| 自定义 endpoint 使用解析后的 Key | `mewcode/client.py:344-350,467-473` |
| 配置叠加与不能显式降级 | `mewcode/config.py:194-218,221-249` |
| 每层必须包含 Provider | `mewcode/validator.py:216-230` |
| CLI 入口与日志副作用 | `mewcode/__main__.py:16-80` |
| headless 私有轮询 | `mewcode/__main__.py:357-393` |
| 非交互 ask 处理 | `mewcode/agent.py:1797-1811` |
| Headless event callback 能力 | `mewcode/agent.py:1567-1755` |
| Plan 对话框 Escape | `mewcode/plan_dialog.py:28-34,97-99` |
| Plan 弹窗与快捷键路径 | `mewcode/app.py:1145-1155,1498-1501` |
| Plan path cache | `mewcode/agent.py:461-479` |
| Hook HTTP timeout / agent stub | `mewcode/hooks/executors.py:97-134` |
| MCP 注册名 | `mewcode/mcp/tool_wrapper.py:61-67` |
| MCP 错误展示前缀 | `mewcode/app.py:1962-1968`、`mewcode/commands/handlers/mcp.py:17-27` |
| Memory 两套目录 | `mewcode/memory/auto_memory.py:55-77` |
| Memory 静默失败与全量 clear | `mewcode/memory/auto_memory.py:118-150,215-219` |
| `/memory` 命令 | `mewcode/commands/handlers/memory.py:7-45` |
| 状态版本硬编码 | `mewcode/commands/handlers/status.py:9` |
| 包版本 | `pyproject.toml:3` |

## 附录 B：文档维护规则

- 每完成一个编号任务，将状态从 `planned` 更新为 `implemented`，附 PR/commit、测试名和评测目录。
- 每次版本发布前重新采集第 9.2 节，不沿用旧提交的数字。
- 新增功能必须先回答：首用入口在哪里、非交互语义是什么、失败如何诊断、取消如何回收、数据如何清理。
- 如果某能力只支持 TUI 或只支持 DAG，要在 `capabilities --json` 和用户文档中同时说明。

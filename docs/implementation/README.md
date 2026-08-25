# EviForge 实施与验证报告

> 状态日期：2026-08-24
> 对应方案：[使用体验不足评估与可执行优化方案](../USABILITY_OPTIMIZATION_PLAN.md)
> 口径：只记录当前源码、自动化测试和仓库内评测产物能够证明的事实；不把本地 fixture、目标值或历史数字冒充真实 Provider/模型收益。

## 1. 交付结论

本轮已经把 EviForge 从“主要依赖 TUI 的功能原型”推进为具有安全配置、严格 Plan 审批、结构化 headless CLI、可恢复 DAG、可审阅 Memory/Experience、MCP 运维和运行数据治理的终端 Coding Agent。Provider 凭据隔离、Plan 状态机、Hook 收缩契约、结构化 RunResult、Memory 作用域治理等主链已有确定性回归保护。

项目仍不是“所有方案项全部完成”的产品：三入口尚未共用覆盖全部组件的单一 Composition Root，DAG 不注入 MCP/inline Skill，Experience 的 replay 结果仍由外部记录而非宿主自动执行，Session capability 画像还是类别级而非实际工具目录指纹，执行隔离仍是 `policy_only`。这些边界在 `eviforge capabilities` 和本文中显式公开。

当前工作树包含本轮修改，尚未形成干净候选提交。因此：

- 可以陈述“功能已实现并有对应 fixture”；
- 可以陈述本轮本地 Windows/WSL 全量回归、wheel 安装和五次确定性发行评测结果，但不能写成远端 clean-clone CI 已通过；
- 未使用真实 API Key，不能陈述 OpenAI、Anthropic 或第三方模型的 live 成功率、延迟或费用。

## 2. 状态判定规则

| 状态 | 判定规则 |
|---|---|
| 已完成（确定性） | 核心用户路径、失败路径和安全边界已有实现，并有本地 mock/fixture 回归测试；不自动等同于 live Provider 验收 |
| 部分完成 | 方案主链已有可用实现，但至少一项明确验收边界、入口或外部验证仍缺失 |
| 未完成 | 当前没有可执行实现，或只有文档/接口占位 |

## 3. P0 实施矩阵（U-001～U-006）

| 编号 | 状态 | 已实现与源码证据 | 验证方法 | 剩余边界 |
|---|---|---|---|---|
| U-001 Provider 凭据隔离 | 已完成（确定性） | `ProviderConfig` 支持 `api_key_env`、`auth: required/none`；官方环境变量只绑定官方端点，自定义端点必须显式声明来源；客户端禁止凭据重定向，安全导出只暴露来源名 | `tests/test_config_security.py`：官方/自定义端点矩阵、无鉴权 header、跳转、repr/YAML/日志 sentinel 脱敏 | 未执行真实 Provider 鉴权；该结论只证明本地传输构造边界 |
| U-002 Plan 单一状态机 | 已完成（确定性） | `PlanSession` 管理 `DRAFT→READY_FOR_REVIEW→APPROVED→EXECUTING→CLOSED`；审批绑定 session、turn、plan path 和内容哈希；Escape 回到 draft，不产生执行语义 | `tests/test_plan_session.py`、`tests/test_plan_dialog.py`、`tests/test_app_plan_runtime.py`、`tests/test_plan_command_routing.py` | 最终全量 TUI Pilot 仍需随候选提交重跑 |
| U-003 配置隔离/覆盖/解释 | 部分完成 | `--config` 与 `EVIFORGE_CONFIG` 单文件隔离；raw layer 合并保留显式 `false/default/[]`；MCP/Hook 可覆盖和禁用；`config explain` 给出最终字段来源；继承自用户层的可执行 Hook/MCP 默认 fail-closed，需 `--trust-config` | `tests/test_config_security.py` 的三层覆盖、单源隔离、provenance、trust 与未解析环境变量用例；`tests/test_cli_usability.py` 的执行前阻断用例 | 默认目录仍兼容 `.mewcode`，没有正式 N-1 迁移命令；当前 provenance 是最终字段来源，不是每次覆盖的完整历史链 |
| U-004 Hook 契约 | 已完成（收缩契约） | TUI/headless/DAG 各自声明支持事件；未声明事件和未实现 `agent` action 在配置期拒绝；command/http 使用动作级总超时、统一结果/错误码、输出截断和取消清理；headless/DAG 最终结构化结果包含脱敏 Hook 结果 | `tests/test_hooks.py` 的事件契约、HTTP/command 超时、取消、输出边界和 unsupported action；`tests/test_cli_usability.py` 的执行前 trust 阻断 | 只保证公开声明的事件子集；不能写成“三入口支持全部 15 个 Hook 事件” |
| U-005 后台收尾与 Recovery | 部分完成 | `TaskManager` 暴露 `snapshot/drain_events/wait_for_idle/shutdown`；headless 支持 `wait/cancel`；等待预算耗尽形成独立 `timed_out` 终态，普通 Agent 与 in-process Team 会回写 Trace；启动前 Recovery 默认在 Provider 创建前阻断 | `tests/test_task_lifecycle.py` 的 completed/cancelled/timed_out/terminal callback；`tests/test_cli_usability.py::test_headless_recovery_blocks_before_provider_factory_or_metadata`；`tests/orchestration/test_dag_resume.py` | 进程 CLI 不提供伪 durable `detach`；外部 pane Team、Hook command、MCP stdio 的完整故障注入矩阵仍待跑齐 |
| U-006 可克隆发行 | 部分完成 | README/WSL 指南采用公共仓库路径；示例离线配置、bootstrap/verify 脚本、VS Code 建议、Windows/Ubuntu CI、发行物和公共命令绝对路径检查已加入 | `tests/test_release_artifacts.py`；`.github/workflows/ci.yml`；`scripts/run-usability-eval.py` | 当前工作树非干净候选，尚未从 GitHub fresh clone 执行最终 CI；不把“workflow 文件存在”写成“远端 CI 已通过” |

## 4. P1 实施矩阵（U-101～U-110）

| 编号 | 状态 | 已实现与源码证据 | 验证方法 | 剩余边界 |
|---|---|---|---|---|
| U-101 首用与诊断 | 已完成（离线/Mock） | `init`、`config check/explain`、`doctor`、`provider test`；所有执行入口支持按名选择 Provider；help/version 和离线 DAG validate 不初始化运行数据 | `tests/test_cli_usability.py`、`tests/test_diagnostics.py`、五次发行评测脚本中的 CLI/fake SSE 检查 | `init` 是安全模板生成器而非完整交互向导；TTFR、真实模型和多人首用成功率未测 |
| U-102 Composition Root/能力画像 | 部分完成 | TUI/headless 共用 `RuntimeBuilder` 的 Runtime、风险、证据与 Recovery 基座；headless 已组装 Memory、Skill、MCP、Session、sub-Agent/Team；`capabilities --json` 显式列出入口差异；Skill full/recent/none 上下文契约已修复 | `tests/runtime/test_builder.py`、`tests/test_agent_runtime_integration.py`、`tests/test_skills.py`、`tests/test_cli_usability.py` | Provider、工具和上层组件仍在 TUI/headless 分别组装；DAG 没有 MCP/inline Skill/Session，因此不是完整共享 Composition Root |
| U-103 结构化自动化 | 已完成（确定性） | 版本化 `RunResult` 与 `schemas/run-result.schema.json`；JSON/JSONL final record；稳定退出码族 0/1/2/3/4/5/6/7/70；机器 stdout 与诊断 stderr 分离；automation manifest 绑定路径、精确 argv、cwd、host 和有效期 | `tests/test_run_result.py`、`tests/test_structured_cli_contract.py`、`tests/test_automation_manifest.py`、`tests/test_cli_contract.py` | 尚无外部 CI 消费方兼容性数据；live Provider 的结构化失败只做了模拟 |
| U-104 MCP 可发现性/运维 | 部分完成 | MCP wrapper 保留 server/tool 元数据；多服务并行初始化；一个总 deadline 覆盖 connect/initialize/list；单服失败不阻塞健康服务；调用超时、状态、list/test/reconnect 探测、脱敏和取消清理可见 | `tests/test_mcp_operator.py`、`tests/test_mcp_timeouts.py`、`tests/test_mcp.py`、`tests/test_mcp_tool_filter_security.py` | headless `reconnect` 为一次性探测，不是对运行中会话热重连；尚未提供完整 `disable` CLI/TUI 闭环 |
| U-105 Memory 数据产品 | 已完成（确定性） | 统一记录字段含 id/scope/type/source/task/trace/hash/status；自动提取先进入 quarantine；支持 list/show/pending/promote/reject/forget/clear/export/diagnostics；删除强制 scope+confirm；注入总上限 16,000 字符并在 user/project 间公平分配 | `tests/test_memory_records.py`、`tests/test_memory.py`；Memory tamper、跨 scope、预算和降级诊断用例 | 尚未以真实长任务评测召回准确率、token 节省或完成率提升 |
| U-106 Trace→Skill 治理 | 部分完成 | `ExperienceWorkflow` 串联 candidate、不可变 Skill 版本、validation summary、promotion gate、feedback、rollback 与审计；TUI `/experience` 提供 status/list/review/create/validate/promote/rollback/feedback，危险状态变更需显式确认 | `tests/evolution/test_workflow.py`、`tests/test_experience_command.py`、`tests/evolution/*` | 当前 `validate` 记录调用方提供的 replay 结果，并不自动执行 replay；无 headless experience 命令；没有“重复犯错率下降”实验 |
| U-107 DAG 校验/进度/恢复 | 部分完成（主链已通） | `dag validate/plan/run/status/resume`；离线 schema/环/role/artifact/acceptance/budget 诊断和调度预览；结构化节点事件；持久化 graph/capability/evidence；resume 跳过已验证成功节点，拒绝可写失败自动重放 | `tests/orchestration/test_validation.py`、`tests/orchestration/test_dag_resume.py`、`tests/orchestration/test_scheduler.py`、`tests/test_cli_dag.py` | token 是请求前预留、完成后核算，不能在单次流中精确硬切断；DAG 能力边界少于 TUI/headless |
| U-108 版本/日志/数据治理 | 部分完成 | `pyproject.toml`、CLI 与 TUI 从同一版本源读取；日志轮转并脱敏；`data path/stats/export/prune`；支持包按安全白名单导出，源码/CAS/Memory/Session 默认排除，数据库需显式 opt-in 并标高敏感 | `tests/test_version.py`、`tests/runtime/test_data_manager.py`、`tests/test_release_artifacts.py` | 仅报告默认日志上限；尚未实施 Runtime 总配额和自动 retention，prune 默认 dry-run |
| U-109 Session/恢复/导出 | 已完成（类别级画像） | headless `--resume` 与 `session list/inspect/resume/export/delete`；metadata 记录工作区、脱敏 Provider profile、能力类别及 task/trace/evidence；在任何 Provider 网络探测前 fail-closed；完整持久化 user/assistant/tool_use/tool_result/thinking，执行中途失败也保存已发生链路 | `tests/test_cli_usability.py` 的同名 Provider/能力漂移零探测、工具链成功/失败恢复和伪脚手架前缀用例；`tests/test_memory.py` 的 thinking/tool 往返；`tests/test_session_evidence_compat.py` | capability profile 尚不含实时 MCP tool catalog、Skill/Agent 定义与健康状态；恢复上下文不等于未知外部副作用 exactly-once |
| U-110 Provider 韧性 | 部分完成 | retryable 分类、有限次数/总时长、指数退避、jitter 与 `Retry-After`；非重试错误立即失败；流已产生可消费事件后断开返回 `provider.partial_stream_ambiguous`，不静默重放；重试决策进入结构化事件 | `tests/test_provider_resilience.py`、`tests/test_structured_cli_contract.py` | 尚未实现按 Provider 隔离的熔断器或显式 fallback 链；未进行真实 429/DNS/5xx 实验 |

## 5. P2 状态矩阵（U-201～U-206）

| 编号 | 状态 | 当前结果 | 下一项可执行验收 |
|---|---|---|---|
| U-201 分发与升级 | 部分完成 | `pyproject.toml` 可构建 0.4.0 wheel；本轮已在独立 Windows venv 带依赖安装并通过 `eviforge --version/--help`；CI 使用锁文件覆盖 Windows/Ubuntu | 增加 N-1 配置/数据迁移与回滚 fixture；远端 clean-clone CI 尚待推送后执行 |
| U-202 可选强隔离 | 未完成 | Runtime 明确报告 `policy_only`，没有把路径/命令策略冒充 OS 沙箱 | 引入容器或独立 WSL executor，对抗测试证明执行面无法读取控制面凭据 |
| U-203 TUI 首用/可访问性 | 部分完成 | 有安全 `init`、错误码、诊断与命令帮助 | 增加首次向导、快捷键速查、可复制诊断面板并做未接触用户测试 |
| U-204 Live Provider | 部分完成 | 本地 fake SSE 覆盖请求协议、鉴权 header 和流式 usage；五次零费用评测 `35/35` 样本通过且认证头泄漏 0 | 在显式预算与 opt-in 下运行 OpenAI/Anthropic/compat smoke，保存 provider/model/date/usage |
| U-205 WSL 路径体验 | 部分完成 | WSL bootstrap/verify 文档化；Worktree 默认不再链接 `.venv`；在中文 DrvFS 路径使用独立 Linux venv 跑通 `1007` 项测试；VS Code 推荐 Remote-WSL | 在 WSL 内服务、Windows 主机服务、远端 HTTPS 三种 Provider 网络位置各跑一轮自检 |
| U-206 Provider 字段语义 | 部分完成 | 非 Anthropic 的 `thinking: true` 配置期拒绝；OpenAI Responses 的 `max_output_tokens` 有请求断言 | 对三个协议逐字段建立请求 golden test，做到 0 个静默无效字段 |

## 6. 用户可见能力

### 6.1 TUI

- ReAct 工具循环、文件/命令工具、Provider 选择和受治理权限模式。
- 单一 Plan 审批会话、内容哈希校验和 Evidence Gate。
- Memory 审阅、Skill 加载、Experience 治理、Session、多 Agent/Team、MCP 和 Recovery 命令。
- 受 Runtime Gateway 管理的审批、风险分级、Trace 和 Action Journal。

### 6.2 Headless/自动化

- `-p`、按名 Provider、会话恢复、Contract、精确授权 manifest、后台 `wait/cancel`。
- `--output text|json|jsonl`、版本化 final record、稳定退出码、结构化重试/Hook/Recovery 事件。
- `config/doctor/provider/mcp/session/recovery/data/capabilities` 运维命令。

### 6.3 DAG

- Explorer/Implementer/Verifier/Integrator 四类角色契约与依赖调度。
- write-set 冲突串行化、Verifier 只读能力、artifact/evidence、预算和 durable run record。
- 离线 validate/plan、结构化进度、status 和证据约束下的 resume。

## 7. 验证与复现

### 7.1 最终候选必须执行

```bash
uv sync --frozen --dev
uv run python -m compileall -q mewcode scripts
uv run pytest -q
uv run eviforge --help
uv run eviforge --version
uv run eviforge --config examples/config.offline.yaml config check --json
uv run eviforge --config examples/config.offline.yaml doctor --json
uv run eviforge dag validate examples/task-graph.json --json
uv run python scripts/run-usability-eval.py
```

WSL 另按 [WSL 使用指南](../WSL_USAGE.md) 执行 `scripts/bootstrap-wsl.sh` 与 `scripts/verify-wsl.sh`。所有最终数字必须来自同一候选 SHA；测试或评测失败时不得选择性删样本。

### 7.2 本轮实测结果

| 项目 | 当前状态 | 最终产物位置 |
|---|---|---|
| Windows 全量 pytest | `1006 passed, 1 skipped in 72.74s` | 当前工作副本本地终端；跳过 POSIX symlink 场景 |
| WSL/Ubuntu 全量 pytest | `1007 passed in 62.79s` | WSL2 Ubuntu 24.04，`scripts/verify-wsl.sh` |
| 五次 deterministic usability eval | `5/5` 轮、`35/35` 样本、Fake SSE header leak `0/5` | [usability-v1](../../evals/results/usability-v1/summary.md) |
| Wheel 安装 | 0.4.0 sdist/wheel 构建成功，独立 venv 带依赖安装后 help/version 成功 | 本地临时隔离环境 |
| live Provider smoke | 未执行 | 只有得到显式凭据、预算和联网授权后才生成 |

`scripts/run-usability-eval.py` 默认重复 5 次，保存环境、命令、stdout/stderr、JUnit、fake SSE 请求和聚合指标。它验证零费用发行主链，不衡量开放式编程能力。

## 8. 已有可复算机制指标

仓库已有一个与本轮最终发行评测分离的冻结机制实验：候选提交 `61078884557c1ea31bc5b621622373fa5d3a328f`，数据集 `risk_policy_mechanism_v1`，4 个独立 threat cluster、6 条 operation fixture、两臂各重复 20 次，共 240 个 ITT run，0 invalid。

| 指标 | legacy policy off | risk policy on | 配对变化 | 95% cluster CI |
|---|---:|---:|---:|---:|
| 全部 operation observations 的 false-allow incidence | 83.3% | 0% | −75pp | [−100pp, −25pp] |
| Fixture classification pass | 16.7% | 100% | +75pp | [+25pp, +100pp] |
| 本地决策 latency median / p95 | 0.029 / 0.049ms | 5.434 / 7.657ms | — | — |

原始结果和复算说明见 [mechanism-v1-clean/summary.md](../../evals/results/mechanism-v1-clean/summary.md)。这个 fixture 只覆盖路径保留区、工作区逃逸、forbidden tag 和 benign read；它证明“风险策略在该冻结数据集上的分类效果与本地开销”，不证明 OS 沙箱、生产危险率或 LLM 编程成功率。

## 9. 尚未完成且可直接执行的后续项

1. 将当前工作树形成候选提交，从公开仓库 fresh clone 后执行远端 Windows/Ubuntu CI。
2. 将 Session capability profile 从能力类别扩展为 MCP tool catalog、Skill/Agent 定义和健康状态的稳定指纹。
3. 跑齐外部 pane Team、Hook command、MCP stdio 的成功/异常/取消/超时矩阵；只有存在独立守护进程时才提供 durable detach。
4. 抽取覆盖 Provider、Memory、Skill、MCP、Hook、Session 和关闭顺序的完整 Composition Root；用 golden snapshot 比较三入口。
5. 为 `/experience validate` 接入宿主执行的确定性 replay runner，再评测固定任务集上的命中率、伤害率和重复错误率。
6. 补 MCP 驻留运行时热重连/disable，实施 Runtime 总配额与自动 retention。
7. 增加容器/独立 WSL executor 和 opt-in live Provider smoke；在完成前继续公开 `policy_only` 与“未执行 live”边界。

## 10. 对外表述边界

可以表述：

- “实现了可审计的审批、证据、恢复与经验治理基座”；
- “提供 TUI、结构化 headless 和可恢复 DAG 三类入口，并公开能力差异”；
- “在 240 次冻结 risk fixture 上，将 false-allow incidence 从 83.3% 降至 0%，代价是本地策略决策 p50 从 0.029ms 增至 5.434ms”。

暂不能表述：

- “支持所有模型/Provider 且生产稳定”——没有 live smoke；
- “TUI/headless/DAG 功能完全一致”——DAG 能力明确不同；
- “可安全运行不可信代码”——`policy_only` 不是 OS 隔离；
- “经验进化显著降低重复犯错率”——自动 replay 与固定任务对照实验尚未完成；
- “GitHub 远端 CI 已通过”——本轮证明的是同一修改工作副本的本地 Windows/WSL 与 fixture 评测，尚未推送新的 clean-clone run。

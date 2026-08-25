# EviForge 简历项目介绍

## 可直接粘贴版

**EviForge｜可审计、可恢复、可演进的终端智能编程助手**

项目周期：2026.06—2026.08

GitHub：<https://github.com/KNIGHT15235/EviForge>

技术栈：Python、asyncio、Pydantic、Textual、ReAct、MCP、SQLite WAL、Git Worktree、pytest、GitHub Actions

**项目概述：** 基于 ReAct 架构实现本地 Coding Agent，覆盖代码理解、任务规划、工具调用、文件修改、Shell 执行、多 Agent 协作和验证闭环；在传统终端助手基础上增加证据门禁、分级审批、持久恢复、结构化自动化与受治理的经验沉淀，强调“执行可控、结果可证、失败可恢复”。

**核心功能：**

- 支持 TUI、headless JSON/JSONL 与 Typed DAG 三类入口，统一接入 OpenAI、Anthropic 和 OpenAI-compatible Provider 配置，并显式展示入口能力差异。
- 提供文件检索/编辑、Shell、ToolSearch、Skill、MCP、Session、Memory 等工具与上下文能力，支持按名选择 Provider 和精确授权 manifest。
- 通过 PlanSession 管理计划草拟、审核、批准、执行和关闭，审批绑定 session、turn、文件路径及内容哈希，取消操作不产生执行权限。
- 以 Explorer、Implementer、Verifier、Integrator 角色运行依赖图，支持写集冲突串行化、只读验证、预算控制、结构化进度和证据约束恢复。
- 使用 SQLite WAL 保存 Task/Trace/Action Journal/Checkpoint；启动时识别 `UNCERTAIN` 外部副作用，默认在 Provider 初始化前阻断重复执行。
- 将 Memory 与经验候选置于 quarantine，经来源追踪、审阅、验证记录、人工确认后再 promote，并支持反馈、版本和 rollback。

**技术亮点：**

- **Agent 运行内核：** 基于 ReAct 构建异步工具调用循环，统一多 Provider 流式响应、上下文窗口与工具 Schema；在 TUI 和 headless 中组装 Memory、Skill、MCP、Session、sub-Agent/Team，并以公共生命周期接口等待、取消和回收后台任务，降低长任务资源悬挂风险。

- **Plan 与安全审计：** 设计带 session/turn/内容哈希绑定的 PlanSession 状态机，并将授权约束到精确 argv、cwd、文件范围、网络 host 与有效期；在 4 个 threat cluster、240 次冻结机制实验中，false-allow incidence 由 83.3% 降至 0%，分类通过率由 16.7% 提升至 100%。

- **记忆与经验治理：** 将用户级/项目级 Memory 统一为含 scope、source task/trace、状态和内容哈希的可审阅记录，采用 16,000 字符公平注入预算；会话证据仅生成 quarantine 经验候选，验证记录、人工确认、反馈和 rollback 共同控制 Skill 版本发布，避免未验证知识自动污染上下文。

- **Typed DAG 多 Agent：** 为 Explorer、Implementer、Verifier、Integrator 定义类型化输入输出与最小能力边界，按依赖并发调度并对 write-set 冲突串行化；支持离线图校验、artifact SHA-256 证据、结构化节点事件、能力/图漂移检测及断点恢复，已完成节点不重复执行，可写失败默认禁止自动重放。

- **自动化与故障恢复：** 设计版本化 RunResult JSON Schema、JSONL 事件流和稳定退出码族，Provider 重试遵循 `Retry-After`、次数与总时长上限，部分流中断返回 typed ambiguous 而非静默重放；五轮零费用发行评测实现 35/35 命令样本通过、5 个 Fake SSE 请求认证头泄漏为 0。

## 指标与表述边界

上述唯一“前后对比”数字来自仓库冻结的 `risk_policy_mechanism_v1`：4 个独立 threat cluster、6 条 operation fixture、两臂各重复 20 次，共 240 个 ITT run、0 invalid；cluster-paired effect 为 −75pp，95% CI 为 [−100pp, −25pp]。风险策略的本地决策 latency 为 p50/p95 `5.434/7.657ms`，legacy 为 `0.029/0.049ms`。原始数据和复算方法见 [机制评测摘要](../evals/results/mechanism-v1-clean/summary.md)。该结果不代表 OS 沙箱、生产危险率或开放式编程成功率。

本轮同一修改工作副本的工程验收结果：

- Windows 全量测试：`1006 passed, 1 skipped in 72.74s`；
- WSL2 Ubuntu 24.04：`1007 passed in 62.79s`；
- 五次发行评测：`5/5` 轮、`35/35` 命令样本通过；Provider Fake SSE 中位耗时 `4301.320ms`，认证头泄漏 `0/5`；
- 可复核证据：[usability-v1](../evals/results/usability-v1/summary.md)；
- Live Provider：`未执行`（只有取得显式凭据、联网授权和预算后才可改写）

简历中不要写“已实现 OS 级沙箱”“三入口能力完全一致”“支持所有真实 Provider”或“经验沉淀显著降低重复犯错率”；当前仓库没有对应实验依据。

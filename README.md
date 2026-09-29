# EviForge

[![CI](https://github.com/KNIGHT15235/EviForge/actions/workflows/ci.yml/badge.svg)](https://github.com/KNIGHT15235/EviForge/actions/workflows/ci.yml)

**可验证、可恢复、会进化的终端 Coding Agent。**

EviForge 在 [LikeCC](https://github.com/KNIGHT15235/likecc) 的 Python ReAct 内核上扩展了版本化计划审批、受治理的记忆与 Skill、Typed DAG 多 Agent 工作流，以及可供脚本读取的执行结果。Textual TUI 和非交互命令共用运行循环与服务装配。

这里的“会进化”指经验经过证据记录、人工确认和版本发布后影响后续任务；模型生成的经验只进入隔离候选区。这里的“可恢复”指保留检查点、核验漂移并拒绝危险重放，不承诺外部副作用的 exactly-once 执行。

## 相比 LikeCC

| 能力 | EviForge 的实现 |
| --- | --- |
| 统一运行时 | TUI / `-p` 都装配 Memory、Skill、MCP、Session、SubAgent、Team，提供等待、取消、回收接口 |
| 计划审批 | PlanSession 绑定 session、turn 与内容哈希；精确工具参数 / argv、cwd、文件范围、网络 origin 和有效期；执行前再次核验 |
| 经验治理 | SQLite 记录来源、状态、哈希、验证和审计；用户与项目共享 16,000 字符记忆预算；发布、撤销、反馈与回滚刷新上下文 |
| Typed DAG | Explorer / Implementer / Verifier / Integrator 强类型输入输出；离线校验、依赖并行与写集冲突串行、SHA-256 证据、持久化恢复 |
| 自动化 | RunResult JSON Schema、JSONL 事件、稳定退出码；有界 Provider 重试；部分输出中断返回 `ambiguous`，不静默重放 |
| 原有能力 | 三类 Provider 协议、六种代码工具、上下文压缩、Slash Commands、Skill、Hooks、SubAgent、Team、Git Worktree 和文件恢复继续复用 |

详细实现与验证证据见 [改进报告](docs/eviforge-improvement-report.md) 和 [测试说明](docs/verification.md)。

## 安装和运行

需要 Python 3.11+。推荐 Linux 或 Windows WSL；原有 `Bash.command` 依赖 `bash`，Worktree 需要 Git。`Bash.argv` 直接启动指定程序。

克隆本仓库并安装锁定依赖：

```bash
git clone https://github.com/KNIGHT15235/EviForge.git
cd EviForge
uv sync --locked
uv run --locked eviforge --help
```

复制 [配置示例](examples/config.example.yaml) 到目标项目的 `.eviforge/config.yaml`，填写真实服务地址和模型。`api_key: ""` 从 `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` 读取。也可在用户目录 `~/.eviforge/config.yaml` 配置。

```bash
uv run --locked eviforge
uv run --locked eviforge -p "阅读当前项目，概括主要模块"
uv run --locked eviforge -p "检查当前改动" --output json
uv run --locked eviforge -p "检查当前改动" --output jsonl
uv run --locked eviforge --mode plan
```

处理其他项目时，可以在本仓库执行 `uv tool install .`，随后切换到目标项目运行 `eviforge`。项目规则写入 `EVIFORGE.md`；TUI 内 `/help` 可查看 `/plan`、`/session`、`/compact`、`/memory`、`/skill`、`/tasks` 等命令。

默认 `default` 权限模式仍需确认写操作；非交互入口遇到未获许可的操作会拒绝，并以 `blocked` / 退出码 3 返回。选择 `acceptEdits` 只改变相应权限策略，显式 deny 和计划约束仍然有效。

## 计划、经验与 DAG

- [计划与自动化](docs/automation-and-plans.md)：审阅计划内容及 action manifest，按哈希批准；读取 RunResult 和 JSONL。
- [记忆与 Skill 治理](docs/governance.md)：候选 → 验证 → 人工确认 → 发布，反馈撤销和版本回滚。
- [Typed DAG](docs/typed-dag.md)：图校验、运行、证据、检查点恢复与显式重试。

以下命令不需要模型配置，也不会请求模型：

```bash
eviforge schema
eviforge dag schema
eviforge dag validate examples/dag-review.json
eviforge governance list
eviforge plan list
```

## 开发与验证

```bash
uv run --locked python -m pytest -q
uv run --locked python tests/verify_subagent.py
uv run --locked python -m scripts.update_schemas
uv build --no-sources
```

CI 执行原有与新增回归、独立 SubAgent 检查、构建及脱离源码的安装包验证。测试使用确定性模型替身和真实 SDK 的本地 HTTP/SSE 响应，并实际执行本地文件、子进程、Git、SQLite、MCP stdio 和 Textual 流程。实测数量、环境和限制以 [验证说明](docs/verification.md) 为准。

权限和路径检查属于应用层约束。精确 argv 批准的是一次程序启动，程序内部仍可产生文件或网络副作用；Worktree 也不是操作系统安全沙箱。经验验证记录由操作者提供，类型和内容哈希不等同于语义正确性证明。没有复现简历中的成功率、拦截率或付费模型指标。

## 来源与许可证

基于 LikeCC 初版代码演进，保留其 MIT 许可证。复制基线提交与逐文件哈希见 [来源记录](docs/likecc-baseline.json)。这是学习终端 Coding Agent 工作方式的独立实践，与 Anthropic 无官方关联。

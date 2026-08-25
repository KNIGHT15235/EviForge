# EviForge

EviForge 是一个可审计、可恢复、可演进的终端 Coding Agent。项目基于 ReAct
循环，支持 TUI/CLI、Plan 审批、MCP、Memory、Skill、子 Agent、Team 与 typed DAG，
并用宿主侧的权限、证据和恢复协议约束模型执行，而不是只依赖提示词自觉。

当前版本：`0.4.0`。Python 要求 `>=3.11`，推荐使用 `uv` 管理锁定环境。
`eviforge` 是首选命令；`mewcode` 仅作为迁移兼容入口保留。

## 核心能力

- **Evidence-first 完成门禁**：Requirement Contract 将验收条件绑定到无 shell 的
  精确 argv verifier；只有当前代码快照的 receipt 全部通过，任务才能返回 PASS。
- **Plan 与最小授权**：PlanSession 绑定本轮 Plan 路径、内容哈希和审批状态；文件、
  命令、cwd、网络 host 与有效期可进一步写入 automation manifest，参数漂移即拒绝。
- **Durable Recovery**：TaskRun、审批票据与 Action Journal 写入 host-owned SQLite；
  启动发现 `UNCERTAIN` 时先阻断 Provider、Hook、MCP 和 Agent 执行，再由用户审阅。
- **多 Agent DAG**：Explorer/Implementer/Verifier/Integrator 具有明确角色、依赖、
  write-set、预算与 Artifact 契约；支持离线校验、JSONL 进度、状态查询与证据化续跑。
- **受治理的经验沉淀**：Memory 先进入 quarantine，再审核、晋升、定向遗忘和导出；
  Experience Candidate 需经过独立 replay/验证记录才可发布为版本化 Skill，并可回滚。

## 三分钟开始

### Windows PowerShell

```powershell
git clone https://github.com/KNIGHT15235/EviForge.git
Set-Location eviforge
uv sync --frozen --dev
uv run eviforge init
```

编辑 `.mewcode/config.yaml`，填写真实模型名，并在当前终端设置模板中声明的环境变量：

```powershell
$env:OPENAI_API_KEY = "<your-key>"
uv run eviforge config check
uv run eviforge doctor
uv run eviforge
```

### WSL / Linux

```bash
git clone https://github.com/KNIGHT15235/EviForge.git
cd eviforge
bash scripts/bootstrap-wsl.sh
export UV_PROJECT_ENVIRONMENT="$HOME/.cache/eviforge/venv"
uv run --frozen --no-sync eviforge init
```

编辑配置并设置凭据后执行：

```bash
export OPENAI_API_KEY="<your-key>"
uv run --frozen --no-sync eviforge config check
uv run --frozen --no-sync eviforge doctor
uv run --frozen --no-sync eviforge
```

WSL 的 VS Code、解释器、网络和故障排查步骤见
[WSL 使用手册](./docs/WSL_USAGE.md)。不要在 WSL 中复用 Windows 的 `.venv`；仓库
默认也不会把虚拟环境 symlink 到 Agent Worktree。

## Provider 安全配置

推荐只在 YAML 中写环境变量名，不写密钥值：

```yaml
providers:
  - name: primary
    protocol: openai
    base_url: https://api.openai.com/v1
    model: replace-with-your-model-id
    api_key_env: OPENAI_API_KEY
    auth: required
```

本地免鉴权 OpenAI-compatible 服务应显式声明：

```yaml
providers:
  - name: local
    protocol: openai-compat
    base_url: http://127.0.0.1:11434/v1
    model: replace-with-tool-calling-model-id
    auth: none
```

安全边界如下：

- 官方 OpenAI/Anthropic 环境变量只绑定官方协议与官方 endpoint；
- 自定义或兼容 endpoint 不会隐式继承官方 Key，必须指定专用 `api_key_env`；
- `auth: none` 不发送 `Authorization`/`X-Api-Key`；
- Provider HTTP 客户端不自动跟随重定向，避免凭据跨 origin；
- `--config PATH` 只加载指定文件，不叠加用户或项目其他配置；
- 默认分层配置若隐式继承用户级 command/http Hook 或外部 MCP，会先要求显式
  `--trust-config`，可用 `config explain --json` 审查来源。

模板位于 [examples/config.wsl.yaml](./examples/config.wsl.yaml)。CI 使用单独的
[离线诊断配置](./examples/config.offline.yaml)，不会冒充真实模型配置。

## 常用 CLI

全局参数（应放在子命令之前）：

```text
--config PATH
--provider NAME
--mode default|acceptEdits|plan|dontAsk|bypassPermissions
--output text|json|jsonl
--trust-config
```

### 配置与诊断

```bash
uv run eviforge init
uv run eviforge config check --json
uv run eviforge config explain --json
uv run eviforge doctor --json
uv run eviforge capabilities --json
uv run eviforge --version
```

`init` 不覆盖已有配置；`config check` 与 `doctor` 不发起 Provider 请求。只有下面的
命令会执行一次显式、限时、可能计费的真实 Provider smoke：

```bash
uv run eviforge --config .mewcode/config.yaml provider test primary --timeout 20 --json
```

### Headless 自动化

```bash
uv run eviforge --provider primary --output json -p "分析仓库并给出三个最高风险"
```

带确定性完成门禁：

```bash
uv run eviforge --mode acceptEdits --output json \
  --contract examples/requirement-contract.json \
  -p "实现审核过的需求并完成验证"
```

带精确最小授权：

```bash
uv run eviforge --mode default --output json \
  --grant-manifest examples/automation-manifest.json \
  -p "执行 manifest 允许的变更"
```

机器输出遵循 [RunResult JSON Schema](./schemas/run-result.schema.json)。退出码族为：
`0` 成功、`1` 运行失败、`2` 配置、`3` 鉴权、`4` 网络、`5` 权限、`6` Evidence
Gate、`7` 预算、`70` 内部错误。后台 Agent 默认有限等待，也可选择
`--background-policy cancel`；当前不声称跨进程 durable detach。

### Session

```bash
uv run eviforge session list --json
uv run eviforge session inspect <session-id> --json
uv run eviforge session export <session-id> --format markdown
uv run eviforge session resume <session-id> -p "继续处理剩余任务" --json
uv run eviforge session delete <session-id> --confirm --json
```

Resume 恢复完整的 user/assistant/tool-use/tool-result/thinking 链与关联元数据，不会重放
进程或未知外部副作用。工作区、脱敏 Provider profile 或能力类别变化会在网络探测前
fail-closed；人工审阅后才可加 `--allow-session-drift`。能力画像当前不包含实时 MCP
tool catalog 或 Skill/Agent 定义指纹。

### Durable Recovery

```bash
uv run eviforge recovery status --json
uv run eviforge recovery inspect <action-id> --json
uv run eviforge recovery ack <action-id> --note "已在外部系统核对" --confirm --json
uv run eviforge recovery retry <action-id> --confirm --json
```

只有可用哈希证明安全的 staged file replace 支持 CLI retry；`UNCERTAIN` 网络/外部
操作永不自动重试。`--allow-recovery` 不是修复操作，只应在人工完成 reconcile 后使用。

### Typed DAG

```bash
uv run eviforge dag validate examples/task-graph.json --json
uv run eviforge dag plan examples/task-graph.json --json
uv run eviforge --mode acceptEdits dag run examples/task-graph.json \
  --max-concurrency 2 --total-tokens 3000 --wall-time 360 --jsonl
uv run eviforge dag status <run-id> --json
uv run eviforge --mode acceptEdits dag resume examples/task-graph.json <run-id> --jsonl
```

Resume 会重新验证 graph hash、工作区/Provider/权限能力哈希、Artifact、验收 receipt、
写入清单与当前文件哈希；已验证节点不重复执行，不确定写节点不自动 replay。Token
限制当前是“分派前预留 + 完成后核算”，不是单次进行中请求的硬中断。

### MCP

```bash
uv run eviforge mcp status --json
uv run eviforge mcp list --json
uv run eviforge mcp test <server-name> --timeout 15 --json
uv run eviforge mcp reconnect <server-name> --json
```

多服务器并行探测且各自有总超时；坏服务不会阻断健康服务。Headless `reconnect` 是
一次性进程诊断，不能持久修改另一个 TUI 进程；TUI 中可用
`/mcp enable|disable|test|reconnect <server>` 管理当前会话。

### 运行数据

```bash
uv run eviforge data path --json
uv run eviforge data stats --json
uv run eviforge data export support-bundle.zip --json
uv run eviforge data prune --older-than-days 30 --dry-run --json
uv run eviforge data prune --older-than-days 30 --apply --confirm --json
```

默认 support bundle 只包含经过脱敏的安全运行元数据，并在 manifest 中列出被排除的
未知/CAS/二进制内容；数据库必须显式选择。Prune 默认 dry-run，不删除项目源码、
Session、Memory 或数据库。

## TUI 命令

- `/plan <任务>`：进入本轮 PlanSession；Escape 只取消，不会变成执行批准。
- `/permission`：查看或切换权限模式。
- `/memory list|show|pending|promote|reject|forget|clear|export`：按 scope 治理记忆。
- `/experience`（别名 `/xp`）：审查 Candidate、登记验证、promote、feedback、rollback。
- `/skill`：查看和加载 Skill；`mode/model/allowedTools` 在 LoadSkill/fork 路径统一校验。
- `/mcp`：查看、测试和启停当前会话 MCP 工具。
- `/recovery`：处理启动时发现的中断操作。
- `/tasks`、`/trace`、`/worktree`、`/review`、`/status`：任务、审计和工作区操作。

Memory 自动提取只写 quarantine candidate，不再让模型全文覆盖已激活记忆。经验 Skill
发布要求持久化验证记录与门禁；当前 `/experience validate` 接收的是外部确定性 replay
结果，不会伪装成系统已经自动执行了 replay。

## 验证与评测

```bash
uv sync --frozen --dev
uv run python -m compileall -q mewcode scripts
uv run pytest -q
uv run python scripts/run-usability-eval.py
```

当前工作副本于 2026-08-24 完成验证：Windows 为 `1006 passed, 1 skipped`
（72.74s），WSL2 Ubuntu 24.04 为 `1007 passed`（62.79s）；0.4.0 wheel 已在
独立虚拟环境完成带依赖安装并启动。五轮确定性发行评测为 `5/5` 轮、`35/35`
命令样本通过，Fake SSE 认证头泄漏 `0/5`。可提交的脱敏证据见
[usability-v1](./evals/results/usability-v1/summary.md)。这些结果来自当前修改工作副本，
不等同于 GitHub 远端 clean-clone CI 已通过。

评测脚本至少重复 5 次，保存 `environment.json`、`commands.jsonl`、`metrics.json`、
JUnit、stdout/stderr 和本地 Fake SSE 请求摘要。它验证真实 SDK 协议与流解析，但
`auth:none` loopback fixture 不是 live Provider，也不证明开放式 Coding 成功率。

CI 在 Windows 与 Ubuntu 上执行 frozen sync、全量测试、离线诊断、DAG validate 和
五轮确定性评测，定义见 [.github/workflows/ci.yml](./.github/workflows/ci.yml)。

## 明确边界

- `protection_mode=policy_only` 是防误操作策略，不是容器或 OS 安全沙箱；不要在同一
  OS 用户下运行不可信代码并宣称强隔离。
- 当前没有 live OpenAI/Anthropic 发布证据；真实兼容性必须用用户显式预算执行
  `provider test` 后单独记录。
- TUI 与 headless 共享大部分 Memory/Skill/MCP/Session/Hook 组装能力，但 DAG 节点
  当前不注入 MCP 和 inline Skill；`capabilities --json` 会公开这些差异。
- Provider 只在尚未产生可消费流时有限重试；部分流断开返回
  `provider.partial_stream_ambiguous`，不会静默重放。未实现自动跨端点 fallback。
- Session resume 不等于 exactly-once；未知 shell/MCP/API 副作用仍以 `UNCERTAIN`
  处理。
- 任何“性能提升 X%”或“重复犯错率下降”都必须对应仓库内原始评测产物，不能把目标值
  或 mock 通过率包装成真实模型收益。

更完整的实现状态与测试证据见
[实施与验证报告](./docs/implementation/README.md)，原始问题与验收定义保留在
[使用体验优化方案](./docs/USABILITY_OPTIMIZATION_PLAN.md)。

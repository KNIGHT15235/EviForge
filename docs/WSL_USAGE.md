# EviForge：VS Code Remote-WSL 使用手册

本文说明如何从公开仓库在 WSL 中安装、配置、运行和验收 EviForge。所有命令均从
仓库根目录执行，不依赖作者电脑路径。

## 1. 环境边界

- WSL 2，推荐 Ubuntu；
- Python `>=3.11`；
- Git、VS Code 与 Remote - WSL 扩展；
- `uv`；
- Linux 虚拟环境与 Windows 虚拟环境必须分开。

仓库建议把 WSL 环境放到：

```text
~/.cache/eviforge/venv
```

这样既不会复用 Windows `.venv/Scripts/python.exe`，也能减少把大量依赖文件写入
DrvFS 的成本。Worktree 默认不 symlink `.venv`。

## 2. 克隆与 bootstrap

```bash
git clone https://github.com/KNIGHT15235/EviForge.git
cd eviforge
```

若 WSL 内还没有 uv，可安装后确认当前 shell 能找到它：

```bash
curl --proto '=https' --tlsv1.2 -LsSf https://astral.sh/uv/install.sh -o /tmp/uv-installer.sh
sh /tmp/uv-installer.sh --no-modify-path
export PATH="$HOME/.local/bin:$PATH"
uv --version
```

执行仓库脚本：

```bash
bash scripts/bootstrap-wsl.sh
```

脚本会：

1. 确认正在 WSL 内运行；
2. 使用 `UV_PROJECT_ENVIRONMENT`，默认指向 `~/.cache/eviforge/venv`；
3. 执行锁文件一致性检查和开发依赖同步；
4. 验证核心模块导入和 `eviforge --help`。

后续终端应设置相同环境：

```bash
export UV_PROJECT_ENVIRONMENT="$HOME/.cache/eviforge/venv"
export VIRTUAL_ENV="$UV_PROJECT_ENVIRONMENT"
export PATH="$UV_PROJECT_ENVIRONMENT/bin:$PATH"
```

可把这三行写入项目专用 shell 配置，但不要让多个 checkout 共用同一个 editable
environment；并行维护多个 checkout 时，为每个 checkout 指定独立路径。

## 3. VS Code Remote-WSL

从 WSL 仓库目录启动：

```bash
code .
```

检查 VS Code 左下角显示 `WSL: ...`。仓库的
[`.vscode/extensions.json`](../.vscode/extensions.json) 推荐 Remote - WSL、Python
和 Pylance；[`.vscode/settings.json`](../.vscode/settings.json) 已配置：

- Linux 解释器 `${HOME}/.cache/eviforge/venv/bin/python`；
- pytest 目录和 WSL 终端环境变量；
- LF 换行。

若解释器没有自动选择：

1. 打开 `Python: Select Interpreter`；
2. 输入 `~/.cache/eviforge/venv/bin/python`；
3. 在 VS Code WSL 终端执行
   `python -c "import sys; print(sys.executable)"`，确认路径不以 `.exe` 结尾。

## 4. 创建配置

首次使用：

```bash
uv run --frozen --no-sync eviforge init
```

该命令创建 `.mewcode/config.yaml`，已有文件时拒绝覆盖。也可以复制模板：

```bash
mkdir -p .mewcode
cp -n examples/config.wsl.yaml .mewcode/config.local.yaml
chmod 600 .mewcode/config.local.yaml
```

默认分层顺序为用户配置、项目配置、项目 local 配置；显式 `false/default/[]` 可以降低
或清空上层值。若希望完全隔离，使用全局参数：

```bash
uv run --frozen --no-sync eviforge \
  --config .mewcode/config.local.yaml config explain --json
```

`--config` 必须放在子命令前，并且只加载指定文件。默认分层若隐式继承用户级
command/http Hook 或外部 MCP，执行入口会 fail-closed；先检查 `config explain`，
确认后显式传入 `--trust-config`。

### OpenAI

```yaml
providers:
  - name: openai-main
    protocol: openai
    base_url: https://api.openai.com/v1
    model: replace-with-your-model-id
    api_key_env: OPENAI_API_KEY
    auth: required
```

```bash
export OPENAI_API_KEY="<your-key>"
```

### Anthropic

```yaml
providers:
  - name: anthropic-main
    protocol: anthropic
    base_url: https://api.anthropic.com
    model: replace-with-your-model-id
    api_key_env: ANTHROPIC_API_KEY
    auth: required
    thinking: false
```

```bash
export ANTHROPIC_API_KEY="<your-key>"
```

### 本地 OpenAI-compatible

```yaml
providers:
  - name: local
    protocol: openai-compat
    base_url: http://127.0.0.1:11434/v1
    model: replace-with-tool-calling-model-id
    auth: none
```

自定义 endpoint 不会读取 `OPENAI_API_KEY`。需要鉴权时，声明该服务自己的
`api_key_env`；免鉴权则使用 `auth: none`，不要设置占位密钥。

如果服务运行在 Windows 而 WSL 的 `127.0.0.1` 无法访问，先用服务自身的监听设置和
Windows 防火墙确认允许的接口，再从 WSL 查询默认网关进行显式连接：

```bash
ip route show default
curl --fail --show-error --max-time 3 http://<reviewed-host>:<port>/v1/models
```

不要为了省事把服务绑定到不受信任网络；MCP/Provider host 仍应是明确审核的地址。

## 5. 离线诊断

编辑模型名后：

```bash
uv run --frozen --no-sync eviforge config check --json
uv run --frozen --no-sync eviforge doctor --json
uv run --frozen --no-sync eviforge capabilities --json
```

这些命令检查 schema、变量引用、Provider 凭据来源、Python/uv/Git、工作目录、MCP
命令、数据目录和 Recovery，不调用模型。仓库的 CI 离线配置可单独验证：

```bash
uv run --frozen --no-sync eviforge \
  --config examples/config.offline.yaml config check --json
```

真实 Provider smoke 是显式联网操作，可能计费：

```bash
uv run --frozen --no-sync eviforge \
  --provider openai-main provider test --timeout 20 --json
```

## 6. 启动方式

### TUI

```bash
uv run --frozen --no-sync eviforge --provider openai-main
```

常用命令：

- `/plan <任务>`；
- `/review`、`/status`、`/permission`；
- `/memory`、`/experience`、`/skill`、`/mcp`；
- `/recovery`、`/tasks`、`/trace`、`/worktree`。

### Headless

```bash
uv run --frozen --no-sync eviforge \
  --provider openai-main --mode default --output json \
  -p "分析当前仓库并列出三个最高风险"
```

`default` 下需要人工审批的写入会拒绝；CI 中应使用精确 automation manifest，而不是
长期开启 `dontAsk` 或 `bypassPermissions`。

### DAG

先离线验证：

```bash
uv run --frozen --no-sync eviforge \
  dag validate examples/task-graph.json --json
```

执行：

```bash
uv run --frozen --no-sync eviforge \
  --provider openai-main --mode acceptEdits \
  dag run examples/task-graph.json --max-concurrency 2 --jsonl
```

崩溃后使用报告中的 run id：

```bash
uv run --frozen --no-sync eviforge dag status <run-id> --json
uv run --frozen --no-sync eviforge \
  --provider openai-main --mode acceptEdits \
  dag resume examples/task-graph.json <run-id> --jsonl
```

DAG resume 只复用证据仍有效的成功节点。写节点状态不确定、Artifact/receipt/hash 失效、
工作区或能力 profile 漂移时均拒绝自动续跑。

## 7. MCP 与 Hook

WSL 中的 stdio MCP command 必须是 Linux 可执行文件；不要配置 Windows `.exe` 或
Windows 虚拟环境脚本。先检查：

```bash
command -v uvx
uv run --frozen --no-sync eviforge mcp status --json
uv run --frozen --no-sync eviforge mcp test <server-name> --json
```

MCP 子进程只继承最小 PATH 与配置显式声明的 env。HTTP header/env 的值会在运维输出中
脱敏。坏服务按单服务超时降级，不阻塞健康服务。

Hook command 使用 shell，只能加载可信配置；不支持的 event/action 在配置期拒绝。
Headless 与 DAG 只接受其实际分派的生命周期事件。存在未处理 Recovery 时，startup
Hook、MCP 与 Provider metadata 都不会启动。

## 8. 验证

完整 WSL 验收：

```bash
bash scripts/verify-wsl.sh
```

本轮在 WSL2 Ubuntu 24.04 中从中文 DrvFS 路径执行该脚本，使用
`~/.cache/eviforge/venv` 独立 Linux 环境，结果为 `1007 passed in 62.79s`；
同一工作副本的 Windows 结果为 `1006 passed, 1 skipped in 72.74s`。脱敏评测证据见
[usability-v1](../evals/results/usability-v1/summary.md)。这证明本机 Remote-WSL 开发链
已打通，不替代远端 fresh-clone CI 或 live Provider 验收。

手工执行：

```bash
export UV_PROJECT_ENVIRONMENT="$HOME/.cache/eviforge/venv"
uv sync --frozen --dev
uv run python -m compileall -q mewcode scripts
uv run pytest -q -p no:cacheprovider
uv run python scripts/run-usability-eval.py
```

评测使用 loopback Fake SSE，不需要用户 Key；它验证真实 SDK 请求、流式解析、usage、
`auth:none` 无认证头，以及 help/version/config/doctor/DAG/release contract。不要把该结果
写成 live Provider 或开放式 Coding 能力。

## 9. 数据位置

- 项目配置、Session、Plan 与项目 Memory：`<repo>/.mewcode/`；
- 用户配置、权限、Skill 与用户 Memory：`~/.mewcode/`（兼容命名空间）；
- Linux host-owned Runtime/Evidence/Evolution/日志：
  `~/.local/share/EviForge/`；
- 本手册建议的虚拟环境：`~/.cache/eviforge/venv`。

查看实际路径与安全导出：

```bash
uv run --frozen --no-sync eviforge data path --json
uv run --frozen --no-sync eviforge data stats --json
uv run --frozen --no-sync eviforge data export support-bundle.zip --json
```

## 10. 常见问题

### `uv: command not found`

```bash
export PATH="$HOME/.local/bin:$PATH"
command -v uv
```

### VS Code 仍显示 Windows Python

关闭普通 Windows 窗口，从 WSL 终端重新执行 `code .`，再选择
`~/.cache/eviforge/venv/bin/python`。

### `No config file found`

```bash
uv run --frozen --no-sync eviforge init
```

### Key 已设置但仍报凭据错误

```bash
test -n "${OPENAI_API_KEY:-}" && echo configured || echo missing
```

确认变量存在于启动 EviForge 的同一个 WSL shell，并检查 YAML 的 `api_key_env` 名称。
自定义 endpoint 不会继承官方变量。

### 启动后提示 Recovery blocked

```bash
uv run --frozen --no-sync eviforge recovery status --json
uv run --frozen --no-sync eviforge recovery inspect <action-id> --json
```

先核对真实外部状态，再 ack 或对哈希可证明安全的 file replace 执行 retry。不要直接删除
Runtime 数据来绕过阻断。

### DrvFS 上测试较慢

把环境保留在 WSL ext4；长期开发也可将 Git checkout 克隆到 `~/src/eviforge`。不要
直接复制未提交工作树并把它当作 clean-clone 验收。

## 11. 不应误解的能力

- `policy_only` 不是 OS 沙箱；
- Provider retry 不会在部分流后自动重放，也没有自动跨 endpoint fallback；
- DAG token budget 不是单次在途请求的硬切断；
- Session resume 恢复上下文，不恢复进程，也不承诺未知外部副作用 exactly-once；
- DAG 当前不注入 MCP 与 inline Skill；
- 本仓库默认测试不调用 live OpenAI/Anthropic。

发布状态、Windows/WSL 实测数和评测产物路径以
[实施与验证报告](./implementation/README.md) 为准。

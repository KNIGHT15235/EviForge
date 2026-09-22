# LikeCC

一个学习 Claude Code 工作方式的 Python 终端编程助手实验项目，也是 **EviForge 的早期功能基线**。它围绕代码阅读、工具执行、上下文管理和子 Agent 分工，探索终端 Coding Agent 的实现方式。

本项目为独立学习实践，与 Anthropic 无官方关联，也不声称复现 Claude Code 的完整内部实现。

## 能力

| 模块 | 已有能力 |
| --- | --- |
| Agent 运行 | 异步工具调用循环、流式响应、Textual 终端界面，以及 `-p` 非交互入口 |
| 模型接入 | Anthropic、OpenAI、OpenAI 兼容协议；通过配置选择模型与服务端点 |
| 代码工具 | 文件读取与编辑、目录搜索、命令执行；编辑前检查文件是否读过、是否被修改 |
| 上下文 | 会话保存与恢复、上下文压缩、用户和项目记忆 |
| 扩展 | Markdown Agent 定义、Skill、Hook 与 MCP 工具接入 |
| 子 Agent | 定义式独立上下文、Fork 历史继承、后台任务通知、独立权限与文件读取状态 |
| 协作与隔离 | 团队任务和消息、Git Worktree 工作目录隔离 |
| 权限 | 权限模式、显式规则、文件路径检查；子 Agent 权限不超过父 Agent |

## 安装

需要 Python 3.11 或更高版本、uv；使用 Worktree 时还需要 Git。推荐 Linux 或 Windows 的 WSL 环境；Bash 工具依赖系统中的 `bash`。克隆仓库并安装锁定依赖：

```bash
git clone https://github.com/KNIGHT15235/likecc.git
cd likecc
uv sync --locked
```

## 配置与运行

在需要处理的项目目录中创建 `.likecc/config.yaml`，参考 [配置示例](examples/config.example.yaml)。将 `YOUR_MODEL` 和服务地址替换为实际可用的配置。

API Key 建议通过环境变量提供：`openai` 和 `openai-compat` 使用 `OPENAI_API_KEY`，`anthropic` 使用 `ANTHROPIC_API_KEY`。示例中的 `api_key: ""` 会读取对应环境变量。

```bash
# Bash / Zsh：替换为自己的凭据
export OPENAI_API_KEY="YOUR_API_KEY"
uv run --locked likecc
```

```powershell
# PowerShell：替换为自己的凭据
$env:OPENAI_API_KEY = "YOUR_API_KEY"
uv run --locked likecc
```

上述 `uv run` 命令在本仓库根目录执行。处理其他目录时，可先从本地源码安装命令，再到目标目录运行：

```bash
uv tool install .
# 切换到目标项目，并为该项目准备 .likecc/config.yaml
likecc
```

配置也可以放在用户目录的 `~/.likecc/config.yaml`。加载顺序为用户配置、项目 `.likecc/config.yaml`、项目 `.likecc/config.local.yaml`；各层按字段合并。真实凭据和个人配置不应提交到仓库。

常见入口：

```bash
uv run --locked likecc --help
uv run --locked likecc -p "阅读当前项目，概括主要模块及其职责"
uv run --locked likecc --mode plan
```

终端界面中可使用 `/help` 查看命令，例如 `/plan`、`/permission`、`/session`、`/compact`、`/memory` 和 `/skill`。项目规则可写入 `LIKECC.md`。

默认权限模式是 `default`。Fork 需要开启 `enable_fork`；定义式子 Agent 默认同步运行，可由定义或调用参数选择后台运行。后台任务不弹出人工确认，未获允许的操作会返回拒绝。

## 目录

```text
likecc/
  agent.py          Agent 运行循环
  client.py         模型协议适配
  tools/            文件、命令与协作工具
  agents/           子 Agent 定义、Fork 与后台任务
  permissions/      权限模式、规则与路径检查
  context/          上下文预算与压缩
  memory/           记忆、项目规则与会话
  skills/           Skill 加载与执行
  hooks/            生命周期扩展
  mcp/              MCP 工具接入
  teams/            团队任务与消息
  worktree/         Git Worktree 管理
tests/              自动化测试与验证脚本
examples/           可公开的配置示例
docs/               验证说明
```

## 验证与边界

LikeCC 在 Python 3.11 和 3.12 下均通过 **720 项 pytest 测试**，另通过 **90 项独立 SubAgent 检查**。完整范围与复现命令见 [验证说明](docs/verification.md)。

- 已验证用例以本地 fake 模型、临时文件和临时 Git 仓库为主，不代表真实模型服务、所有操作系统或外部 MCP 服务都已验证。
- TUI 与 `-p` 共用核心 Agent 能力；`-p` 尚未装配 TUI 的 Memory、Session、Skill 与 MCP 全套管理器。
- Hook 的 `agent` 执行器、插件来源的 Agent 加载尚未实现。
- EviForge 后续版本规划的证据验收、持久化恢复和经验治理不属于本初版已实现的能力。
- Fork 保留可复用的请求前缀；实际缓存命中取决于服务端、模型和请求内容，不承诺固定命中率。
- 子 Agent 的报告格式由提示词约束；Worktree 提供工作目录隔离，权限检查不等同于操作系统级安全沙箱。
- 本仓库公开版本仅包含源码、测试与示例，不包含开发阶段的审计备份、运行日志或个人配置。

## 许可证

采用 [MIT License](LICENSE)。

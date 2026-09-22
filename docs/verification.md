# 验证说明

以下为 LikeCC 0.2.0 在 2026-09-22 完成的发布验证。测试使用本地 fake 模型、临时文件和临时 Git 仓库；不需要模型 API Key。

## 已有结果

| 验证对象 | 结果 | 范围 |
| --- | --- | --- |
| pytest / Python 3.11.16 | 720 passed，0 skipped | 完整单元与集成回归 |
| pytest / Python 3.12.3 | 720 passed，0 skipped | 全新锁定依赖环境中的完整回归 |
| 独立 SubAgent 脚本 / Python 3.11 | 90/90 | Agent 定义、工具过滤、Fork、任务通知及相关契约 |
| 安装 | `uv sync --locked --dev` 通过 | 从公开 PyPI 安装；依赖版本和文件哈希保持不变 |
| 分发包 | wheel 与 sdist 构建通过 | 独立 Python 3.11 环境安装锁定依赖和 wheel 后，脱离源码检查 CLI、样式、4 个 Agent、4 个 Skill 及动态工具资源通过 |
| 命名与发布文件 | 检查通过 | 模块、CLI、配置目录、指令文件、环境变量、动态资源与测试引用一致；旧标识无残留 |

验证环境为 Ubuntu 24.04 / WSL。Provider 用例通过真实 SDK 加本地 MockTransport 验证，MCP 用例使用本地 stdio 服务；没有调用真实模型服务或外部 MCP 服务。公开版本不包含开发机器上的审计备份、私有配置和运行日志。

## 本地复现

在仓库根目录执行：

```bash
uv sync --locked
uv run --locked pytest -q
uv run --locked python tests/verify_subagent.py
uv run --locked python -m compileall -q likecc
uv run --locked likecc --help
```

测试需要 Python 3.11 或更高版本。Git / Worktree 集成用例需要本地 Git 可用；应以测试输出中的实际通过、跳过和失败数量为准。依赖安装可能需要访问包源，测试中的模型回复由本地 fake 提供。

## 重点回归

- 定义式子 Agent 默认同步；Fork 始终后台运行，结果通过任务通知回传。
- Fork 保留父请求的 system、历史和工具 schema；独立来源标记阻止递归委派，引用文档中的标签不会误触发限制。
- 子 Agent 的权限模式受父 Agent 上界约束；配置策略和本地拒绝规则保留，父会话的临时允许审批不自动继承。显式拒绝优先于安全命令的自动放行。
- 子 Agent 的文件缓存与读取记录独立。其他 Agent 修改文件后重新读取应看到新内容，未读过的新内容不能被静默覆盖；相同 mtime 也需要核对内容。
- ToolSearch 和 LoadSkill 使用子 Agent 自己的状态；Hook、客户端等按职责共享。
- 显式 Worktree 隔离、后台生命周期、任务取消和资源回收有相应回归用例。

## 验证边界

这些测试证明的是已覆盖输入下的程序行为，不用于推导模型任务成功率、生产环境稳定性或固定的 prompt cache 命中率。真实模型鉴权、端点兼容性、外部工具副作用和具体终端显示仍需在目标环境中验证。

统一入口为 Python 包 `likecc`、命令 `likecc`、配置目录 `.likecc`、项目规则 `LIKECC.md`，以及 `LIKECC_*` 团队协作环境变量。CI 会重新执行全量测试、独立 SubAgent 检查、构建和安装包验证。

本初版尚未实现 Hook 的 `agent` 执行器和插件来源的 Agent 加载；headless 尚未装配 TUI 的 Memory、Session、Skill、MCP 全套管理器。权限检查和 Worktree 不提供操作系统级安全沙箱，EviForge 后续版本的证据验收、持久化恢复与经验治理也不在本版的验证范围内。

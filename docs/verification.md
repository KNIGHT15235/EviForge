# EviForge 0.1.0 验证说明

验证日期：2026-09-22。环境：Ubuntu 24.04 / WSL。所有测试在真实项目代码上执行；模型部分使用确定性替身或真实 SDK + 本地 HTTP/SSE 响应。

## 最终结果

| 验证 | 结果 |
| --- | --- |
| Python 3.11.16 全量 pytest | **893 passed，0 failed，0 skipped**；57.37 秒 |
| Python 3.12.3 全量 pytest | **893 passed，0 failed，0 skipped**；80.62 秒；EviForge 独立锁定依赖环境 |
| 独立 SubAgent 检查 | **90/90**；Python 3.12.3 |
| 安装 | `uv sync --locked --python 3.12` 成功 |
| 分发构建 | wheel 与 sdist 成功 |
| 脱离源码验证 | 独立 Python 3.12 环境安装锁定运行依赖及 wheel 后，CLI、Schema、内置 Agent / Skill、动态资源和无 Provider 配置的治理命令通过 |
| 原项目完整性 | LikeCC 基线 178 文件哈希全部一致，原项目工作区无变更 |

893 项包含保留的 720 项 LikeCC 回归和 173 项新增验收。原测试仅随项目重命名更新引用，并调整 SDK 重试构造测试替身，以及将旧 Hook Agent 空实现断言改为“无运行时不能执行”；不通过删除原有断言或跳过功能取得通过。

机器统计和逐测试模块数量见 [validation-results.json](validation-results.json)。本地完整 JUnit 报告保存在 `.eviforge/verification/pytest311.xml`、`.eviforge/verification/pytest312.xml`，运行数据目录不纳入源码。

## 覆盖范围

- 原有：多 Provider、序列化、六种代码工具、ReAct、权限、Prompt、上下文预算与压缩、会话恢复、Slash Commands、Skills、Hooks、MCP、SubAgent、Fork、Team、Worktree、文件编辑隔离。
- 计划：session / turn / 内容哈希、参数 / cwd / 路径 / origin、过期、一次性消费、拒绝优先、并发 grant、工具禁用和替换、子 Agent 上界、冻结界面审批与旧事件回放。
- 治理：真实 SQLite、用户/项目隔离、16,000 字符含包装预算、公平注入、隔离候选不可见、验证/确认/发布、负反馈、撤销、回滚、Skill fork 与摘要/恢复附件中的撤销。
- DAG：四角色强类型输入输出、图校验、实际 Agent、依赖并行及冲突串行、真实文件与命令、证据哈希、能力漂移、节点恢复、不重跑完成节点、可写失败拒绝重放，以及写入后审计/提交失败。
- 自动化：真实 CLI text / JSON / JSONL、退出码、session 恢复、失败通知收口、Hook Agent、双 MCP stdio 服务跨任务关闭、并发关闭和内部任务回收。
- Provider 故障：三个真实 SDK 适配的五类 SSE 部分中断、429 后恢复、Retry-After、次数/时间上限、空流、thinking/工具片段、非法工具 JSON、内容过滤及输出续写耗尽。

真实本地文件、Bash / argv 子进程、Git 仓库、SQLite、MCP stdio 和 Textual `run_test` 都参与了集成验证。tmux / iTerm2 的外部终端适配保留原有模拟测试；本次没有在 macOS 的 iTerm2 或每一种实际终端执行。

## 复现

```bash
uv sync --locked
uv run --locked python -m pytest -q
uv run --locked python tests/verify_subagent.py
uv run --locked python -m scripts.update_schemas
uv build --no-sources
```

脱离源码验证分发包（路径按环境调整）：

```bash
uv export --locked --no-dev --no-emit-project --output-file /tmp/eviforge-requirements.txt
uv venv --python 3.12 /tmp/eviforge-wheel
uv pip install --python /tmp/eviforge-wheel/bin/python --require-hashes -r /tmp/eviforge-requirements.txt
uv pip install --python /tmp/eviforge-wheel/bin/python --no-deps dist/eviforge-0.1.0-py3-none-any.whl
/tmp/eviforge-wheel/bin/python scripts/check_distribution.py
```

该脚本切换到临时 HOME 和临时工作目录，以隔离导入方式检查安装包，避免从源码目录误导入。CI 执行同类测试、构建和安装验证。

## 修复记录与解释边界

实现过程中发现并修复的问题、对应执行边界见 [改进报告](eviforge-improvement-report.md)。所有最终验收项通过后才形成上述统计；先前的失败运行不冒充最终通过结果。

这些结果验证已覆盖输入下的程序行为，不是模型任务成功率实验。没有请求真实付费模型或外部 MCP，也没有复现简历中的数值指标。普通 Agent 的正常完成不等同于代码质量证明；严格节点结果由 Typed DAG 与验证节点检查。类型、哈希、应用层权限和 Worktree 不提供操作系统级进程沙箱。

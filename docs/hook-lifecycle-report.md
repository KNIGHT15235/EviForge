# EviForge Hook 六事件完善报告

日期：2026-10-09。基于提交 `27c66665abeb4e0f49b7b874b38b2ad64abdbf06`。

## 验收目标与结果

本次目标是 6 个生命周期事件、4 类业务动作，共 10 组映射；保留原有证据约束、轮末验收和最终报告，合计 13 条默认规则。所有动作已接入实际 Agent 运行链路，同时覆盖 TUI 与 headless。

当前 Hook 情况：session_start 做安全检查；turn_start 做安全检查、发送通知；pre_tool_use 做安全检查；post_tool_use 做安全检查、自动提交；turn_end 做代码验收、记录日志、发送通知；session_end 做最终证据报告、自动提交、发送通知；pre_send 注入证据约束提示词。

## 对照与保留

本地原版提供通用事件、条件和 command/prompt/http/agent 执行框架，没有可直接复制的完整默认动作映射；原版 agent 执行器还是占位实现。当前 EviForge 已有完整的 Runtime Agent 执行器和四条默认规则，采用增量扩展而非回退实现。

保留敏感文件保护、验收要求提示词、Python AST/Git whitespace/显式项目检查、证据指纹与报告、受控完成阶段重试、只读工具并行、自定义 Hook 条件/once/async/scope、多 Provider 及现有权限/Plan/MCP 约束。

## 主要改进

- 新增会话/轮次路径安全检查与基线；成功写入后的代码验证、归属哈希和本地自动提交。
- 新增轮末 JSONL 日志、三类本地通知；TUI 通知默认展开并提供中文标题，headless 用原有 Hook 事件通道交付。
- 自动提交要求任务开始于干净、有 HEAD 的 Git 根目录；只处理主 Agent 成功 WriteFile/EditFile 产生且检查通过的文件。已有改动、暂存、外部编辑、外部 HEAD 变更、冲突、重命名、快照超限或验收失败时跳过。
- Git 使用明确 argv 和 literal pathspec，保留 Git hooks/签名/身份设置，不自动 push。拒绝或取消时仅在 HEAD/暂存内容仍匹配时恢复本次暂存，不撤销工作区内容。
- 平衡正常结束、Provider 失败、部分响应中断、取消及提前退出时的收尾；完成验证重试不再重复触发 session_end。
- 通知按 Agent 身份领取，避免取消子 Agent 时误取主进程 shutdown 通知。失败/取消通知通过 TUI 和 headless 的收尾适配器及时交付。
- 更新配置示例、README、安装包检查及 Windows CI，保证新增动作随 wheel 发布。

## 验证证据

| 检查 | 最终结果 |
| --- | --- |
| Windows Python 3.12.7 全量 pytest | **975 passed，8 skipped**，114.21 秒 |
| WSL Ubuntu-24.04 / Python 3.12.3 全量 pytest | **983 passed**，69.56 秒 |
| SubAgent 独立验证脚本 | **90/90 通过** |
| wheel 构建及独立环境安装检查 | **通过**；包资源、CLI、13 条默认规则、真实验证报告、错误代码识别 |

Windows 跳过项受平台/权限条件限制；同套全量用例在 WSL 无跳过。新增 `tests/test_lifecycle_actions.py` 含 27 个参数化后的验收用例，使用临时真实 Git 仓库、真实文件操作及 Textual Pilot；外部模型通过确定性测试 Provider 替代，未调用付费模型或向外部服务发送通知。新增测试不是仅检查枚举数量。

实际覆盖：普通/特殊字符文件名提交、首次成功提交及结束时补交、避免重复提交、已有改动/暂存保护、外部改写保护、禁用提交、语法与配置检查失败、Git pre-commit 拒绝及暂存恢复、提交中取消、轮末验证中取消、Provider 部分输出后中断、日志不含提示词/工具内容、受保护写入拒绝、子 Agent 隔离、共享通知隔离、符号链接产物目录保护、TUI 正常/错误/取消通知。

复现命令（分别使用 Windows/WSL 对应解释器，选择空的临时目录）：

```text
python -m pytest -q --basetemp=<fresh-temp-dir> --tb=short
python tests/verify_subagent.py
uv build --wheel --no-sources
<wheel-env-python> scripts/check_distribution.py
```

本机原始日志保留在 `.eviforge/qa-hooks-20261009/`：`release-windows.log`、`release-wsl.log`、`verify-subagent.log`、`wheel.log`。它们是忽略的本地验收产物，不包含在源码提交中。安装包检查中的一次语法错误是预期的负向验证，最终检查退出码为 0。

## 使用条件与边界

通知默认是本地终端通知及 JSONL 记录；外部 HTTP 通知需显式配置。自动提交为有条件的本地检查点，`hook_policy.auto_commit: false` 可独立关闭。没有项目测试配置时，静态检查不冒充功能测试，证据仍标记未验证项。

安全检查覆盖路径保护、基线与代码校验，不是完整漏洞扫描或操作系统沙箱。Bash/MCP/子 Agent 等未归属变更不自动提交。此前工具阶段生成的检查点不会因后续任务失败自动回滚。与外部进程并发修改同一 Git 仓库不保证事务隔离。

沿用原项目语义：session 是一次 Agent.run，turn 是一轮 ReAct 迭代。枚举中的 error、compact、permission_request、file_change、command_execute 独立分发仍未实现；本次没有把它们当作验收目标或宣称新增。完整配置和限制见 [Hook 使用说明](hooks.md)。

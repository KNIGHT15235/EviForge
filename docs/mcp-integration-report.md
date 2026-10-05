# EviForge 0.2.0 MCP 集成实施与验收报告

日期：2026-10-05。实施基线为 EviForge `0.1.0` / `9c7ab54`，它已经包含从 LikeCC 扩展的 Plan、经验治理、Typed DAG、自动化与恢复能力。本次增加的是五项外部服务接入和共用 MCP 运行、审批、证据与恢复适配；不能将已有功能重复计算为本次新增。

操作者已明确调整范围：暂缓消息和 Wiki 验收，其余能力通过后发布。该限制随代码公开，不以本地策略单测代替真实业务验收。

## 1. 外部服务结果

| 服务 | 本轮真实验收 | 结果与边界 |
| --- | --- | --- |
| GitHub | 读取 README、搜索 Issue、创建并回读 Issue、创建并回读草稿 PR | 6 项通过；在明确授权的废用仓库进行。测试 Issue/PR 已关闭，测试分支保留审计；不开放合并、删除或推送代码工具 |
| Context7 | Pydantic 库 ID 解析与 `model_validate` 文档检索 | 2 项通过；解析到 `/pydantic/pydantic`，返回 5,018 字符文档；匿名访问实测可用 |
| Playwright | 导航/快照/填写/点击、真实 PNG 归档、跨 origin 图片与 fetch/302 拦截 | 3 组通过；独立 Edge 上下文，未联系未授权 origin。独立验收截图 16,591 字节 |
| 飞书 | 指定文档读取、Base 字段查询、记录创建/更新/搜索、任务创建/完成、Markdown 报告导入及文档回读 | 8 种工具的所选调用通过；导入任务 `job_status=0` 且回读命中标记。最终复核复用已有记录/任务/文档回执，避免重复创建 |
| Serena | 符号概览、定义/函数体、引用、模式检索、文件读取 | 5 项通过；仅绑定隔离 Python 项目，远端代码写入、shell、记忆写入和项目切换不开放 |

飞书消息没有成功发送回执。官方 0.5.1 的发送/历史工具仅支持 tenant 身份，真实发送接口拒绝用户令牌；操作者因此选择暂缓。实验性的身份兼容入口已移除，公开配置不包含消息工具。Wiki 未做真实验收。普通电子表格 Sheets 也未验收，不能用 Base 成功代替它；全局文档搜索、已有文档编辑、附件管理没有本轮通过证据。

## 2. 共用实现及相对基线的改进

| 基线问题 | 0.2.0 行为 |
| --- | --- |
| 通用 MCP 配置不足以表达服务边界 | 添加启停、必需服务、cwd、连接/调用时限、工具名单、服务类型与资源策略；公开五服务模板默认关闭、外部写入默认关闭 |
| 简化 Schema 转换会丢失嵌套约束与可空字段 | 使用完整远端 JSON Schema 校验，保留缺省/显式 null、嵌套结构、组合约束；禁止外部 Schema 引用与非法数值 |
| 远端名字与 Provider 命名限制冲突 | 生成长度受控的稳定本地名字；含点工具追加散列避免冲突，传给服务器仍用原名 |
| 工具发现、失败和关闭状态不完整 | 分页发现，重复 cursor 拒绝；单服失败隔离，失效注册清理；必需服务失败阻止依赖运行；TUI `/mcp` 正确列出实际工具 |
| MCP 动作不在可信 Plan adapter 内 | 将精确参数、cwd、仓库/文档/表格等资源和身份/配置/Schema 指纹加入 Plan；批准及执行均重查当前能力。环境变量 endpoint/command/cwd 等变化也会使旧授权失效 |
| 仅按远端说明判断权限不可靠 | 本地维护读写与资源适配。GitHub 仅指定仓库、草稿 PR；飞书强制用户身份与指定 token/table/task；Serena 仅当前项目；子 Agent/Team 默认只继承托管读取能力 |
| 图片仅显示占位符，缺少真实证据 | 保存返回的图片/二进制字节，记录 MIME、字节数、SHA-256、call ID；文本输出和单个 artifact 有上限，结构化结果脱敏保留 |
| 远端写入超时后容易重复执行 | 开始/结束 JSONL 回执；不确定写入标为 `ambiguous`，归档失败标为 `completed_unarchived`，停止 Agent，并阻止同服务后续写入，要求人工核查后 append-only reconcile |
| 飞书 `isError=false` 容易被当成成功 | 检查业务 `code`；报告导入还检查实际 `job_status=0`，失败不能记作通过 |
| 浏览器 origin 参数不能约束重定向等请求 | 在可信官方子进程的隔离上下文安装请求 guard，拒绝外部 origin、所有重定向、WebSocket、Service Worker 和绕过隔离的参数 |

最大化复用既有 Runtime、Agent、Registry、ToolSearch、PlanService 和权限执行链。TUI、headless 与 provider-free MCP CLI 使用相同 Client/Manager/Wrapper；没有为每个服务重写 ReAct。Typed DAG 继续沿用角色工具边界，不直接继承任意 MCP。

## 3. 验证层次与事实

1. 基线 Linux 离线测试：893 项通过，作为改动前参照。
2. 最终 Windows 全量：**919 项通过、5 项平台跳过，89.93 秒**。跳过条件为符号链接权限/系统文件条件、大小写敏感文件系统和 POSIX 进程组；这些平台边界继续由 Linux CI 检查。没有为通过测试而删除测试或扩大通用跳过范围。
3. 最近 MCP/Plan/原运行时集中回归：179 项通过、4 项跳过；新增 endpoint 漂移后的 MCP/Plan 集中回归：61 项通过、2 项跳过。
4. SubAgent 离线组件：90/90 通过，包含加载、过滤、Fork、追踪、后台取消/回收和通知；故障用例中的 `boom` 是预期注入。
5. 五服务验收通过生产 MCP Client → Manager → Wrapper → Registry 路径，而非直接 REST 调用替代核心验收。GitHub REST 只用于准备测试分支、独立核对与关闭对象。
6. 真实模型联合流程：Qwen `qwen3-max`，DashScope 的 `openai-compat` 协议。通过 Runtime/Agent/ToolSearch 调用 GitHub 与 Context7，生成本地 HTML，再调用 Playwright 填写、点击、确认 `Saved /pydantic/pydantic`、截图并写报告。共 15 次工具调用，API 报告 input 21,309 / output 1,507 tokens；这是该次运行用量，不是性能基准。
7. 联合流程 PNG 9,026 字节，SHA-256：`18c52eee3bcaef3652222d2cd6bbe1d3b5b4efe0be542ff79c8392a8ba3dd4cf`。校验实际归档文件，不只比较文字占位。
8. `0.2.0` wheel/sdist 构建，并在独立环境、临时 HOME、源码目录之外验证资源、内置 Agent/Skill、CLI、RunResult/DAG Schema、经验治理及无 Provider 的 MCP 列表。

远端首轮 Linux CI：924 项通过、SubAgent 90/90、构建和独立 wheel 检查通过。手动 live workflow 的 Context7 与隔离 Chromium 验收通过，GitHub CI 写入未启用。首轮 Windows 测试摘要为 182 项通过、2 项跳过，但 uv 启动入口返回非零状态；Windows CI 改为直接运行锁定环境的 Python，并显式记录/传递 pytest 退出码，最终以新提交 CI 为准。

私人业务内容、具体飞书资源、应用密钥、Token、OAuth store、截图和原始回执仅存于 Git 忽略目录；公开报告包含验收摘要和可复现实验脚本，不包含这些凭据。

## 4. 测试中发现并修复的问题

- Windows fixture 使用 Linux `pwd` 或依赖管理员符号链接权限：替换成跨平台 Python 命令；只对 Windows 无符号链接权限等条件跳过，Linux 检查仍执行。
- 飞书 OAuth 换 Token 报 400：应用凭据预检定位为错误 App Secret；重新精确读取并存储后预检成功。增加授权前预检与安全错误输出，不把异常正文/密钥写入日志。
- 官方 OAuth 人工等待窗口短，以及成功回调与轮询重复关闭服务：保留官方 PKCE/Token 存储，延长人工操作窗口，容忍服务器已经关闭的具体清理情况。
- 飞书记录检索需要 `base:record:retrieve`：根据真实错误补开用户批准的窄权限并重新 OAuth；没有用更广的应用权限掩盖问题。
- Windows sandbox 内系统凭据和 Edge 子进程不可用：在用户已授权的本机用户上下文完成验收，保留浏览器自身沙箱，没有关闭浏览器安全选项。
- Qwen 使用错误的 Responses 协议：改用该端点对应的 Chat Completions / `openai-compat`，真实模型流程通过。
- Playwright 新版 schema 要求 `target` 和截图 `scale`：使用实际发现结果，不硬套旧示例字段。
- 启动 guard 前 cwd 未展开环境变量、能力指纹未展开 endpoint 等字段：统一展开并增加拒绝旧能力调用的回归用例。

## 5. 版本、CI 和发布规则

版本目录见 [mcp-catalog.json](../integrations/mcp-catalog.json)。Python 使用 `uv.lock`；Node 两项服务及传递依赖使用提交的 npm lock；Serena 固定顶层 `1.7.0`，未宣称拥有跨平台的完整传递依赖锁。Serena advertised serverInfo `1.28.1` 与 package `1.7.0` 分别记录。GitHub/Context7 是远程服务，记录的是观察版本，无法通过客户端固定服务器部署。

默认 push/PR CI 包含 Linux 全量测试、SubAgent、构建、独立 wheel 安装检查，以及 Windows MCP/Plan/原运行时回归。手动 live workflow 默认仅访问 Context7 和隔离 Chromium；GitHub 写入须显式勾选并由维护者配置 CI Secret，限定废用测试仓库。飞书个人 OAuth、Serena 与真实模型仍使用本机授权验收。默认 CI 绿灯不能替代它们的真实验收。

发布采用非 force 的 main 推送；推送前检查远端未漂移，推送后核对本地/远端 SHA 与该提交的 CI。最终对用户的发布回执给出实际 commit 和 CI 链接；本报告不使用自引用 commit 值，也不将未执行的手动 live workflow 标为成功。没有额外发布 PyPI 包、标签或 GitHub Release。

代码回退使用 revert/修复提交。Git 回退不会撤销飞书任务、文档、表格或 GitHub 业务对象；这些对象按实际回执分别处理。不确定写入不得直接重放。

## 6. 尚存限制

- 消息与 Wiki 按操作者决定暂缓；飞书机器人身份尚未验收，用户 OAuth 不等于机器人的消息权限。
- 普通 Sheets、全局飞书搜索、编辑已有文档和附件管理未宣称通过；公开模板只开放已经实测的工具。
- 浏览器防护是请求边界，不是 OS 或 MCP 子进程沙箱；受信的官方 MCP 包仍是本机程序。为边界清晰，首版拒绝所有重定向。
- Serena 只做语义读取；其日志或语言服务器缓存属于工具运行产物，不能据此声称远端进程不会写文件。
- 单服务独立验收覆盖五项；真实模型联合流程覆盖 GitHub、Context7、Playwright 和原生文件工具，没有把“五服务都经同一模型完整调用”作为已达事实。
- 本轮不复现简历中的数值指标，不据单次验收宣称性能、攻击覆盖或生产可用率。

详细接入步骤见 [使用指南](mcp-usage.md)，面试解释见 [飞书与 GitHub 接入讲解](mcp-interview-guide.md)。

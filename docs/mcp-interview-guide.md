# EviForge：飞书与 GitHub MCP 接入讲解及口述稿

本文以 `0.2.0` 实现和实际验收为依据。MCP 是 Agent 与外部工具服务的协议；LLM 决定调用什么工具，EviForge 负责校验与执行，官方 MCP Server 再访问业务 API。接入 MCP 并不意味着自己实现了飞书/GitHub 全部 API。

## 问题一：EviForge 怎么加飞书 MCP？

### 详细过程

**第一步，复用既有框架，选官方服务与传输方式。**

EviForge 已经有 MCP Client、Manager、Tool Wrapper、工具 Registry 和 ToolSearch，我扩展这些公共模块。飞书选择官方 `@larksuiteoapi/lark-mcp 0.5.1`，以 stdio 子进程运行。`integrations/node/package-lock.json` 固定 Node 包及依赖；配置指定 Node、CLI、工具名单和超时。App ID/Secret 从忽略的本机凭据注入子进程环境，避免出现在命令行。启动参数采用 `mcp --oauth --token-mode user_access_token --tool-name-case dot`。

**第二步，解决身份和 OAuth。**

用户个人文档与自建应用机器人的权限并不相同，因此文档/Base/任务使用用户身份。开放平台登记 localhost callback，开启所需文档、Base、任务 scope，再由用户确认官方授权页。登录助手复用官方 OAuth/PKCE 和加密存储，不自行设计 Token 格式。Windows 上 Token 存储和 MCP 子进程要处于同一用户的系统凭据环境。应用密钥和业务 Token 不进入仓库。

这一步遇到过 Token exchange 400。先调用应用凭据预检，定位为保存的 App Secret 不准确，精确提取后预检成功。另对官方较短的人工等待窗口与重复关闭回调服务做了外围兼容，日志只输出安全错误类别。排错时区分应用凭据、用户同意、业务 scope、资源访问权这四层，不能看到“已登录”就宣布功能可用。

**第三步，把远端工具转换成受控的 Agent 能力。**

`MCPManager` initialize/list_tools 获得真实 schema，`MCPToolWrapper` 用完整 JSON Schema 校验，再注册到本地 Registry。飞书工具原名包含点，Provider 名称会规范化并追加散列，调用服务器时仍用原名。ToolSearch 延迟加载所需 schema，避免首次把所有服务工具灌入上下文。

本地策略不信任远端“只读”描述。每个调用要求 `useUAT=true`，document token、app_token/table_id、task GUID 各自必须匹配白名单。报告导入和任务创建没有既有对象 ID，所以使用独立的 create capability，默认关闭。创建后获得新 token/GUID，要更新范围并重新发现；旧批准不能自动覆盖新资源。全局搜索也默认关闭。

**第四步，接入 EviForge 的可验证和恢复链。**

Plan adapter 绑定精确工具/参数、cwd、资源，以及配置、认证存储和 schema 的指纹。批准或执行时内容变了就拒绝。每次 MCP 调用写开始和结束 JSONL；文本和结构化结果脱敏，图片保存真实字节及 SHA-256。除了 MCP `isError`，还检查飞书业务 `code`；导入报告检查实际 `job_status=0`。外部写入超时或结果归档失败会停止 Agent，保留不确定状态，操作者核查后再 reconcile，不自动重放。

**第五步，用真实资源做验收，并承认边界。**

在用户指定的文档与 Base 中读取字段、创建并更新测试记录、通过 record search 回读 ID；创建个人测试任务并标为完成；导入 Markdown 报告后按 token 读取，确认标记。记录检索实际要求 `base:record:retrieve`，补开用户批准的这项权限后通过，不能用 read scope 代替。

消息尝试被真实接口拒绝：官方工具仅支持 tenant，而我们采用的是用户身份。用户选择暂缓消息和 Wiki，公开模板也不提供消息工具。不能回答“飞书所有能力已完成”，也不能把 Base 当作普通 Sheets。已验收的是八种工具的文档、Base、任务和报告链。

### 约三分钟口述版

我给 EviForge 加飞书 MCP，主要做了三部分：官方服务接入、权限与恢复适配、真实业务验收。

首先我复用了项目已有的 MCP Client、Manager、工具注册和 ToolSearch，没有为飞书重写 Agent。服务器选择官方 lark-mcp，并固定了包版本，通过 stdio 子进程运行。EviForge 从服务器发现真实工具和 JSON Schema，做完整参数校验后注册成本地工具，模型再通过 ToolSearch 加载需要的能力。

身份上，我的场景是操作用户自己的文档、表格和任务，所以采用用户 OAuth。开放平台配置本地 callback，用户确认 scope，Token 沿用官方加密存储；应用密钥只在本机忽略目录和子进程环境里。每次调用必须显式 useUAT=true，避免导入报告时切换成应用身份。

接下来不是把所有工具开放给模型，而是增加本地资源规则：文档 token、Base 和 table ID、任务 GUID 都要匹配白名单，创建报告和任务单独开关。Plan 授权还绑定精确参数、资源以及配置、身份和 Schema 指纹，范围或凭据改变后，旧批准不能继续用。

我也把外部操作接入了证据与恢复机制。每次调用都有开始和结束日志；不仅看 MCP 的 isError，还看飞书业务码和导入任务状态。写入如果超时，我不会让 Agent 猜测失败后再发一次，而是标成不确定、停止执行，先核对远端状态，再由操作者确认是否执行过。

最后在真实资源上完成了文档读取、Base 记录创建更新及搜索回读、任务创建完成，以及报告导入后读取验证。过程中发现记录检索需要 retrieve 权限，read 权限并不够；补开窄权限后通过。消息工具则要求机器人身份，当前用户令牌被拒绝，所以消息和 Wiki 按用户决定暂缓，并写进报告。这项工作的重点，是让外部能力可以受控地进入 Agent，而不是只在配置里加一个服务器地址。

## 问题二：EviForge 怎么加 GitHub MCP？

### 详细过程

**第一步，使用官方远程端点，不自己包装一套 GitHub API 工具。**

配置 `https://api.githubcopilot.com/mcp/`，传输为 Streamable HTTP，认证是由本机环境解析的 Bearer Token。Client 控制连接与单次调用时限、禁止 HTTP 自动重定向、统一持有并关闭会话。Token 自身限制与 EviForge 本地仓库规则是两层边界；不能把模型对权限的理解当授权。

**第二步，动态发现和注册。**

initialize 获得 serverInfo/protocol，list_tools 支持分页并拒绝重复 cursor。远程发现 39 个工具，本地首版允许 14 个，其余被过滤；真实验收是其中 6 项调用，不能将 39 个都宣称实测。工具保留远端 schema，模型参数经完整校验再发送。ToolSearch 按需加载，本地稳定名字避免与其它服务冲突。远程部署无法被客户端锁定，因此保存观察版本，并用 schema/config/credential 指纹检测漂移。

**第三步，建立仓库与动作边界。**

读取必须匹配 `policy.repositories`。搜索需要一个明确 `repo:owner/name` 条件，并拒绝 OR、负向仓库条件等扩大范围的表达式。写入默认关闭；首版只允许 Issue create 与 `draft=true` 的 PR，禁用 merge、删除、组织管理及代码推送工具。Issue/PR 的多 method 接口还需逐项检查，不能因为工具名字叫 read 就不检查参数。

**第四步，与 Plan 和恢复协同。**

Plan 使用实际 wrapper 导出 intent，绑定工具、精确 JSON arguments、cwd、仓库资源和能力指纹，以及既有 session/turn/有效期/次数限制。计划批准和执行走相同当前 Registry；改标题、仓库、认证或 schema 都不能沿用旧批准。子 Agent/Team 默认只继承托管读取能力。Tool Wrapper 留存 call ID、参数散列、资源及结束状态，写入不确定时阻止重放；最终判断要依赖 Issue/PR 的 ID 和回读结果。

**第五步，真实验收与发布分开。**

在用户明确允许修改的废用仓库读取 README、搜索 Issue、创建并回读测试 Issue、创建并回读草稿 PR。REST 用于准备 branch/content、独立核对和关闭测试对象；核心六项由生产 MCP 路径执行。测试 Issue 和 PR 已关闭，分支保留审计。

EviForge 的代码发布仍用正常 Git 非 force push，而不是向模型开放 MCP push/merge。推送前核对 main 是否漂移；推送后比较本地/远端提交，等待该 SHA 的 CI。默认 CI 无个人凭据，测试 Linux/Windows、SubAgent 和 wheel；真实 GitHub 写入另设手动显式开关与 CI Secret，不在普通 PR 中自动写业务对象。

### 约三分钟口述版

我给 EviForge 加 GitHub MCP，采用的是 GitHub 官方远程服务，通过 Streamable HTTP 连接，Token 从本机环境注入。项目原来的 MCP Client 和 Manager 负责初始化、分页发现工具、注册和关闭连接，LLM 再通过 ToolSearch 按需加载 Schema，所以不需要自己写一套 GitHub REST 工具给模型。

但完成连接只解决了第一步。我把它当作会产生真实外部副作用的能力来设计。本轮远端发现了三十九个工具，本地只开放十四个。读取必须落在配置的仓库白名单，搜索也必须带明确的 repo 条件。写入默认关闭，首版只支持创建 Issue 和草稿 PR；合并、删除和直接推送代码都没有交给模型。

这些规则接进了 EviForge 的 Plan。计划会绑定工具名、精确参数、工作目录、仓库范围，以及认证、配置和 Schema 指纹。用户批准一个仓库中的具体动作，模型不能换个仓库或改参数继续执行。远程服务 Schema 发生变化，也需要重新发现和审批。

恢复方面，每次调用都有 call ID 和开始、结束记录。创建 Issue 或 PR 后，要获取对象 ID，再回读确认；如果网络超时，我不会直接重复创建，因为服务端可能已经成功，只是回执没回来。此时 Agent 停止，操作标成不确定，先人工查远端状态，确认后才能解除后续写入限制。

我在用户授权的废用仓库做了真实测试，包括读取 README、搜索 Issue、创建并回读 Issue、创建并回读草稿 PR。核心调用走生产 MCP；REST 只做测试分支准备、独立核对和清理，成功后把测试 Issue 和 PR 关闭。随后又用真实 Qwen 模型，通过 ToolSearch 调 GitHub 和 Context7，写页面、用 Playwright 验证并归档截图，证明它能进入完整 Agent 流程。

代码发布则使用正常 Git push，发布前核对 main，发布后核对远端 SHA 和对应 CI。这样我能区分“服务已连接”“业务动作已验收”和“代码已发布”，每个结论都有自己的证据。

## 进一步追问时的简短回答

- **为什么不是仅添加配置？** 配置只负责找到服务器；完整 schema、可信资源策略、Plan、业务码、证据和不确定写入恢复都需要客户端代码适配。
- **MCP 与 Function Calling 有什么关系？** Provider 的 Function Calling 是模型提出工具调用的接口；MCP 负责工具发现和执行。Wrapper 将 MCP 工具转换为 Provider 可用 Schema，并把两条链连接起来。
- **为什么不自动重试写入？** 超时不等于未执行，直接重发可能重复创建业务对象。读取可重新发现后再调用；写入要先核对远端副作用。
- **有何限制？** 消息/Wiki 暂缓；飞书普通 Sheets 未验收；Serena 只读；浏览器 origin guard 不是 OS 沙箱；远程服务版本只能观察，不能客户端锁定。
- **是否达到简历指标？** 本轮验证能力与边界，不按单次调用或测试数量推断简历中的攻击覆盖、性能和生产成功率。

## 代码阅读入口和官方资料

配置/策略：`eviforge/config.py`、`validator.py`、`mcp/policy.py`。协议/注册：`mcp/client.py`、`manager.py`。执行/证据：`tool_wrapper.py`、`recovery.py`。Plan：`permissions/capabilities.py`、`planning/session.py`。真实验收：`scripts/verify_feishu_mcp.py`、`verify_github_mcp.py`、`verify_agent_mcp.py`。

- [飞书官方 MCP](https://github.com/larksuite/lark-openapi-mcp/blob/main/README_ZH.md)
- [GitHub 官方 MCP](https://github.com/github/github-mcp-server)
- [MCP 工具协议](https://modelcontextprotocol.io/specification/2025-06-18/server/tools)

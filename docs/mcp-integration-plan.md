# EviForge MCP 集成、验证与发布改进方案

编写日期：2026-10-05

项目目录：`D:\ldj\03\Eviforge`

目标仓库：<https://github.com/KNIGHT15235/EviForge>

审查基线：`main` 分支，提交 `9c7ab54`，项目版本 `0.1.0`

建议发布版本：`0.2.0`，实施前结合仓库届时状态确定

文档状态：**原始实施方案。2026-10-05 已按本方案实施；实际能力、测试与限制见 [实施报告](mcp-integration-report.md)。本方案中的计划值不作为完成证据。**

范围调整：操作者明确选择“暂缓消息和 Wiki 验收，完成其余能力后发布并如实标注限制”。因此这两项不再作为本次发布门禁；普通 Sheets 与未授权的全局搜索也不能据多维表格验收宣称通过。以下保留最初规划，实际交付以实施报告为准。

## 1. 目标与范围

在现有 EviForge 上接入 GitHub、Context7、Playwright、飞书和 Serena 五项 MCP 服务，形成从需求获取、代码理解、编码、验证到结果交付的可执行工作流。最大化复用现有 ReAct、共享 Runtime、工具注册、ToolSearch、权限审批、PlanSession、会话、事件日志、证据归档和恢复机制。

“集成完成”必须同时包括：可复现的配置与依赖、服务连接、工具发现、合法调用、错误处理、权限控制、运行记录、真实服务验证、原功能回归、分发包检查，以及验收后发布到目标 GitHub 仓库。只添加配置示例或通过本地替身测试不构成完整验收。

### 1.1 本次必须交付的服务能力

| 服务 | 必须集成并验收的能力 | 本次启用边界 |
| --- | --- | --- |
| GitHub | 查询目标仓库、Issue、PR；在测试仓库创建 Issue 和草稿 PR 并回读确认 | 指定仓库；不默认启用合并、删除、组织管理或代码推送工具 |
| Context7 | 库标识解析、文档查询；将检索结果用于编码任务 | 文档检索，不向查询发送凭据或无关私有源码 |
| Playwright | 页面导航、结构快照、点击、填写表单、截图；实际验证页面行为 | 独立浏览器上下文与受控测试站点；不默认开放任意脚本执行、个人浏览器会话或无限制文件访问 |
| 飞书 | 搜索和读取文档/知识库；导入报告；查询和更新多维表格；创建/更新任务；指定测试会话中的消息读写 | 预先指定的应用、用户身份、文档、表格、任务及会话范围 |
| Serena | 在指定项目中定位符号、获取引用、读取相关代码；将结果用于修改与测试 | 首版以语义检索为主，代码写入继续走 EviForge 原生文件工具 |

Serena 包含在本次总体范围，安排在前四项之后接入。五项所选能力全部验收通过，才可称本方案完成。服务的所有 OpenAPI 或全部远端工具不在本次范围；最终报告应准确列出启用、禁用和未验证项。

Serena 的远端代码写入、记忆写入、任意命令，以及 Playwright 任意 JavaScript 执行、文件上传等高自由度工具，留待专门的副作用跟踪适配后扩展。首版不能在 README 中宣称已支持这些禁用能力。

### 1.2 示例完整流程

1. 从指定飞书文档读取需求并保存来源、获取时间和内容摘要。
2. 从指定 GitHub Issue 获取补充条件；通过 Context7 查询使用中的库版本。
3. 通过 Serena 获取符号和引用信息，在 EviForge 的 Plan 流程中形成修改方案。
4. 使用现有 ReadFile / EditFile / WriteFile / Bash 修改代码并执行测试；需要审批的操作按现有边界执行。
5. 使用 Playwright 操作受控页面，核验交互结果并归档快照和截图。
6. 将 Git diff、测试输出、浏览器证据和来源引用汇入本地验收报告。
7. 获得相应写操作授权后，创建 GitHub 草稿 PR、向指定飞书位置导入报告、更新表格/任务；发送通知须有明确用户指令。

## 2. 当前代码事实与差距

下表依据本地源码静态审查。历史验证文档中的测试结果为已有记录，本轮没有重新执行，实施时必须先建立新的基线。

| 模块 | 已有能力 | 本次需要补充或修复 |
| --- | --- | --- |
| `eviforge/config.py`、`validator.py` | `mcp_servers`，command/args、url/headers、env，分层配置合并 | 启停、连接/调用时限、配置类型校验、重复名检查、服务范围、环境变量缺失处理、配置快照 |
| `eviforge/mcp/client.py` | stdio、Streamable HTTP、初始化、列工具、调用、连接持有任务和关闭 | 显式工作目录、连接/调用超时、分页、状态更新、受控重连、认证失败分类、诊断脱敏 |
| `eviforge/mcp/manager.py` | 动态注册工具、按服务器收集错误、关闭连接 | 单服失败隔离、注册失败清理、原子替换、公共服务状态、发现分页、避免失效 wrapper 持有旧连接 |
| `eviforge/mcp/tool_wrapper.py` | 远端工具转成本地 Tool，延迟加载，返回 isError | 完整 JSON Schema 校验、无损参数传递、工具身份、结构化结果、图片证据、错误和不确定结果 |
| `eviforge/tools/impl/tool_search.py`、工具 Registry | 关键词搜索、按名称加载、按 Provider 导出 Schema | 正确展示可用工具；多服务名称、Schema 和配置漂移；搜索阶段不绕过禁用策略 |
| `eviforge/commands/handlers/mcp.py`、`app.py` | `/mcp` 状态展示及服务说明注入 | 实际名字为 `mcp_<server>_<tool>`，展示筛选却使用 `mcp__<server>__<tool>`，会漏列工具 |
| `eviforge/runtime.py`、`__main__.py` | TUI / headless 共用 MCP 装配和生命周期 | 两种入口共用新增配置、状态、超时、权限及审计；必需服务失败影响运行结果 |
| `permissions/capabilities.py`、`planning/session.py` | 精确参数、cwd、路径、网络 origin、时限、次数和 Agent audience 的 Plan 授权 | 当前只信任已知内置工具；不能通过改变 MCP category 来获得 Plan 兼容，须新增本地可信适配 |
| `eviforge/agents/tool_filter.py` | 子 Agent/Team 能继承 MCP 并执行定义中的允许/禁用列表 | 现有后台/队友路径会把 MCP 加回工具集；需加入服务范围、身份、只读/写入授权的交集 |
| `eviforge/dag/agent_runner.py` | Typed DAG 仅暴露角色允许的工具，独立证据和检查点 | 首版继续让 MCP 在 DAG 外获取上下文、保存交付；DAG 节点不直接继承任意 MCP |
| `ToolResult`、会话与自动化输出 | 当前工具返回主要为文本和 is_error | 新增可选结构化数据、证据引用、执行状态；图片不能只剩 `[image: ...]` |
| 配置示例和测试 | 默认 `mcp_servers: []`；有本地 stdio、重连和关闭测试 | 五项服务的配置/版本记录、HTTP 实测、真实服务验证及发布门禁 |

另有两个需要在集成时验证的兼容问题：

- 当前参数模型只转换顶层基本类型；嵌套约束、enum、组合类型等不能由该实现完整表达，默认额外字段处理还可能丢失合法参数。
- 当前结果提取未保留 `structuredContent`，图片只返回占位文本；不能据此宣称已获得完整浏览器证据。

## 3. 设计原则与验收边界

1. **复用 MCP 协议层。** 五个服务共用 Client / Manager / Wrapper；服务差异集中到配置、工具能力规则和结果归档，不复制五套 Agent 循环。
2. **有明确版本。** 本地服务器、Node/Python 运行时和浏览器依赖记录固定版本；正式配置不能使用未锁定的 `@latest` 或 Git 分支。远程服务记录访问时间、返回的 serverInfo 和工具 Schema 快照。
3. **可配置启用。** 未启用的服务不连接；缺少凭据时说明具体状态。公开仓库只保存模板，应用密钥、Token、个人会话和私有业务内容留在本地配置或凭据存储中。
4. **权限由本地实现决定。** 远端工具名称、description、annotations 只能作为提示，不能决定“只读可信”或自动批准。服务提供的内容作为任务资料处理，不可改变授权规则。
5. **保持 EviForge 的可恢复边界。** 已完成的远端写入不随会话恢复自动重放；网络超时或取消不等同于写入失败，更不等同于远端回滚。
6. **按证据说明能力。** 真实服务测试、协议替身测试和真实模型工作流分开统计；没有凭据、被网络阻断或跳过的测试不得标为通过。

## 4. 共用接入层改进

### 4.1 配置、版本与启动

保持原有 `mcp_servers` 配置兼容，在现有 dataclass、校验器和配置合并逻辑中增量实现以下字段。字段名称为方案建议，实施时可以调整，但对应行为必须实现并记录。

| 字段 | 建议行为 |
| --- | --- |
| `enabled` | 新配置支持显式启停；旧条目保持现有启用语义；公开五项示例默认关闭 |
| `integration` | `github/context7/playwright/feishu/serena/custom`，用于选择本地已审查的工具能力配置 |
| `required` | 当前任务声明依赖该服务；启动失败或必需工具缺失时不能把任务标成 success |
| `cwd` | stdio 服务启动工作目录，解析为明确绝对路径；Worktree 切换后重新核对 |
| `startup_timeout_seconds` | 包括启动、初始化和工具发现；建议默认 60 秒，Serena 可单独增加 |
| `call_timeout_seconds` | 每次工具调用的总预算；建议默认 60 秒，按具体工具配置上限 |
| `allowed_tools` / `denied_tools` | 以远端原始工具名匹配；deny 优先；未匹配的新增工具不自动放行 |
| `policy` | 所选工具的资源范围、写入条件、最大输出/证据大小等；不能声明任意工具为可信只读 |

实现要求：

- command 与 url 互斥且非空；name 唯一；args 必须是字符串列表，headers/env 必须是字符串映射；时限和大小为正且有上限。
- 同名配置按既有层级覆盖；显式 `enabled: false` 能关闭上层配置。禁用服务清理其工具、发现状态及连接。
- 环境变量占位符在明确支持的字段解析；缺少必需凭据时返回脱敏的 `auth_required` / 配置错误，不能发送字面 `${TOKEN}`。
- 不把 Provider API Key 自动传给所有 MCP 子进程。逐项验证 SDK 的默认环境继承，并仅额外传入服务运行所需变量。
- Windows 测试实际的 `npx.cmd` / 可执行文件路径、SystemRoot、临时目录和含中文/空格目录；不以字符串拼接 shell 命令绕过参数检查。
- 启动允许并发，但并发数有界；单个可选服务失败不阻断其他服务注册。整体启动受总时限约束。
- 新增服务版本清单，例如 `examples/mcp/server-versions.json`，记录包版本、运行时版本、Schema 摘要、校验时间和安装方法。具体版本在实施时确定，不在方案中虚构已验证版本。

配置形态示例（**含待实现字段，不可直接视为当前版本可运行配置**；providers 等原配置仍须保留）：

```yaml
mcp_servers:
  - name: github
    integration: github
    enabled: false
    required: false
    url: https://api.githubcopilot.com/mcp/
    headers:
      Authorization: "Bearer ${EVIFORGE_GITHUB_TOKEN}"
    startup_timeout_seconds: 60
    call_timeout_seconds: 60
```

其余四项提供独立示例文件和总示例；用户只启用需要的服务。模板不得携带真实 Token。

### 4.2 工具身份、发现与状态

- 工具内部身份使用 `(server_name, remote_tool_name)`，保留原始 Schema 和工具映射，避免用字符串前缀推断归属。
- 兼容已有 `mcp_<server>_<tool>` 名称；不为对齐 Claude Code 而强制改成双下划线。
- 对不符合 Provider 工具名限制的名称生成稳定别名；长度、字符和碰撞均测试。遇到无法消除的冲突报错，不覆盖另一工具。
- `/mcp` 和系统说明从 Manager 的公共状态读取工具身份，修复双下划线筛选问题，并显示已注册、已启用、已发现的数量。
- `list_tools` 处理 `nextCursor`，防止只注册第一页；读取服务 instructions 时标为外部资料，不能提升为系统级授权。
- 状态至少区分 disabled、connecting、ready、auth_required、failed、closed。调用结果不确定作为独立执行状态处理。
- 重连重新发现并比较工具/Schema；变化后更新注册及发现状态，废弃旧实例，不能保留失效 wrapper。能力变化使相关未消费授权失效。
- 新增公共 CLI 管理入口，例如 `eviforge mcp list/tools/doctor`。这些属于待实现命令；doctor 默认检查本地依赖和配置，显式连接检查才接触服务，不能通过调用写工具“检测健康”。

### 4.3 参数校验与原样传递

改进共同工具执行入口，允许 MCP wrapper 使用独立参数校验流程；内置工具仍沿用现有 Pydantic 模型。

1. 保留远端原始 `inputSchema`，确定其 JSON Schema dialect。使用明确锁定的 JSON Schema 校验依赖或等价实现；不依赖当前顶层 Python 类型映射充当完整验证。
2. 支持 nested object/array、required、enum、anyOf/oneOf/allOf、nullable、additionalProperties，以及本地 `$ref`。不可联网任意加载远端 `$ref`。
3. 不做静默类型转换、丢弃字段或填入未提供的默认值；遵循 Schema 决定额外字段是否允许。合法 `null` 与缺省字段保持区别。
4. 对 Schema 不支持或非法情况，隔离该工具并显示原因；不降级成完全无校验调用。
5. 将**校验后的原始 JSON 参数**用于审批快照和远端调用，确保“审阅参数 = 执行参数”。同一个规范化算法产生哈希。
6. Provider 对工具 Schema 的限制由协议适配层处理；不能为迎合 Provider 悄悄改变实际远端参数含义。

### 4.4 工具结果、图片与证据

保持 `ToolResult(output, is_error)` 的旧调用兼容，增加有默认值的可选字段，如执行状态、structured data、content 元数据和 artifact references。

- 分别处理 `content`、`structuredContent`、`isError`；保留文本、图片、资源链接及嵌入资源的类型与来源。结果裁剪只裁剪模型上下文展示，不删除原始证据。
- 图片解码后验证 MIME、字节长度和格式，保存于当前 run 的 MCP artifact 目录，计算 SHA-256；文本结果返回路径和引用。大文件受限，不把 base64 塞进 JSONL 或模型文本。
- 首版模型可以依据页面结构快照完成验证；截图仍须作为真实文件保存。若向模型提供图片，必须分别实现并测试 Anthropic / OpenAI 内容块转换，不能假设所有模型具备视觉能力。
- 对服务声称生成的本地文件，只接收已配置 artifact 根目录内且可实际读取的文件；检查路径、符号链接和实际字节。不信任服务自报的哈希。
- 资源 URI 不自动下载、不自动打开本地文件；获取动作另行检查协议、目标和凭据。
- 尽量复用 `eviforge/dag/artifacts.py` 的哈希和不可变保存能力；如果需要抽出共用 ArtifactStore，保留旧 DAG 路径和证据格式的兼容测试。
- 记录证据来源是“本地测试输出”“远端返回”“浏览器截图”等。哈希证明归档内容一致，不能单独证明测试语义正确。

### 4.5 错误、重连、重试与恢复

- 区分配置错误、缺凭据、401/403、限流、启动失败、协议错误、参数错误、工具业务错误、取消和通信中断。
- 连接与只读发现可以按有限次数重试，并尊重 Retry-After 和总时间预算；不能叠加 SDK 与应用层无限重试。
- 只有本地规则明确为只读、无业务写入的调用才可有限重试。GitHub 创建、飞书导入/更新/发送、浏览器交互等不能仅因超时自动重放。
- 请求发出后未收到确定结果，对可能有副作用的调用记为 `ambiguous`；持久化 operation_id、目标、请求摘要和已知远端 ID。
- 恢复优先回读已知对象；无 ID 时可用本次唯一标记在允许范围内查找，但找到多项或查不到均不等于可直接重试。需要明确重试授权，并保留前次状态。
- 日志落盘失败不能变成成功写入凭证。外部写入完成但本地提交失败仍进入人工核对流程。
- 关闭和取消必须在总时限内回收服务持有任务、stdio 进程及会话级浏览器资源；不关闭用户独立启动、非本运行拥有的服务器。

## 5. 权限、Plan、Agent 与经验治理

### 5.1 本地服务能力规则

建议新增 `eviforge/mcp/policies.py` 与 `integrations.py`，只保存以下内容：服务选择、已审查的工具名/Schema 指纹、能力分类、目标资源提取和调用前检查。真实远端工具仍由同一 wrapper 执行。

| 服务 | 授权时必须显示并检查的目标 |
| --- | --- |
| GitHub | owner/repo、Issue/PR 编号、分支、操作类型；Token 本身的仓库范围也应收窄 |
| Context7 | 库 ID、版本或查询、外发查询内容 |
| Playwright | 浏览器会话、当前页面 origin、目标 URL/元素、表单内容摘要、截图位置 |
| 飞书 | 调用身份、doc/wiki token、app/table/record ID、task ID、receive_id/type、操作类型 |
| Serena | 激活项目的真实路径、查询范围；首版禁用远端代码和记忆写入工具 |

对于浏览器点击、飞书复杂 API 和其他无法完整推导实际内部行为的操作，审批说明应明确“批准具体调用”，不得宣称客户端可约束服务内部的全部文件和网络副作用。需要更强隔离时用受控服务配置、专用账号或容器落实并单独验证。

### 5.2 接入现有 PlanSession

当前 Plan 只信任内置实现。新增 MCP 支持必须同时修改 `permissions/capabilities.py` 和 `planning/session.py`，不能只把 wrapper 的 category 改成 read。

- 注册本地可信的 MCP capability adapter，通过实际 wrapper 类型、服务身份、工具 Schema、策略版本和配置摘要确认身份。
- 扩展版本化 action manifest，使资源范围可表达 repository/document/table/chat/browser/project 等标识；旧 Plan 文件继续兼容加载。
- action 同时绑定 session、turn、内容哈希、Agent audience、规范化完整参数、cwd、服务配置/Schema 摘要、时限和使用次数。
- `normalize_action` 必须获得可信的工具快照/解析上下文，不能为了处理 MCP 名称而实例化任意类或接受模型自报实现。
- 未批准的规划阶段，只能使用已审查的读操作。批准后允许具体列出的写调用，执行前再次核对目标和参数，消费次数；明确 deny 永远优先。
- 工具重连后的身份/Schema 漂移、切换项目/Worktree、过期、参数修改、禁用或替换工具均拒绝旧授权。
- 未建立可信适配的 custom MCP 维持现有保守审批行为，不能因远端 annotations 自报 readOnly 而在 Plan 中自动放行。
- headless 复用两阶段批准流程；遇到待授权或不确定调用，使用既有 blocked / ambiguous 与退出码约定。

### 5.3 SubAgent、Team 和 Typed DAG

- 普通子 Agent / Team 的有效权限取父策略、服务工具白名单、Agent 定义及本次授权的交集。
- 修复“将所有 MCP 加回后台工具列表”的宽泛继承；后台默认仅继承已审查的读能力，不继承父 Agent 的一次性写授权。
- TUI、headless、后台子 Agent、队友必须通过同一参数、资源和执行结果检查；直接调用工具和 Hook 不能形成绕过路径。
- 本次保持 DAG 节点原有工具边界。外部资料先由普通 Agent 获取并归档，再以显式输入/文件引用交给 DAG；DAG 输出验收后再由普通 Agent 交付到外部服务。
- 不把任意 MCP wrapper 加入 `ROLE_TOOLS`。后续若需要 DAG 内 MCP，应另行实现图契约、资源冲突、能力指纹和恢复规则。

### 5.4 资料和经验的信任等级

飞书需求、GitHub Issue、Context7 文档、网页和 Serena 服务说明均作为外部资料。检索内容不能覆盖用户指令、系统提示或授权状态。外部资料形成的经验仅进入现有 quarantine 候选；继续经过验证、人工确认、发布及撤销流程，不自动成为长期 Memory 或 Skill。

## 6. 五项服务实施细节

### 6.1 GitHub

采用 GitHub 官方服务器；默认选择 Streamable HTTP 的 `https://api.githubcopilot.com/mcp/`。首次使用明确配置的 Token，后续需要 OAuth 时单独实现客户端认证与凭据存储，不假设现有 httpx Client 已支持 OAuth。GitHub CLI 已登录不等于 MCP Token 已配置。

- 将仓库检索/Issue/PR 的读操作与创建操作分开配置，按实际发现的工具名记录启用表。
- 测试写操作只使用用户明确指定的测试仓库；不拿发布仓库中的真实业务 Issue 或 main 作为试写目标。
- 创建 Issue / 草稿 PR 时加入唯一测试标记，记录远端 ID，回读标题、正文、分支和状态；清理测试对象需有对应授权。
- 代码文件的修改、commit 和最终推送继续采用本地 Git 与既有发布流程；MCP 验收不要求启用远端推送/合并工具。

来源：[GitHub 官方 MCP](https://github.com/github/github-mcp-server)、[服务器配置说明](https://github.com/github/github-mcp-server/blob/main/docs/server-configuration.md)。

### 6.2 Context7

优先使用远程 `https://mcp.context7.com/mcp`，按官方当前文档配置认证。实施时确认已选方案的匿名可用性/额度，正式可复现验收记录实际身份与服务限制。

- 当前官方工具包括 `resolve-library-id` 和 `query-docs`；不要沿用未经核对的旧工具名。
- 输入使用明确库名、已安装版本和具体问题；保存查询、库 ID、获取时间与文档来源。
- 实测至少覆盖已知 Python 库的查询、未知库、无匹配结果和限流处理。无结果应明确返回，不编造文档。
- 模型根据文档完成一个小任务，仍需运行本地测试；不能把服务返回内容视为测试通过证据。

来源：[Context7 官方说明](https://github.com/upstash/context7/blob/master/README.md)。

### 6.3 Playwright

采用 Microsoft 的 `@playwright/mcp`，以固定包版本的 stdio 服务启动。首次安装运行时、包和浏览器应作为明确的准备步骤，避免在每次 Agent 启动中下载最新依赖。

- 使用当前项目的明确 cwd 和独立浏览器上下文；默认 headless，截图输出到本次 run 的专用目录。
- 先验证 snapshot、navigate、click、fill、screenshot 等所选工具；禁用任意 eval/run_code、连接个人浏览器与扩展额外能力。
- 本地测试站点明确绑定 loopback，不共享用户登录态；需要业务站点时使用专用测试账号。
- 目标 URL、页面变化和表单操作均记录；状态不能跨独立 session 混用；Worktree 切换后重建相关绑定。
- 服务的 allowed/blocked origins 仅为辅助过滤。官方说明其不构成安全边界且不覆盖重定向；验收不能把该选项等同于网络沙箱。
- 截图显式文件名可能受 workspace root 而非 output-dir 影响，因此必须检验实际生成位置和归档字节。

来源：[Playwright MCP 官方说明及配置](https://github.com/microsoft/playwright-mcp)。

### 6.4 飞书

采用飞书官方 `@larksuiteoapi/lark-mcp`，优先通过 stdio 启动；国内飞书使用 `https://open.feishu.cn`。应用身份和用户身份分开测试并在状态、调用记录中显示。App ID、App Secret 和 Token 不写入公开文件或命令日志。

- 准备独立测试应用、所需 OpenAPI 权限、OAuth 回调配置，以及用户授权；使用官方服务支持的环境变量或本地安全配置提供凭据。
- 个人文档等用户资源优先走明确的 `user_access_token`；需要登录时先按官方流程完成，不由 EviForge 猜测或自动替换成租户身份。
- 首批工具：文档搜索、`docx.v1.document.rawContent`、`docx.builtin.import`、知识库节点搜索/读取；多维表格记录搜索/创建/更新；任务创建/更新；指定会话消息列表/发送。
- `-t` 只开放所选工具，避免直接开放全部 OpenAPI；EviForge 侧再执行工具和资源范围过滤。
- 当前官方 README 明确：不支持文件上传下载；云文档内容支持读取和导入，暂不支持直接编辑已有内容。本次报告以导入新文档交付，不能承诺原位编辑或截图附件上传。
- 多维表格/任务更新可以写入报告 URL、本地证据摘要及哈希；上传截图附件如后续需要，另行选择并验证专门适配。
- 发送消息只用于用户指定的测试会话和明确通知请求；开启飞书服务本身不构成向他人发消息的授权。
- 报告导入、记录更新和消息发送分别回读确认。导入成功但后续通知失败时记录各步骤状态，恢复不能再次导入一份报告。

来源：[飞书官方 MCP README](https://github.com/larksuite/lark-openapi-mcp/blob/main/README_ZH.md)、[配置指南](https://github.com/larksuite/lark-openapi-mcp/blob/main/docs/usage/configuration/configuration-zh.md)、[预设工具清单](https://github.com/larksuite/lark-openapi-mcp/blob/main/docs/reference/tool-presets/presets-zh.md)。

### 6.5 Serena

使用固定版本 `serena-agent` 与对应运行时，按官方当前 CLI 启动 stdio 服务。EviForge 本体继续支持 Python 3.11；Serena 所需独立 Python 版本按锁定发行版安装，不能误认为二者必须使用同一虚拟环境。

- 启动命令、context、mode 和项目参数以锁定版本的 `--help` 及官方文档核对；选用适合 EviForge 的最小工具集。
- 首版启用符号查找、引用查找和代码阅读等已审查工具，代码编辑继续走 EviForge 原生工具，确保 FileHistory、Plan、实际写入记录和 DAG 能力不被绕过。
- 使用有跨文件引用的 Python fixture 验证定位正确性；再在临时真实仓库完成“语义检索辅助修改 → 原生工具编辑 → 测试通过”。
- 项目路径必须明确并与当前 Worktree 一致；切换 cwd 后不能继续使用旧索引或旧项目的结果。
- Serena 的 onboard、配置、索引和记忆可能产生本地文件：安装/初始化在准备阶段明确执行并记录目录；禁用运行中的记忆写工具，区分业务只读与内部缓存写入。
- 不采用上游自动批准 Hook 或系统提示覆盖来绕开 EviForge 权限。

来源：[Serena 官方仓库](https://github.com/oraios/serena)、[客户端接入文档](https://oraios.github.io/serena/02-usage/030_clients.html)、[工具清单](https://oraios.github.io/serena/01-about/035_tools.html)。

## 7. 实施顺序、改动位置与阶段门禁

以下为后续实施顺序。每阶段完成实际测试后再进入下一阶段；错误直接最小修复并重跑受影响检查，不能用删除测试或放宽授权来取得通过。

| 阶段 | 工作 | 主要位置 | 进入下一阶段的条件 |
| --- | --- | --- | --- |
| P0 基线 | 核对 AGENTS、Git 状态和远端；记录基线 SHA/依赖；全量原功能测试；准备测试资源 | 现有 tests、CI、docs | 有本次基线报告；已有失败有明确处理；不覆盖他人未提交改动 |
| P1 共用协议层 | 配置、身份/状态、前缀修复、分页、Schema、结果/证据、超时/关闭 | config、validator、mcp、tools、agent、runtime | stdio/HTTP 本地实测、参数/结果回归及 TUI/headless 一致性通过 |
| P2 权限与恢复 | 本地能力规则、Plan 适配、子 Agent 交集、写调用 journal 和不确定状态 | permissions、planning、agents、automation、mcp | 拒绝/过期/漂移/恢复测试通过，旧 Plan 和 DAG 回归通过 |
| P3 文档与仓库 | Context7、GitHub；锁定示例和真实读写验收 | integrations、examples/mcp、tests | 查询真实服务成功；GitHub 测试对象写入/回读及失败路径有证据 |
| P4 浏览器 | Playwright；页面 fixture、结构断言与截图归档 | MCP artifacts、tests/integration、scripts | 真实浏览器完成完整页面交互；截图字节、哈希与失败检测正确 |
| P5 协作 | 飞书应用/用户认证、文档/表格/任务/消息 | integrations、examples、live tests | 所选能力在指定真实测试资源上完成读写、回读和拒绝测试 |
| P6 语义检索 | Serena 项目绑定、索引/工具选择、Worktree | integrations、agents、worktree、tests | 真实 Serena+语言服务的跨文件查询和辅助修改任务通过 |
| P7 总验收 | 五服务组合流程、原能力全量回归、包安装、报告 | tests、scripts、docs、CI | 第 10 节发布门禁全部满足 |
| P8 发布 | 提交、推送、远端 CI、main 更新核对 | Git/GitHub、发布记录 | 目标远端 main 指向已验收发布提交，发布 CI 成功且报告可访问 |

建议新增的最小文件集合：

```text
eviforge/mcp/integrations.py       # 五项服务的预设及已审查能力配置
eviforge/mcp/policies.py           # 资源范围、身份、Plan 适配入口
eviforge/mcp/artifacts.py          # 结果证据归档；复用既有 ArtifactStore
eviforge/mcp/cli.py               # 公共状态、工具列表与诊断
examples/mcp/*.yaml               # 五项独立示例和总示例
examples/mcp/server-versions.json # 固定依赖和发现快照信息
tests/test_mcp_*.py               # 共用协议/配置/权限/恢复回归
tests/integration/test_mcp_*.py   # 本地真实服务及有标记的线上测试
scripts/verify_mcp_integrations.py# 验收编排、失败传播和机器报告
docs/mcp-integrations.md          # 用户使用指南
docs/mcp-verification.md          # 实测结果、限制和复现
docs/mcp-improvement-report.md    # 相对 0.1.0 的改进与修复
```

只有当共用抽象确实需要时新增文件，避免仅为每项服务建立空壳模块。所有建议模块和命令当前均为待实现项。

## 8. 验证全流程

### 8.1 环境和前置资源

- Python 3.11 和至少一个较新支持版本；Windows 本机与项目既有 Ubuntu/WSL 环境分别验证。Node、uv、浏览器与 Serena 独立运行时记录版本。
- 创建隔离工作目录/临时仓库、专用测试网页和本次唯一测试标记。原 LikeCC 目录不参与修改。
- 准备 GitHub 测试仓库、Context7 身份、飞书测试应用及资源。凭据缺失时仅完成不依赖它的工作并记录 pending；最终线上门禁仍未通过。
- 真实模型端到端验证使用明确配置的 Provider 和小任务，记录模型/参数/用量。不把 fake 模型调用统计当作真实模型成功率。
- 日常 CI 默认离线；需要外部凭据/写入的验收作为单独触发任务，不向 fork PR 暴露密钥。

### 8.2 第一层：单元与协议契约

| 编号 | 测试内容 | 必须断言 |
| --- | --- | --- |
| C01 | 旧配置、新配置、缺变量、禁用覆盖、重复名、非法 cwd/类型 | 合法配置可用；非法配置给明确脱敏错误 |
| C02 | 工具名含点号、短横线、下划线、超长和碰撞 | Provider 名称合法，内部映射唯一，不覆盖原工具 |
| C03 | 多页 list_tools、部分服务失败、注册失败 | 所有页面被处理；失败服务被清理，其他服务可用 |
| C04 | 嵌套、enum、union、nullable、额外字段、非法 Schema | 参数原样传递；错误输入在发送前被拒绝 |
| C05 | 文本、structuredContent、图片、资源、isError、超大输出 | 类型保留；证据实际归档；错误不会被判成功 |
| C06 | ToolSearch、禁用工具、重新发现、Provider Schema | 只发现获准工具；Anthropic/OpenAI/OpenAI-compatible 均可调用 |
| C07 | `/mcp`、CLI 状态及服务说明 | 显示名称/数量与 Registry 一致，不再发生前缀漏列 |
| C08 | 鉴权失败、调用超时、限流、取消、关闭 | 状态和错误类别正确，时限生效，无无限等待 |
| C09 | 请求内容/日志/事件/异常中的凭据 | Token、Secret、敏感 header 被遮蔽，配置摘要不暴露秘密 |

### 8.3 第二层：真实本地集成

1. 本地 FastMCP stdio 服务和 Streamable HTTP 服务真实启动，经生产 Client 完成 initialize → 多页发现 → ToolSearch → 参数检查 → call_tool → 回读 → close。
2. 通过确定性模型流驱动**真实 Agent**完成调用，而非只直接调用 wrapper；覆盖 TUI 与 headless json/jsonl。
3. 两个及以上服务并发启动/调用、某个服务宕机、重连换 Schema、取消和跨任务关闭；检查进程、任务、连接均回收。
4. 真实 Playwright 在临时测试站点执行填写/提交/断言/截图；故意引入坏页面使验证失败，证明测试能检出错误。
5. 真实 Serena 和所选语言服务查询临时 Python 仓库的符号与引用；同时测试修改后索引和 Worktree 切换。
6. 安装独立 wheel，在源码目录外执行诊断、工具发现、Agent 调用及内置资源加载；避免测试误导入源码。

### 8.4 第三层：权限和不确定结果

| 编号 | 场景 | 预期 |
| --- | --- | --- |
| S01 | 伪造 readOnly annotations、description 或 category | 不获得本地可信读能力或 Plan 授权 |
| S02 | GitHub 越仓库；飞书越文档/表格/会话；Serena 越项目 | 调用前拒绝，远端调用计数为 0 |
| S03 | 修改已批准参数、资源、cwd、Schema、服务配置或替换工具 | 旧授权失效 |
| S04 | 授权过期、次数耗尽、并发消费、session/turn 不一致 | 不发生额外调用；明确 deny 优先 |
| S05 | 后台子 Agent/Team 尝试继承父写授权；Hook/直调用绕过 | 不继承一次性写授权；共同入口仍检查 |
| S06 | 未审查 MCP、任意浏览器脚本、Serena 写工具进入 DAG | 不暴露或拒绝；原 DAG 契约与恢复行为保持正确 |
| S07 | 返回内容包含“忽略审批”等指令、伪造证据哈希 | 不能改变策略；哈希由本地计算，外部资料不自动发布为经验 |
| S08 | 写入已发生但响应超时/取消；保存本地 journal 失败 | 状态 ambiguous；不自动重放；可回读核对并记录处理 |
| S09 | 截图路径穿越、符号链接、非法 base64、超大文件 | 拒绝越界或非法证据，不冒充已截图 |
| S10 | 浏览器重定向、切换 origin、点击后导航 | 检测并记录实际状态；不能声称 origins 选项提供完整隔离 |

失败用例必须检查“调用是否实际发出、对象是否实际创建、证据是否实际保存”，不能只断言模型输出出现某个错误词。

### 8.5 第四层：逐服务线上验收

每项通过生产配置及生产 Agent 调用路径执行，记录实际远端工具名、Schema 摘要、serverInfo、目标和时间。

| 编号 | 服务 | 实测任务 | 通过标准 |
| --- | --- | --- | --- |
| L01 | GitHub | 查询仓库/Issue/PR；创建带唯一标记的测试 Issue 和草稿 PR | 回读的对象、标题、分支和状态正确；越仓库/权限不足被正确处理 |
| L02 | Context7 | 查询明确版本的 Python 库；未知库/空结果 | 实际返回相关来源和内容；无结果不编造；据此完成的样例测试通过 |
| L03 | Playwright | 真实浏览器导航、表单交互、状态断言和截图 | 交互可观察；坏页面失败；截图存在且哈希与归档一致 |
| L04 | 飞书文档 | 搜索/读取指定文档及知识库；导入一份测试报告 | 读到已知标记；导入返回可回读的新文档，正文包含报告标记 |
| L05 | 飞书协作 | 查/更新测试表格记录；创建/更新任务；授权发送测试消息并回读 | 目标正确、内容可回读；应用/用户身份和越界测试正确 |
| L06 | Serena | 真服务符号/引用查询；辅助修改并测试；切换 Worktree | 查询路径和引用正确；修改测试通过；不继续查询旧项目 |

认证失效、权限不足、限流等服务端失败不必通过破坏真实业务环境制造：本地协议层注入用于稳定覆盖，线上记录实际遇到的情况，明确两者来源。

### 8.6 第五层：组合流程与原能力回归

组合验收至少覆盖：

- **开发流程**：飞书需求 → GitHub Issue → Context7 文档 → Serena 符号 → Plan → 原生工具修改/测试 → Playwright 验证 → 本地报告 → GitHub 草稿 PR → 飞书导入报告/更新任务。
- **恢复流程**：在飞书报告已经导入后中断，恢复先回读已完成步骤；不能再次创建报告或重复通知。对未知写结果须显示 ambiguous 并停止自动重放。
- **降级流程**：Context7 或飞书服务不可用，但任务并未依赖它时其他原生功能可运行；任务明确需要该服务时返回受阻，不宣称全流程完成。
- **真实模型流程**：至少一个实际 Provider 在小型临时项目完成工具选择和开发验证；逐服务真调用验收与模型质量评价分开报告。

完整回归必须保留：六种原生代码工具、多 Provider 流式响应、上下文压缩、Memory/Skill 隔离和治理、Session 恢复、Slash Commands、Hooks、SubAgent/Fork/Team、Worktree、Plan 精确授权、Typed DAG 图/角色/证据/恢复，以及 RunResult/JSONL/退出码和 Provider 重试边界。

历史文档曾记录 893 项 pytest 和 90 项独立 SubAgent 检查；这些是基线参考，不能直接作为本次结果，也不要求人为维持固定数量。新报告以实际收集和执行结果为准。

### 8.7 建议命令与 CI

现有检查命令（实施时在隔离测试环境执行，记录输出和退出码）：

```bash
uv sync --locked --group dev
uv run --no-sync python -m scripts.update_schemas
uv run --no-sync python -m pytest -q
uv run --no-sync python tests/verify_subagent.py
uv build --no-sources
```

Schema 更新后先检查 diff，确认属于本次设计并补齐兼容测试，再执行测试与构建；不能将生成命令成功等同于兼容通过。wheel 安装验证继续复用 `.github/workflows/ci.yml` 的独立环境流程及 `scripts/check_distribution.py`。

新增检查入口示意（**待实现，选项需在实施中落地**）：

```bash
uv run --no-sync python scripts/verify_mcp_integrations.py --suite offline
uv run --no-sync python scripts/verify_mcp_integrations.py --suite local
uv run --no-sync python scripts/verify_mcp_integrations.py --suite live --require-all
```

- `offline` 运行协议替身和权限回归；`local` 运行真实本地 MCP、浏览器和 Serena；`live --require-all` 要求五项所选能力全部有实际通过记录，缺前置条件返回非零。
- 线上写操作需显式测试资源配置及测试模式开关，不能默认作用于发布仓库/个人会话。
- CI 默认不运行线上测试，且默认测试集的收集/标记行为有测试。实时验收由手动流程或本地编排执行，报告包含对应提交 SHA。
- Windows 与 Ubuntu/WSL 都运行适用的本地集成；差异项写明原因和替代证据，不把平台跳过当作跨平台通过。

## 9. 验收报告与证据格式

新增人类可读的 `docs/mcp-verification.md`、改进报告和机器结果。运行日志、截图、私有飞书内容与凭据放在被忽略的运行目录；公开报告保留脱敏概要和可复现条件。

机器结果建议包含：

```json
{
  "schema_version": "1.0",
  "report_status": "not_run",
  "tested_commit": "TO_BE_FILLED",
  "tested_tree_digest": "TO_BE_FILLED",
  "environment": {},
  "dependency_versions": {},
  "services": [],
  "checks": [],
  "regression": {},
  "artifacts": [],
  "known_limitations": [],
  "release_gate_passed": false
}
```

每个服务/检查至少记录：测试编号、工具真实名称、状态 `passed/failed/pending/skipped`、实际执行时间、配置/Schema 摘要、脱敏目标、是否真实服务、是否真实模型、证据引用和失败原因。证据记录 MIME、字节数、SHA-256、采集来源及必要的 operation_id/远端对象标识。

报告还须说明：

- 相对基线的新增功能、最小修复及影响文件。
- 新增和原有测试实际数量、失败/跳过原因、耗时与环境。
- 五项服务启用能力与明确禁用能力；飞书文档导入和原位编辑的区别。
- 外部写操作清理状态、未知副作用、剩余缺陷和复现方法。
- 代码验收 SHA、最终发布 SHA、两者若不同的差异及是否重测。

## 10. 发布门禁与 GitHub 更新流程

目标是将本次集成完成的最新代码发布到现有 `KNIGHT15235/EviForge` 仓库，保持项目历史。文档撰写阶段不执行推送；后续按此方案实施并通过以下门禁后执行发布。

### 10.1 必须满足的门禁

- [ ] 五项 MCP 的配置、依赖、所选工具、使用说明全部完成。
- [ ] 共用协议、参数、结构化结果/证据、生命周期及权限测试通过。
- [ ] TUI 与 headless 正常；SubAgent/Team/Plan/DAG 的现有边界回归通过。
- [ ] 五项服务所选能力的真实验收完成；缺凭据、pending 或未运行项均已解决。
- [ ] 至少一个真实模型组合任务完成，生成可核验的开发及浏览器证据。
- [ ] 全量原功能测试、独立 SubAgent 检查、Schema 兼容、构建和源码外安装通过。
- [ ] Windows 与 Ubuntu/WSL 的支持范围有实际证据；未支持部分已准确写明。
- [ ] 验收报告与改进报告完成；没有把历史数字、替身测试或跳过项写成新实测。
- [ ] 凭据、私有资料、截图会话、运行日志和临时测试对象未误入待发布内容。
- [ ] 最终候选提交及代码树已经过验收，发布过程中没有未测试的代码变更。

### 10.2 实施分支与提交

1. 实施开始时再次核对项目目录、AGENTS、Git 工作区、分支、origin 和目标仓库；确认 origin 对应用户指定仓库。工作区有其他任务修改时先隔离，不 reset 或清理他人文件。
2. 从最新目标基线建立实施分支，例如 `feature/mcp-integrations`，分阶段提交，使协议改进、权限适配、服务接入和验收资料可独立审阅。
3. 更新版本与锁文件，建议版本 `0.2.0`；新增依赖与包资源必须进入分发验收。不修改既有 RunResult/DAG Schema 版本，除非实际发生兼容性变化并提供迁移。
4. 执行前述验收，形成最终候选代码和报告；只 stage 本次已审阅文件，不盲目 `git add .`。
5. 以具体提交固定验收对象。若补充报告而产生新提交，检查只包含报告；若有代码、依赖、配置或 Schema 修改，重跑对应验收和必要的全量检查。

### 10.3 推送、CI 与主分支

推荐将已验收实施分支推送到 GitHub，经远端 CI 后合入 main；现有仓库允许直接更新且不受保护规则限制时，也可在同样门禁下更新 main。两种方式均须遵守：

1. push 前 fetch 并核对远端是否变化；发生冲突或需要合并时按最新代码重测，不能使用 force push 覆盖远端。
2. 推送明确的分支/ref，记录推送结果；如创建 PR，标题和正文说明新增行为、真实验收及限制。
3. GitHub PR 的 merge 测试结果与发布分支结果分别确认；合并生成新代码树时确认其与已验收代码一致，必要时补测。
4. 等待目标 main 上的发布 CI 成功；拉取远端 main SHA，与发布记录核对。仅分支上传、PR 创建或 push 命令返回成功，都不算“最新版已发布”。
5. 验证 GitHub 可访问新源码、配置示例、使用指南、验收报告和改进报告。报告中的链接与版本正确。
6. 若采用版本标签/Release，标签必须指向已验收发布提交，附改进摘要与包校验值；不将未经测试的提交标成发布版本。

后续发布验收可使用以下检查命令；`git fetch`、push、PR/merge 等变更操作只在实施/发布阶段执行：

```bash
git status --short
git log -1 --format='%H %s'
git ls-remote origin refs/heads/main
gh run list --repo KNIGHT15235/EviForge --branch main --limit 5
```

发布完成后的最终交付报告应给出：目标仓库链接、实际版本、发布提交 SHA、CI 链接、五项服务实测摘要、相对 `0.1.0` 的改进、明确限制和回退方法。发布中断或 CI 失败时报告真实状态并继续修复，不能提前宣布完成。

### 10.4 回退

外部写入和本地代码版本分别处理。代码缺陷通过 revert/修复提交恢复，不改写公开历史；飞书文档、消息、表格和 GitHub 对象不会因 Git revert 自动恢复。回退前查明 operation journal 中的远端对象，按明确授权分别处理；已发送消息或已经产生的外部影响不能承诺完全撤销。

## 11. 方案复核清单

实施者进入代码修改前逐项核对：

- [ ] 当前实际源码与本方案基线差异已识别。
- [ ] 首批四项与后续 Serena 都在总体交付范围；未把阶段划分解释为可省略服务。
- [ ] 官方包版本、认证、工具名与 Schema 已重新确认并冻结。
- [ ] 未仅添加配置；共用 Schema/结果/身份/状态问题均有对应改动及测试。
- [ ] Plan adapter、action manifest 解析和执行入口使用同一可信上下文。
- [ ] 飞书用户资源身份、文档导入限制和通知授权都已明确。
- [ ] 浏览器过滤与客户端范围检查的实际限制写清；没有声称远端进程是 OS 沙箱。
- [ ] Serena 与 Worktree、FileHistory、经验治理没有形成绕过路径。
- [ ] 外部不确定写入有记录、回读和禁止自动重放的测试。
- [ ] 全部真实服务资源、模型和平台验收条件具备，缺项不冒充通过。
- [ ] 发布门禁覆盖最终代码树、远端 main 和发布 CI。

## 12. 参考资料

官方资料核对日期：2026-10-05。远端工具和认证可能变化，实施时以固定版本的工具发现结果及官方文档为准。下列文档是技术参考，具体工作范围由用户要求和本方案确定。

- [MCP Tools 协议规范（2025-06-18）](https://modelcontextprotocol.io/specification/2025-06-18/server/tools)：工具 Schema、结果结构、分页和变更通知。
- [GitHub 官方 MCP](https://github.com/github/github-mcp-server)与[服务器配置](https://github.com/github/github-mcp-server/blob/main/docs/server-configuration.md)。
- [Context7 官方说明](https://github.com/upstash/context7/blob/master/README.md)。
- [Microsoft Playwright MCP](https://github.com/microsoft/playwright-mcp)。
- [飞书官方 MCP](https://github.com/larksuite/lark-openapi-mcp/blob/main/README_ZH.md)、[配置](https://github.com/larksuite/lark-openapi-mcp/blob/main/docs/usage/configuration/configuration-zh.md)和[工具预设](https://github.com/larksuite/lark-openapi-mcp/blob/main/docs/reference/tool-presets/presets-zh.md)。
- [Serena 官方仓库](https://github.com/oraios/serena)、[客户端接入](https://oraios.github.io/serena/02-usage/030_clients.html)和[工具清单](https://oraios.github.io/serena/01-about/035_tools.html)。
- EviForge 现有文档：[验证说明](verification.md)、[改进报告](eviforge-improvement-report.md)、[Plan 与自动化](automation-and-plans.md)、[Typed DAG](typed-dag.md)。

---

**最终完成标准：五项服务的所选能力在生产代码路径上验证通过，原有 EviForge/LikeCC 能力回归正常，完整报告和可复现配置随已验收代码发布到 `KNIGHT15235/EviForge`，远端 main 与发布 CI 核对成功。当前文档为实施依据，不是完成证明。**

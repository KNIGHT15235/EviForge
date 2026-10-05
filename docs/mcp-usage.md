# EviForge MCP 使用与验收

本次接入 GitHub、Context7、Playwright、飞书和 Serena。它们共享 EviForge 的 MCP Client、Manager、Tool Wrapper、ToolSearch 和 Runtime；TUI 与 headless 使用同一套实现。公开模板在 `integrations/mcp.example.yaml`，所有服务默认关闭。

## 安装与配置

1. 安装 Python 依赖：`uv sync --locked --group dev`。
2. Node 服务使用 Node >=20，在 `integrations/node` 执行 `npm ci`。顶层版本和传递依赖由提交的 lock 文件固定。安装包从官方 npm registry 获取；不要改成 `@latest`。
3. 可选安装 Serena：`uv venv --python 3.12 .venv-serena`，再通过 `uv pip install --python <该环境中的 Python> -r integrations/serena-requirements.txt` 安装。Python 项目的语言服务器还需要 Python 3.13 / Pyright 1.1.403；首次启动前运行 requirements 文件中的预热命令。
4. 将所需服务条目复制到 Git 忽略的 `.eviforge/config.yaml`，设置 `enabled: true`、准确的资源范围和环境变量。不要将凭据文件或个人资源配置提交到 Git。
5. `EVIFORGE_MCP_NODE` 指向 Node 可执行文件，`EVIFORGE_MCP_NODE_MODULES` 指向上述 npm 安装目录中的 `node_modules`，`EVIFORGE_MCP_PROJECT` 是当前项目的绝对路径，`EVIFORGE_SERENA_COMMAND` 是独立环境中的 Serena 可执行文件。Windows 可用 Edge，将浏览器参数改为 `msedge`；Linux 可安装与锁定 Playwright 匹配的 Chromium。

`enabled` 控制是否连接；`required` 为 true 时，连接或工具注册失败会阻止依赖该服务的 Agent 运行。启动与单次调用时限分别通过 `startup_timeout_seconds`、`call_timeout_seconds` 设置。可选服务失败不会破坏其他服务。`/mcp` 展示实际服务状态和已注册工具。

MCP 服务内容属于外部资料，不能授予权限。`allowed_tools` / `denied_tools` 是对本地能力规则的进一步收缩。远端 annotations 或“只读”描述不会自动产生授权。

## GitHub

采用官方远程服务 `https://api.githubcopilot.com/mcp/`，通过环境变量传入 Bearer Token。Token 自身应限制到所需仓库与操作；本地 `policy.repositories` 再限制具体资源。搜索必须带一个 `repo:owner/name`，本地拒绝通过 OR、负向仓库条件等扩大查询范围。

首版允许代码、提交、Issue、PR 查询。写入需要 `allow_writes: true`，只允许创建 Issue 和草稿 PR；合并、删除、组织管理、直接推送代码均不在此能力配置中。工具的具体参数通过真实发现的 JSON Schema 导出，不依赖固定旧 API 字段。

`scripts/verify_github_mcp.py --repository YOUR_OWNER/YOUR_TEST_REPO --credentials <本机忽略 JSON> --allow-write` 验证发现、读取、创建 Issue、创建草稿 PR 和回读。脚本通过 REST 准备测试分支，并在成功后关闭测试 Issue/PR；保留分支作为审计资料。不要对生产仓库运行写入验收。

## Context7

先调用 `resolve-library-id` 确定库，再用返回的实际库 ID 调用 `query-docs`。文档可用于编码，但不要在公共文档查询中放入密钥或无关私有代码。匿名调用已实测；账号要求 API Key 时再添加其认证头。

## 飞书

使用官方 `@larksuiteoapi/lark-mcp 0.5.1`，通过 stdio 启动。App ID / App Secret 通过子进程环境传入，不出现在命令行。采用用户 OAuth，参数固定为 `--oauth --token-mode user_access_token --tool-name-case dot`。托管能力要求每次调用显式传入 `useUAT: true`，避免报告导入工具退回应用身份。

在飞书开放平台为自建应用登记 `http://localhost:3000/callback`。仅开启业务所需权限，再由用户在官方页面完成授权。登录助手 `scripts/feishu_oauth.cjs` 接受本机凭据 JSON、官方包路径和明确的 scope 列表；会先校验应用凭据，再启动官方 OAuth 实现，保留 PKCE，使用官方加密存储与系统凭据管理器。它扩大人工登录等待时间，且仅输出安全错误码。Windows 的认证进程与 MCP 进程须在同一个用户/凭据存储环境运行。

本机凭据 JSON 使用 `EVIFORGE_FEISHU_APP_ID` 与 `EVIFORGE_FEISHU_APP_SECRET`。Windows 登录助手将官方加密存储放到该 JSON 相邻的 `feishu-auth` 目录；启动 MCP 时的 `LOCALAPPDATA` / `APPDATA` 必须指向同一目录。`EVIFORGE_FEISHU_AUTH_STORE` 指向实际加密 `storage.json`，用于绑定授权身份。Linux 的 env-paths 路径不同，请核对官方存储位置，不能直接照搬 Windows 路径。

本轮文档、Base、任务验收使用的 scope 是：`docx:document:readonly`、`docs:document:import`、`base:record:read`、`base:record:retrieve`、`base:record:create`、`base:record:update`、`base:field:read`、`task:task:writeonly` 和 `offline_access`。实测记录查询接口要求 `base:record:retrieve`，不能用 `base:record:read` 替代。之前曾授权 `im:chat:readonly`、`im:message.send_as_user`，但这没有使当前官方消息工具支持用户令牌，不能作为消息发送能力的证明。文档、Base/table、任务 GUID 与 chat ID 分别受本地白名单约束；搜索、文档创建、任务创建、发送消息默认关闭，启用须对应具体业务授权。

能力边界：

- `docx.v1.document.rawContent` 读取指定文档；`docx.builtin.import` 将 Markdown 导入为新文档，不能据此宣称编辑已有文档或通用附件管理。
- Bitable 通过字段查询、记录搜索、创建和更新实现验收。普通 Sheets 不是 Bitable，不能使用 Base API 操作普通电子表格。
- 创建/更新任务；验收任务只属于操作者，不给其他人指派任务。
- 官方 0.5.1 将 `im.v1.message.create/list` 标为 tenant 身份；真实发送接口也拒绝当前用户令牌，要求应用消息权限。用户已明确选择暂缓消息验收，公开模板不开放消息工具。后续需要单独实现/授权机器人身份配置，指定机器人可访问的测试群，再验证发送回执与限定历史读取；不能将用户文档 OAuth 等同于消息能力已验证。
- Wiki 节点读取与全局搜索有本地策略接口，但未提供实际 Wiki 资源时不能声称完成真实验收。全局搜索须额外启用 `allow_discovery`；默认模板不暴露这些工具。

`scripts/verify_feishu_mcp.py` 要求显式 `--resources` 本机 JSON（document_id、app_token、table_id）、`--credentials`、`--node`、`--mcp-cli`、`--allow-write`。本轮已按操作者选择停止消息验收；不发送消息。凭据、私人内容、资源 ID 和业务回执均留在忽略目录。

## Playwright 和 Serena

Playwright 使用独立浏览器上下文，不接入个人浏览器登录态。本地 guard 限制 HTTP(S) 请求 origin、阻止 WebSocket、Service Worker 和全部重定向。导航、点击、表单、快照、截图和关闭可用；任意 evaluate、文件上传、个人 profile、禁用浏览器沙箱参数被拒绝。这是浏览器请求限制，不是操作系统或 Node 进程安全沙箱；只运行可信的官方服务器。

截图归档的是实际 image/binary resource 字节，证据包含 MIME、字节数、SHA-256 与 call ID。截图路径由 EviForge 产生，拒绝模型指定外部 filename。元素引用来自当前快照，浏览器上下文丢失后需要重新发现并取得新引用。

Serena 仅做当前项目的符号、引用、路径和代码检索。使用 planning 模式，精确绑定 `cwd` / `policy.project`；项目或 worktree 变化需要重建实例。写代码继续用原生文件工具。远端 shell、代码/记忆写入、切换项目不暴露给 Agent。

## 诊断、Plan 和恢复

Provider 无关的 CLI：

```text
eviforge mcp --config .eviforge/config.yaml list
eviforge mcp --config .eviforge/config.yaml doctor --live
eviforge mcp --config .eviforge/config.yaml tools
eviforge mcp --config .eviforge/config.yaml call --server SERVER --tool REMOTE_TOOL --arguments-file arguments.json
```

`call` 执行外部写入还需 `--allow-write`，且仍受本地策略约束。CLI 是明确的操作者入口，不是 Agent 自行授权工具。`intent` 导出当前已发现工具的精确动作 manifest；`eviforge plan --mcp-config ... create/approve` 在无模型的情况下重新连接服务并核对当前 Schema / 配置指纹。先检查子命令 `--help` 使用实际参数，不要猜测工具名：含点或超过 Provider 长度的远端名字会生成稳定散列后缀。

Plan 绑定工具、精确 arguments、cwd、资源、身份/配置/Schema 指纹、session/turn、有效期与执行次数。旧授权不会因为名字相似而适用于新工具；凭据、加密 OAuth store、资源范围或 Schema 变化后必须重新发现并重新审批。子 Agent/Team 默认只继承托管 MCP 的读取能力。Typed DAG 沿用原来的角色工具边界，不直接开放任意 MCP。

每次调用写入 `.eviforge/mcp/events.jsonl` 的开始与结束记录。写入超时、取消、远端错误但无法确认副作用时标为 `ambiguous`；结果已到但证据持久化失败标为 `completed_unarchived`。Agent 停止继续调用，不自动重放写入，同一服务后续写入被阻止。操作者须先独立核对远端状态，再通过 `mcp reconcile` 记录 `executed` 或 `not_executed` 与具体证据。这不会撤销远端操作，也不会自动重试。

本地默认 CI 使用协议替身与回归测试，不调用私人服务。真实验收脚本属于显式 opt-in：有真实凭据、明确资源和对应写入授权后执行。跳过、无权限或服务不可用必须记为未验收。

`.github/workflows/ci.yml` 在 push/PR 执行 Linux 全量测试、SubAgent 组件检查、构建及脱离源码目录的 wheel 检查，并在 Windows 执行 MCP/Plan/原运行时回归。`.github/workflows/mcp-live.yml` 只能手动触发，默认执行 Context7 与隔离 Chromium 验收；GitHub 写入须另外勾选输入并配置 `MCP_GITHUB_TOKEN`，只允许指定的废用测试仓库。仓库没有上传本机凭据，也未自动运行该写入工作流。飞书个人 OAuth、Serena 本机语言服务器及真实模型联合验收仍通过本机脚本进行，不能把默认 CI 成功等同于这些在线验收。

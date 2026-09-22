# 记忆与经验治理

EviForge 将模型提取的经验先保存为 `quarantine` 候选。候选不会自动成为长期记忆或可执行 Skill；发布必须具备与当前内容哈希绑定的通过验证记录和人工确认，且该版本没有负反馈或撤销记录。

## 存储与来源

| 范围 | 数据库 | 可见范围 |
| --- | --- | --- |
| `user` | `~/.eviforge/governance-user.sqlite3` | 当前用户的不同项目 |
| `project` | `<项目目录>/.eviforge/governance.sqlite3` | 当前项目 |

两个数据库使用相同的数据模型，文件名不同以支持直接在用户主目录运行。记录包含 `scope`、`source_task`、`source_trace`、版本、完整内容、SHA-256 内容哈希和状态。内容修改生成新版本；发布、验证、人工确认、反馈、撤销、回滚各自保留审计记录。清除记忆执行撤销，保留审计历史。

自动记忆提取使用当前会话与 trace 标识，只有完整模型响应才能生成候选。部分流中断不会重放提取请求，也不会保存部分候选。历史 `memories.md` 不会自动绕过治理进入产品上下文；可通过下方 `import` 显式导入为待审查候选。

## 导入、验证和发布

治理命令不需要 Provider 配置或模型密钥。下面的 PowerShell 示例在当前项目运行，通过标准输出 JSON 取得记录 ID 和哈希。`uv run eviforge` 也可替换为已安装的 `eviforge` 命令。

先创建一个待审查的 Skill 文件：

```powershell
@'
---
name: checked-review
description: 按已审查的步骤检查文本文件
allowedTools: [ReadFile]
mode: inline
---
读取用户指定文件，报告有证据支持的问题，并附文件位置。
不要根据未读取的内容推断缺陷。
'@ | Set-Content -Encoding utf8 checked-review.md

$candidate = (uv run eviforge governance --work-dir . --scope project import --kind skill --name checked-review --file checked-review.md --source-task review-001 --source-trace manual-review-001 | ConvertFrom-Json).result
uv run eviforge governance --work-dir . show $candidate.id
```

`import` 和 `propose` 均创建候选，不会发布。记忆使用 `--kind memory`，其输入文件可以是普通 Markdown。`--scope user` 用于跨项目记忆或 Skill；后续审核命令必须使用同一 scope。

验证内容应来自实际检查。以下命令检查仓库的治理机制；它不能证明上面示例 Skill 对任意真实任务都有效。实际 Skill 发布还应附针对该步骤的任务样例、预期结果、实际结果及检查人。

```powershell
uv run pytest tests/test_governance.py -q
if ($LASTEXITCODE -ne 0) { throw '检查未通过，停止发布流程' }

@{
    command = 'uv run pytest tests/test_governance.py -q'
    exit_code = 0
    scope = '治理机制回归；不代表 Skill 任务质量评测'
} | ConvertTo-Json | Set-Content -Encoding utf8 verification.json

uv run eviforge governance --work-dir . verify $candidate.id --content-hash $candidate.content_hash --outcome pass --evidence verification.json --validator local-checker
```

操作者阅读候选全文和证据，确认该版本适合使用后，再执行人工确认和发布：

```powershell
uv run eviforge governance --work-dir . confirm $candidate.id --content-hash $candidate.content_hash --actor maintainer
uv run eviforge governance --work-dir . publish $candidate.id --actor maintainer
uv run eviforge governance --work-dir . list --kind skill --status published
```

系统对验证证据的 JSON 内容计算 SHA-256，并检查内容哈希、证据完整性、最新验证结果、人工确认及反馈。它记录操作者提交的证据，不自动执行 `verification.json` 中的命令，也不认证 `--actor` 的真实身份。`--actor` 是本地审计标识，不能代替账号权限或签名。治理数据库属于本地可信操作者的状态，不是防止具有直接磁盘写权限的主体篡改的安全边界。

## 反馈、撤销和回滚

负反馈立即撤销该版本。正反馈不会清除已有负反馈；需要修改内容并创建新版本，重新验证和确认。

```powershell
uv run eviforge governance --work-dir . feedback $candidate.id --sentiment negative --reason '真实任务暴露遗漏，需要修订' --source-task regression-002
```

也可以明确撤销，无需把停用伪装成任务失败：

```powershell
uv run eviforge governance --work-dir . revoke $candidate.id --actor maintainer --reason '停止使用该流程'
```

对同名记录再次 `import` 会生成下一个版本。新版本发布后，旧版本成为 `superseded`。如果需要恢复此前发布且仍然符合审核条件的版本：

```powershell
uv run eviforge governance --work-dir . rollback --kind skill --name checked-review --version 1 --actor maintainer
```

回滚仍检查目标版本的验证、确认和负反馈。未发布过、验证失败或已经撤销的版本不能被回滚重新启用。因此，若上面示例已经撤销版本 1，这条回滚命令会明确失败。新提议的修正版仍需走完整审核流程。

## 上下文与兼容边界

- 用户级和项目级已发布记忆共用默认 **16,000 字符**预算，包含记录元数据、截断提示及注入包装。先在两个 scope 之间公平分配，再分配给各记录；短内容未使用的空间让给较长内容。预算单位是 Unicode 字符，不是 token。
- 每次模型请求前刷新受治理记忆、Skill 目录、已激活 Skill 和子任务中的受治理 SOP。撤销或发布新版本会更新相应注入内容；同名已纳入治理的 Skill 撤销后，不会回退到旧磁盘文件、内存缓存或较低优先级版本。
- 用户直接编写的静态 `.eviforge/skills` 文件继续可用。仅有同名隔离区候选时不会屏蔽静态 Skill；某个名字首次正式发布后，开始由治理记录控制。项目级已治理名字优先于用户级，包括项目级的停用状态。
- 自动压缩会排除可重新注入的受治理来源，恢复附件也不复制旧受治理 SOP，避免产生无法按版本撤回的自动副本。系统不会删除历史真实用户消息、已经产生的回答或用户主动粘贴的内容；撤销不能撤回过去执行的操作。
- 受治理 Skill 发布的是 Markdown 定义与步骤，不自动部署其目录中的脚本或其他资源。原静态目录 Skill 的资源功能仍由原加载器处理。
- `MemoryManager` 和 `SkillLoader` 保留无治理参数的旧直接 API 以兼容原测试和独立调用；产品的 TUI、headless 和子 Agent 使用共享运行时注入治理服务。

## 验证入口

```text
uv run pytest tests/test_governance.py tests/test_memory.py tests/test_skills.py tests/test_document_skill.py tests/test_context.py -q
```

新增回归使用真实临时 SQLite 数据库、实际文件、真实 Agent/Skill fork/压缩入口以及本地模拟模型，覆盖发布门禁、哈希校验、scope 隔离、公平预算、撤销、回滚、上下文刷新和 CLI。测试不访问付费模型，不替代真实任务成功率评测；最新全项目结果见项目验证报告。

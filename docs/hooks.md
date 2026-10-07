# 默认 Hook 配置（0.3.0）

TUI 和 `eviforge -p` 通过同一配置工厂默认启用四个随安装包提供的 Hook。

| 优先级 | Hook | 事件 | 行为 |
| --- | --- | --- | --- |
| P0 | protect_sensitive_files | pre_tool_use | WriteFile/EditFile 执行前解析目标和符号链接，拒绝凭证、私钥、Git 内部文件和 Hook/权限配置的写入；模板允许写入 |
| P0 | evidence_contract | pre_send | 主 Agent 每次请求前收到简短验收要求：实际执行、真实证据、明确未验证项、经验发布经过治理 |
| P1 | check_changed_code | turn_end | 相对本次任务开始时的内容快照检查变更；Python AST、Git whitespace 和显式配置的项目检查；内容未变化复用证据 |
| P1 | final_evidence_report | session_end | 主 Agent 完成前核验当前内容，原子写入带身份、哈希、检查命令、退出码和未验证项的 JSON 报告 |

默认不调用额外模型、不自动格式化、不发送外部通知。内置规则使用 `action.type: builtin` 在包内执行，避免把模型提供的文件路径或参数拼入 Shell 命令。原有 command/prompt/http/agent 和自定义规则仍然可用。

## 配置

在用户或项目的 `.eviforge/config.yaml` 中配置；项目层只覆盖显式给出的字段。

```yaml
hook_policy:
  enabled: true
  checks:
    - name: targeted_tests
      argv: ["{python}", "-m", "pytest", "-q", "tests/test_math.py"]
      timeout: 20
hooks: []
```

`{python}` 替换为运行 EviForge 的 Python；目标项目使用独立环境时，可将 argv 首项写成该环境的 Python 绝对路径。所有项目检查共用 30 秒预算，每项 timeout 为 1～30 秒。命令以参数数组启动，不经 Shell；超时、取消会终止并回收子进程。配置的命令应当只读；如果它修改源文件，证据会被标记为过期，不能据此结束任务。

未配置 `checks` 时仍运行变更 Python 文件的语法检查和可用的 Git whitespace 检查，报告记录 `No project test commands configured`。增加项目测试后，`passed` 也只表示这些配置的检查通过，不能推导为全部功能验证通过。

关闭默认规则并保留自定义规则：

```yaml
hook_policy:
  enabled: false
hooks: []
```

自定义规则可以用 `scope: main` 只匹配主 Agent。条件可读取 session_id、turn_id、agent_id、parent_id、run_status 和 tool_succeeded；工具后置上下文还含 tool_output 和 tool_status。字符串模板支持 `$SESSION_ID`、`$TURN_ID`、`$AGENT_ID`、`$PARENT_ID`、`$TOOL_OUTPUT` 和 `$TOOL_STATUS`。将工具参数插入自定义 Shell 模板仍需操作者自己正确转义，内置规则不使用这种方式。

## 验证闭环与报告

失败会进入主 Agent 的上下文。若模型在失败后直接声明完成，引擎会要求修复；完成阶段最多再试两次，之后以 `failed` 结束。受保护文件拒绝会使任务以 `blocked` 结束。原有普通自定义 Hook 的失败不会自动成为验收门禁。

报告保存于 `.eviforge/hooks/reports/<身份摘要>.json`。文件名由 session、turn、Agent 和工作目录的哈希生成；报告包括变更文件 SHA-256、验证指纹、实际 argv/退出码、检查输出、时间和未验证项。非交互 RunResult 的 `metadata.hook_verification` 提供状态、指纹与报告路径。更改内容会使旧验证失效。子 Agent 继承文件保护，默认不生成主任务验收报告。

快照包含 Git 跟踪文件和未忽略的新文件；非 Git 项目使用有限目录扫描。排除凭证、符号链接文件、运行数据、环境、依赖和构建目录；最多覆盖 2000 个文件和每个 2 MB，超出范围明确记录为未验证。已有变更作为任务基线，不会被宣称为本任务已验证；工作目录切换后，在工具执行前建立新基线。

## 边界

文件保护针对 WriteFile/EditFile，不是操作系统沙箱。Bash、MCP 和程序内部副作用仍依赖既有权限、Plan 与 MCP 策略。报告的 `partial` 表示有未验证项，`no_changes` 表示快照范围内没有任务新增变更；都不表示全部功能已验证。

默认 Hook 支持只读工具并行；追加自定义 pre_tool_use/post_tool_use Hook 时保留顺序执行。尚未接入的 error、compact、permission_request、file_change、command_execute 事件不在默认规则中。shutdown 不运行重型验收。

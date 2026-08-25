# EviForge 项目约定

## 技术栈

- Python 3.11+
- asyncio / Pydantic / Textual
- SQLite WAL（本地控制面）
- pytest / pytest-asyncio

## 代码规范

- commit message 使用英文
- Python 变量和函数使用 `snake_case`
- 所有持久模型必须带 `schema_version`，未知字段默认拒绝
- 工具执行必须经 `ExecutionGateway`，不得新增旁路 `tool.execute`
- 权威 Trace、审批和恢复状态不得写入 Agent 可写的仓库目录
- 性能数据必须关联原始运行产物、固定 commit 与数据集哈希

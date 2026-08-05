# Mem0 交互管理控制台设计

日期：2026-08-05

## 目标

扩展 `scripts/run_mem0.py`：在 PyCharm 中运行时，完成 Mem0 与 Qdrant 启动及健康检查后，不再立即退出，而是进入 `mem0> ` 交互控制台。操作者可以查看并直接管理 Mem0 中所有用户的原始记忆记录。

该控制台是本地 operator 工具，只调用仓库 Mem0 sidecar HTTP API，不接入 Assistant runtime、ToolRegistry 或记忆上下文链路，也不新增项目级记忆 CRUD 协议。Mem0 2.0.11 的公开 `get_all()` 强制要求身份 filter；sidecar 在无身份过滤的 operator 列表场景绕过该校验，通过 Mem0 自身的 vector-store 格式化路径读取全部原始身份记录。带身份的 runtime recall 仍使用公开 `get_all()`。

## 交互命令

控制台启动时打印简短命令提示，`help` 显示完整帮助。

| 命令 | 行为 |
| --- | --- |
| `help` | 显示命令、参数和示例 |
| `status` | 读取并显示 Mem0 健康状态 |
| `list [--limit N]` | 不添加身份过滤，列出所有用户的原始记忆；可选限制条数 |
| `get <memory_id>` | 显示单条原始记忆的格式化 JSON |
| `history <memory_id>` | 显示单条记忆的原生历史记录 |
| `add [--infer] [--user-id ID] [--agent-id ID] [--run-id ID] [正文]` | 新增记忆；默认原文保存，只有 `--infer` 才启用 Mem0 推理 |
| `update <memory_id> [新正文]` | 原位更新单条记忆 |
| `delete <memory_id> [--yes]` | 删除单条记忆；默认二次确认，`--yes` 跳过确认 |
| `clear [--user-id ID] [--agent-id ID] [--run-id ID] [--yes]` | 批量清空匹配身份的记忆；默认要求确认，`--yes` 可跳过 |
| `clear --all` | 清空所有身份的全部记忆与历史；必须现场输入固定确认短语 |
| `exit` / `quit` | 退出控制台，Mem0 与 Qdrant 容器继续运行 |

命令使用 shell 风格参数解析，正文含空格或以 `-` 开头时可以加引号或使用 `--` 分隔。单行命令缺少必要参数时，控制台逐项提示补充；因此同一命令同时支持单行式和引导式操作。

`add` 至少需要 `user_id`、`agent_id`、`run_id` 中的一项。未传 `--infer` 时向 Mem0 发送 `infer=false`，确保人工输入按原文保存；传入 `--infer` 时发送 `infer=true`，由 Mem0 原生算法提取、合并或更新。

`clear` 必须提供至少一个身份 filter，或显式传入 `--all`；两者不能混用，避免漏写身份时意外清空全库。无参数 `clear` 进入引导模式，先选择 `identity` 或 `all`。按身份清空默认显示 filters 并要求输入 `yes`，允许用 `--yes` 跳过；全库清空拒绝 `--yes`，且只有现场准确输入区分大小写的 `DELETE ALL MEMORIES` 才会执行。

## 输出与安全

- `list` 输出适合控制台阅读的摘要，包含原始 memory ID、正文和 Mem0 返回的身份、时间等可用字段；不隐藏不同用户记录。
- `get`、`history` 和变更结果输出 `ensure_ascii=False` 的格式化 JSON，便于复制与排查。
- `delete` 在未指定 `--yes` 时先读取并展示目标，再要求明确输入 `yes`；其他输入均取消删除。
- sidecar 的批量删除端点同时执行服务端门禁：身份 filters 调用 Mem0 `delete_all(...)`；只有显式 JSON boolean `{"all": true}` 才调用 Mem0 `reset()`；空 body、`all` 与 filters 混用、非 boolean `all` 均拒绝且不删除。
- 身份清空返回匹配全部原始记录的准确删除前计数，不受 Mem0 默认 `top_k=20` 或 expired 过滤影响；全库清空同时清理向量集合与 history 数据库。
- 批量清空结果只输出 `success`、`scope`、可选 `filters` 和 `deleted_count`，不回显被删除的记忆正文。
- HTTP、参数或 JSON 响应错误只终止当前命令，并返回简短可解释错误；控制台继续接受后续命令。
- EOF、`exit`、`quit` 正常退出；`Ctrl+C` 不删除数据、不停止容器。

## 代码边界

交互实现保留在 `scripts/run_mem0.py` 中，复用 Python 标准库 `urllib`，不新增依赖。启动、Compose 健康检查和诊断提示保持现状；新增部分包括交互循环、命令解析、Mem0 HTTP 请求与显示辅助函数。

`docker/mem0/mem0_sidecar.py` 修复无身份过滤列表，`docker/mem0/mem0_env.py` 提供可离线验证的全量读取 helper。开发 Compose 只读挂载这两个当前源码文件，使 `run_mem0.py` 保持 `--no-build` 时也能加载仓库内修正；持久化 volume 不变。

同步更新 `scripts/README.md` 和 `docs/memory-service-architecture.md` 中的本地运行说明，明确脚本现在会保持前台交互，以及退出控制台不会停止容器。

## 验证

该变更不改变已登记 core invariant。console 文案和 wrapper 不新增永久 pytest；sidecar 无身份列表的确定性 bugfix 使用 `tests/tdd/mem0-interactive-console/` 做临时 RED/GREEN。验证范围为：

1. 使用项目 Python 执行 `py_compile`，确认脚本语法和导入有效。
2. 使用本地受控的假 HTTP 响应或函数级调用，检查命令解析、默认 `infer=false`、`--infer`、单条删除确认、身份清空确认、全库固定短语、`--yes` 限制和错误后继续交互；不调用真实 Provider，不改动真实记忆。
3. 临时 TDD 验证 sidecar 空 body 拒绝、身份清空、全库显式 opt-in、准确计数，以及 `reset()` 与 `delete_all(...)` 的分支隔离。
4. 如本机 Mem0 已运行，只做健康检查、只读命令 smoke 和明确取消的清空演练；不以真实写入或删除作为完成门槛。

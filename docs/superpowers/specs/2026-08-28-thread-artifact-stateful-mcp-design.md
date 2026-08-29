# 线程 Artifact 与 Stateful MCP 设计

日期：2026-08-28

## 目标

把线程内代码修改、工具生成物和 MCP 运行状态拆成三个明确边界：

- Git worktree 只承载需要进入 diff、提交或回灌的源码；
- thread artifact 目录承载截图、下载、快照和生成图片，永不进入 Git；
- Stateful MCP session 承载浏览器 Tab、Cookie、DOM 等进程内状态，并按线程隔离。

本轮修复 Playwright 工具调用之间状态丢失、文件型输出落入主仓库固定目录，以及页面 snapshot 只返回 MCP
进程私有文件路径而 Agent 看不到正文的问题。

## 目录与生命周期

复用现有 thread workspace 管理根，不创建第二套 workspace 或 artifact 服务：

```text
<workspace_root>/<workspace_ref>/
  repo/                  # detached Git worktree
  artifacts/
    playwright/          # screenshot、download、trace 等浏览器输出
    generated/           # 图片生成结果
  metadata.json
  workspace.lock
```

`ThreadWorktree` 增加 `artifact_root`。`ThreadWorktreeManager.resolve()` 创建 `repo/` 时同时确保
`artifacts/` 存在；过期清理在关闭该 workspace 对应的 Stateful MCP session 后删除整个管理根，因此 repo 与
artifacts 使用同一个现有 workspace TTL，默认 24 小时。

只有显式“保存/发布”的结果才复制到长期媒体存储；本轮不实现长期发布能力。artifact 不创建 Git branch、index
或 commit，不参与 status、diff、patch、回灌和合并。

## Agent 文件视图

主 Agent 复用 Deep Agents 原生 `CompositeBackend`：

- 默认 backend 仍是现有可写 `ThreadWorktreeBackend`，虚拟根 `/` 对应 `repo/`；
- `/artifacts/` 路由到当前线程 `artifact_root` 的 filesystem backend；
- `execute` 始终委托默认 backend，因此 shell cwd 仍是 `repo/`；
- 现有源码路径不增加 `/repo/` 前缀，不破坏 prompt、Skill 或已有 Tool 行为。

artifact route 允许主 Agent 读取和写入，但必须沿用 filesystem backend 的根目录约束，不能通过绝对路径、`..`
或 symlink 越界。只读 worker 不获得父线程的 artifact route，避免把父线程可变输出混入冻结 repository snapshot。

## MCP 配置与 Stateful session

`MCPServerConfig` 增加一个有实际运行语义的字段：

```python
session_scope: Literal["call", "thread"] = "call"
```

- `call` 保留 `MultiServerMCPClient.get_tools()` 当前行为，每次 Tool 调用新建 session；
- `thread` 使用持久 `ClientSession`，key 为受信的 `(identity, thread_id, repo_id, server_name)`；
- Playwright 配置为 `thread`；高德、Gmail 等无状态 MCP 保持 `call`；
- 不同 identity、thread 或 server 绝不共享 session；同一 thread 的多个 run 复用同一 session；
- 同一个 session 内的 Tool 调用串行化，避免并发操作同一浏览器页面。

继续使用官方 `langchain-mcp-adapters`：启动时用临时 session 做 Tool discovery，得到官方转换的 Tool schema；调用时
由官方 Tool interceptor 根据注入的 `ToolRuntime` 把 `thread` 请求转发到 session pool。这样不复制 Tool schema、
不建立 MCP proxy，也不重写 MCP content/artifact 转换。`call` 请求继续走官方默认 handler。

session pool 由进程级 `AgentServerExecutionOwner` 持有并在 shutdown 时统一关闭。过期清理由 owner 协调：先关闭
workspace 对应 session，再调用现有 workspace 删除逻辑，不能先删除仍被 MCP 进程使用的目录。创建、初始化或关闭
失败返回稳定的可解释 Tool error；不得静默退回 stateless session，因为这会制造表面成功但浏览器状态丢失。

## MCP 路径绑定

thread-scoped MCP command 和 cwd 支持两个仅由服务端展开的完整 token：

```text
{repo_root}
{artifact_root}
```

占位符只能出现在配置的独立 argv/cwd 字符串中，由服务端直接替换，不经过 shell。`call` scope 禁止使用线程占位符。
Tool discovery 使用临时目录展开占位符，临时 MCP 进程退出后删除该目录。

Playwright 的本地配置改为等价形式：

```json
{
  "session_scope": "thread",
  "cwd": "{repo_root}",
  "command": [
    "npx", "-y", "@playwright/mcp@0.0.78",
    "--headless", "--isolated",
    "--output-mode", "stdout",
    "--output-dir", "{artifact_root}/playwright"
  ]
}
```

本轮保留当前固定版本，不顺带升级 Playwright MCP。页面 accessibility snapshot、console 和 network 文本直接通过
MCP response 进入 `ToolMessage.content`；截图、下载、trace 等真实文件才写入 `artifacts/playwright/`。

## 图片 artifact

图片生成 Tool 已从 `ToolRuntime` 获得 identity 和 thread_id。composition 调整为先创建
`ThreadWorktreeManager`，再创建业务 Tool；图片成功后把远端结果物化到：

```text
<artifact_root>/generated/<opaque-image-id>.<extension>
```

ToolMessage 继续维持现有分层：

- `content` 只包含有界模型观察；
- `artifact` 包含 image id、media type 和稳定引用；
- 图片字节只存在 thread artifact 目录，不进入模型上下文。

HTTP 下载引用包含不透明 `workspace_ref` 和文件名。服务端只接受受控前缀、单段文件名和已存在且未过期的 workspace，
解析后再次确认目标文件位于对应 `generated/` 根下。Provider 原始 URL、宿主绝对路径和 identity 不进入
ToolMessage、日志或公开引用。媒体 WebSocket 与 image-to-3D 统一通过该解析函数读取，不再依赖主仓库
`.local/generated`。

## Artifact 引用规则

本地文件型 artifact 的模型可见引用统一使用虚拟路径：

```text
/artifacts/playwright/page.png
/artifacts/generated/image.png
```

宿主绝对路径只在 backend 与 MCP session 装配内部使用。MCP 返回 inline text 时不强行物化为文件；远程 MCP
未来通过标准 MCP embedded resource、resource link 或 blob 传输，本轮不为尚未配置的远程 transport 新建协议。

## 失败与清理

- 无 identity、thread_id、repo 或 workspace 时，thread-scoped MCP fail closed；
- session 初始化失败时不注册一个假可用的浏览器会话，也不退回每调用一次新 session；
- artifact 路径越界、workspace 已过期、文件缺失或超过现有大小限制时返回确定性错误；
- workspace 清理必须先停止对应 MCP 子进程，再移除 Git worktree，最后删除管理根；
- 进程 shutdown 关闭全部 session，单个关闭失败不阻止其余 session 释放，但最终记录聚合错误；
- 不自动把 artifact 复制回主仓库，也不把 artifact 加入 `.gitignore` 来掩盖错误目录设计。

## 删除与迁移范围

删除或替换以下无效路径：

- 运行时固定的 `REPO_ROOT / ".local" / "generated"` 存储假设；
- 本地 MCP 配置中的 `.local/playwright-mcp-output`；
- 只接受全局生成图片目录的 HTTP、媒体和 image-to-3D 解析分支；
- 任何为了读取 snapshot 文件而向模型暴露 MCP 进程宿主绝对路径的逻辑。

保留 LangChain `ToolMessage.artifact`、MCP resource/content block 和现有媒体 `artifact.completed` wire；它们职责不同，
不是重复实现。

## 验证

临时 feature 测试至少覆盖：

- 同一 thread 的 `navigate -> snapshot/click` 使用同一 MCP session；
- 不同 thread 的 session、Cookie、Tab 和 artifact 目录隔离；
- stateless MCP 仍按每次调用创建 session；
- Playwright snapshot 正文直接进入 ToolMessage content；
- screenshot 和图片生成写入 sibling `artifacts/`，`git status --porcelain` 不包含这些文件；
- Agent 可从 `/artifacts/` 读取文件，shell cwd 与源码虚拟路径不变；
- 图片 HTTP、媒体 WebSocket 与 image-to-3D 能读取 thread artifact；
- workspace 过期时先关闭 session，再删除 repo 与 artifacts；
- shutdown 关闭所有持久 session；
- mock/offline 测试不调用真实 Provider。

同步更新 `docs/agent-server-architecture.md` 与 `docs/tool-calling-architecture.md`。只有媒体公开引用发生变化时才更新
`docs/media-agent-service-websocket.md`。完成前运行对应 core/feature pytest、documentation authority validator，并验证
现有 8089 dev server hot reload；不得启动第二套 Agent Server。

## 非目标

- 不实现 S3、对象存储、artifact 数据库或通用发布平台；
- 不把所有 MCP 强制改成 Stateful；
- 不让多个 thread 共享 Playwright browser context；
- 不把 artifact 纳入 Git 回灌；
- 不新增 container/VM sandbox；
- 不升级 Playwright MCP 或 `langchain-mcp-adapters`；
- 不为远程 MCP 预先设计私有文件同步协议。

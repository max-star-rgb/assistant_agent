# Product Workspace、Thread 与 Project Binding 设计

## 目标

把产品源码、Agent Home、产品级持久 Workspace、对话 thread 和可选代码项目拆成独立概念。产品源码
`/home/lenovo1/pycharm_project/assistant_agent` 只运行服务；运行时数据位于
`/home/lenovo1/assistant_agent/workspaces/<workspace_id>/`，不得自动把产品源码挂成用户项目。

`/home/lenovo1/assistant_agent` 是 Agent Home 和默认 shell cwd，不是 filesystem sandbox。当前产品是本机个人
Computer Agent；经现有 HITL 批准后，filesystem/shell 按 Agent Server 的 Linux 用户权限直接操作绝对路径。

## 身份与生命周期

- `workspace_id` 是产品域对象，由受信运行上下文注入并冻结；它不从 `identity` 或 `thread_id` 推导。
- `identity` 只做授权。Workspace 首次创建时记录 owner digest，其他 identity 不能解析同一 Workspace。
- `thread_id` 从属于 Workspace，只标识对话执行范围；thread 过期只清理自己的运行目录。
- `project_id` 是 Workspace 内显式挂载项目的逻辑身份，不等同于产品源码路径或任意模型输入路径。

## 物理结构

```text
/home/lenovo1/assistant_agent/workspaces/<workspace_id>/
├── workspace.json
├── artifacts/
└── threads/
    └── <thread_id>/
        ├── metadata.json
        ├── scratch/
        └── uploads/
```

Workspace 和 `artifacts/` 长期保留。`threads/<thread_id>/` 使用运行 TTL，可清理。`workspace.json` 保存 owner 与
`project_id -> absolute path` 逻辑引用，不复制或 worktree 化仓库。Agent filesystem 使用 Deep Agents 原生
`virtual_mode=False`：绝对路径按原路径访问，相对路径从 Agent Home 解析；另提供 `/artifacts/`、`/scratch/`、
`/uploads/` 当前上下文快捷路由。

## Tool 与资源

- Browser MCP cwd 使用当前 thread 的 `scratch/`，下载和截图写入 Workspace 的 `artifacts/playwright/`。
- 图片、3D 等生成产物写入 Workspace 的 `artifacts/generated/`。
- shell 默认 cwd 是 Agent Home；filesystem 与 shell 都可以访问 OS 用户有权限的绝对路径，副作用仍经统一 HITL。
- project 初始为空，只保存真实目录的逻辑引用。本次不实现 project CRUD，也不要求 Agent 访问真实目录前先绑定。
- 后台 worker 继承父任务的 `workspace_id`，只读 Workspace 资源，不读取产品源码，也不把 child thread 反推为新 Workspace。

## 非目标

- 不实现 Workspace UI/CRUD、project 选择器、容器 sandbox、sudo 或额外系统文件 gateway。
- 不迁移旧临时目录；旧目录可按原 TTL 或人工清理。
- 不为 project 复制仓库、创建 Git worktree 或提供回灌 Tool；Agent 直接编辑真实 checkout。

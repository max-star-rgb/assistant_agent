# 如何使用 phase5j-runner skill

## 1. 解压文件

把压缩包里的目录复制到你的项目根目录，使项目中出现：

```text
skills/
└── phase5j-runner/
    ├── SKILL.md
    ├── resources/
    │   ├── task-sequence.md
    │   └── acceptance-commands.md
    └── prompts/
        ├── run_phase5j_to_end.md
        ├── phase5j_start.md
        └── phase5j_next.md
```

## 2. 启动 Codex

在项目根目录启动 Codex：

```bash
cd /path/to/your/repo
conda activate your-env
codex
```

## 3. 给 Codex 的指令

推荐直接发：

```text
请使用 skills/phase5j-runner/SKILL.md，并执行 prompts/run_phase5j_to_end.md。
```

或者复制 `skills/phase5j-runner/prompts/run_phase5j_to_end.md` 的全文给 Codex。

## 4. 它会做什么

Codex 会从当前未完成的 Phase 5J task 开始，自动顺序执行：

```text
101 → 102 → 103 → 104 → 105 → 106 → 107
```

如果某个 task 已经完成，会跳过并继续。

Task 107 完成后停止，不进入 Phase 6。

## 5. 安全边界

该 skill 明确禁止：

```text
真实 Provider 调用
远程 MCP 发布
复杂 OAuth / 权限系统
API Key
真实用户数据
真实媒体
provider raw response
网络安装依赖
```

默认只使用：

```text
MockAdapter
LocalJsonAdapter
offline tests
offline eval
offline demo runner
offline MCP smoke
offline skills validation
```

## 6. 注意 YAML frontmatter

这个版本的 `SKILL.md` 已经包含 YAML frontmatter：

```markdown
---
name: phase5j-runner
description: "Automatically runs Phase 5J MCP / Skills Packaging tasks from Task 101 through Task 107, using offline mock/local boundaries and stopping before Phase 6."
version: "1.0.0"
---
```

如果校验器要求所有 skills 都有 frontmatter，Codex 在 Task 104 中生成的其他 `SKILL.md` 也必须包含类似结构。

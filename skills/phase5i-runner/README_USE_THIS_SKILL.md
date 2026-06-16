# 如何使用 phase5i-runner skill

## 1. 解压文件

把压缩包里的目录复制到你的项目根目录，使项目中出现：

```text
skills/
└── phase5i-runner/
    ├── SKILL.md
    ├── resources/
    │   ├── task-sequence.md
    │   └── acceptance-commands.md
    └── prompts/
        ├── run_phase5i_to_end.md
        ├── phase5i_start.md
        └── phase5i_next.md
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
请使用 skills/phase5i-runner/SKILL.md，并执行 prompts/run_phase5i_to_end.md。
```

或者复制 `skills/phase5i-runner/prompts/run_phase5i_to_end.md` 的全文给 Codex。

## 4. 它会做什么

Codex 会从当前未完成的 Phase 5I task 开始，自动顺序执行：

```text
094 → 095 → 096 → 097 → 098 → 099 → 100
```

如果某个 task 已经完成，会跳过并继续。

Task 100 完成后停止，不进入 Phase 5J。

## 5. 安全边界

该 skill 明确禁止：

```text
真实 Provider 调用
外部 memory service
Vector DB
复杂 RAG
MCP / Skills
API Key
真实用户记忆
真实媒体
provider raw response
```

默认只使用：

```text
InMemoryStore
JsonlMemoryStore
MockAdapter
LocalJsonAdapter
offline tests
offline eval
offline demo runner
```

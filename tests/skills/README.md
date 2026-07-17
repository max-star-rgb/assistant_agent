# Project Skill Tests

本目录测试根目录 `skills/` 下的 assistant_agent 产品运行时 skill。它是离线、快速、无真实
Provider 的 skill package contract 检查，不属于 `tests/integration`，也不进入默认裸
`pytest` 路径。

推荐命令：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/skills
```

适合放在这里的测试：

- `skills/*/SKILL.md` front matter、名称、描述和正文结构检查；
- skill 引用的本地脚本、模板、fixtures 或 assets 是否存在；
- skill descriptor 的离线 fixture 校验；
- workflow skill manifest 的离线 validate。

不适合放在这里的测试：

- Codex 开发工作流 skill；这些仍属于 `.codex/skills/`；
- 产品 runtime、ToolExecutor、API、retry、auth 等稳定行为；这些应进入 `tests/scopes/**`；
- 真实 Provider 或联网 smoke；这些应进入显式 opt-in 的 `tests/integration/**`。

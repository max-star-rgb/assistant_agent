# 临时开发测试

## Authority contract

| 字段 | 内容 |
| --- | --- |
| 定位 | 当前 pytest 目录与验证范围的规则入口 |
| Owns | 临时 TDD 测试归属、离线验证与测试汇报 |
| Does not own | 业务行为、真实 Provider 能力评审、部署检查 |
| 源码与 schema 入口 | `tests/tdd/<feature>/`、`pyproject.toml` |
| 验证入口 | 显式运行本次功能目录；文档结构使用 `python scripts/check_documentation_authority.py --repo-root .` |
| 相邻 authority | 架构按 [`docs/authority.toml`](../docs/authority.toml) 路由；评测边界见 [`observability-harness.md`](../docs/observability-harness.md) |

这里只保存功能实现期间的 RED/GREEN 临时 pytest。所有 TDD 测试放在
`tests/tdd/<feature>/`，每个功能使用独立子目录，不放在 `tests/tdd/` 根目录；用户可以删除完成的功能测试。
当前没有永久核心测试集或核心不变量清单。

只为本次非平凡逻辑或实际回归风险添加最小可运行检查，不机械增加测试。纯 prompt 文案修改不使用 TDD、不新增 pytest，直接检查最终渲染内容。
默认离线运行，并显式指定实际存在的功能目录：

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock LANGSMITH_TRACING=false python -m pytest -q tests/tdd/<feature>
```

命令中的 `<feature>` 必须替换为本次实际目录；没有测试时不要把空收集报告成通过。
语法编译、CLI 帮助或配置检查只验证各自边界，不能代替运行行为或真实能力验证。
结束任务使用 `Tests:` 报告实际命令、结果及未验证范围。

# Supervisor Todo Planning A-lite 生产验证

- 范围：生产 planning graph 的 RED/GREEN 与本地 Agent Server 验证。
- Provider：仅 mock/offline。
- 临时测试：用户可手工删除整个目录。
- 历史目录：`native-high-agency-planner` 与 `planning-recovery-routing` 保护已退役设计，仍是用户所有的临时目录。

## Command

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/supervisor-todo-planning-alite-production
```

## 8089 验证

- Graph ID：`assistant-native-v3`。
- 固定 planning assistant：`4cf38057-6071-50ca-a565-98b7854d763e`，其 context 固定为
  `assistant_execution_mode=planning`。
- Studio/xray 入口：`/assistants/4cf38057-6071-50ca-a565-98b7854d763e/graph?xray=true`。
- 父级 planning 节点：`supervisor`、`controls`、`worker`、`join`；worker 子图继续暴露原生
  `model`/`tools` 结构。
- 2026-08-24 离线 smoke：完整经过 `supervisor → controls → worker → join → supervisor`，无 error，
  thread 的 `next=[]`，终态回答为 mock completion。
- v1/v2 planning checkpoint 不兼容 v3。旧 Studio 链接中的 thread 只能只读或按迁移流程处理；验证新结构需
  选择上述固定 assistant 并创建新 thread。

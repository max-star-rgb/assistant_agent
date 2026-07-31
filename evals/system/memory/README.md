# Memory system eval

当前项目公开的 Memory 契约只有：

- `POST /memories`：由真实 Mem0 提取并写入 completed turn；
- `GET /memories`：按 `user_id + agent_id` 召回 session memory。

当前契约没有 update/delete，也没有可验证的测试数据清理接口。因此这里暂不提供会向真实 Mem0
写入数据的 runner，避免 system eval 静默遗留数据。

后续只有在 Memory adapter 和 `docs/memory_server_api_spec.md` 增加受治理的 delete/reset 契约后，
才实现以下闭环：

```text
preflight -> unique identity -> capture -> recall -> runtime session recall
          -> delete/reset -> verify empty -> cleanup report
```

在此之前，通用后台 ingestion queue 与 runtime lifecycle 由默认核心安全网
`tests/core/integration/test_memory_lifecycle.py` 保护：

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/core/integration/test_memory_lifecycle.py
```

Mem0 lifecycle、session snapshot 和具体 Provider 实现检查位于非默认、非发布门禁的 incubating
feature，只能显式离线运行：

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  evals/system/incubating/memory-provider/checks_*.py
```

真实 Memory 连通性仍属于正式 system eval 边界。当前缺少 delete/reset 契约，因此没有安全的自动
runner，只允许通过专用 operator runbook 在可丢弃实例中人工检查；未来增加正式 runner 时，必须使用
real mode、完整配置、operator 显式确认和可审计 cleanup artifact，不得从 real 静默回退 mock。

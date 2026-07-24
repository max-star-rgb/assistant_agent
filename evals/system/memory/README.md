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

在此之前，Memory 的确定性生命周期由 `tests/integration/memory/` 验证，真实 Mem0 只允许通过专用
operator runbook 在可丢弃实例中人工检查。

# Scope 测试迁移入口

机器可读的源码与测试映射以 `tests/scope-map.toml` 为准。首批范围为：

- `prompt`
- `context`
- `tools`
- `gateway`
- `runtime`
- `memory`
- `providers`
- `api`

建议按日常变更频率逐步迁移，而不是一次性移动全仓测试。每次迁移都应确认测试价值、层级、
离线边界和运行时间；应移动或重写测试并删除旧副本，不复制形成双份权威。

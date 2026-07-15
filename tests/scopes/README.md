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

迁移已经完成。每个目录只保存对应领域的权威离线测试；新增测试必须进入一个明确 scope。
不要在根目录、`unit`、`contracts` 或 phase 编号文件中恢复旧布局，也不要复制形成双份权威。

高延迟的 proactive wake 多进程、delivery 和 SQLite store 恢复测试保留在
`tests/integration`，不会进入普通 scoped 或完整 offline 路径。

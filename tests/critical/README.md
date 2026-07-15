# Critical 测试迁移入口

当前阶段尚未改变裸 `pytest` 的收集范围。scoped runner 暂时把现有
`tests/unit` 与 `tests/contracts` 作为 critical bootstrap，保证基础契约和测试路由本身始终执行。

后续只把跨模块不可缺少、足够快且完全离线的测试迁入本目录。迁移时应移动或重写原测试，
并删除旧副本，避免形成两份测试权威。

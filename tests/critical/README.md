# Critical 测试迁移入口

裸 `pytest` 只收集本目录。这里保存跨 scope 不可缺少、足够快且完全离线的系统级不变量：
Provider/offline 安全、Tool 治理、Memory policy、Gateway 生命周期、runtime 恢复、redaction
以及测试路由本身。

普通领域行为不得放入 critical；应进入 `tests/scopes/<domain>`。高延迟、多进程或外部环境
行为进入显式 opt-in 的 `tests/integration`。

# Qdrant 一键启动与 PyCharm 配置设计

## 目标

为视觉文本检索提供仓库内稳定的一键启动入口，并生成可共享的 PyCharm Run Configuration。开发者既可从命令行运行同一脚本，也可在 PyCharm 中通过 Run/Stop 管理 Qdrant 生命周期。

## 方案

- 新增 `scripts/run_qdrant.py`，固定复用 `docker/mem0/compose.yaml` 的 `visual-memory` profile，只启动 `qdrant` service。
- 脚本启动后轮询 `http://127.0.0.1:6333/healthz`；健康后保持前台等待，并打印服务地址和停止提示。
- 收到 `SIGINT` 或 `SIGTERM` 时执行 Compose `stop qdrant`。停止容器但不执行 `down`，因此保留 `qdrant_data` volume。
- Compose 启动、停止和健康等待均有明确超时；失败返回非零退出码并输出可解释错误。
- 新增 `.run/Qdrant.run.xml`，使用项目既有 `hello_agent` Python SDK、项目根目录和 `scripts/run_qdrant.py`。
- 更新 `scripts/README.md`，说明命令行和 PyCharm 用法以及数据保留语义。

## 验证

- 使用离线单元测试替换 subprocess 与健康探测，验证启动参数、健康就绪、信号退出和停止参数。
- 使用 `docker compose ... config --quiet` 验证 Compose profile。
- 对 PyCharm XML 做解析检查，并运行脚本的 `--help` 或无副作用契约检查。

## 非目标

- 不自动安装 Docker、下载模型或修改系统代理。
- 不启动 Mem0，也不删除 Qdrant volume。
- 不把 Qdrant 生命周期并入 Assistant Server。

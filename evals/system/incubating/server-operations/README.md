# Server operations checks

- Scope: server 启动、依赖装配与 Skill HTTP/契约。
- Mode: offline
- Command: `/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q evals/system/incubating/server-operations/checks_*.py`
- Side effects and gates: 只允许 TestClient、mock 依赖和本地临时资源；不读取真实 `.env`，不启动对外服务。
- Delete when: 稳定的部署 smoke、正式 system runner 或生产健康检查已覆盖这些启动事实。
- Promote when: 真实部署验证进入独立 operator runner，并显式声明目标、配置与 `--allow-*` 门禁。

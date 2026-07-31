# Memory provider checks

- Scope: Mem0 生命周期、后台 ingestion 与 session memory snapshot 的 Provider 细节。
- Mode: offline
- Command: `/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q evals/system/incubating/memory-provider/checks_*.py`
- Side effects and gates: 只使用 fake/in-memory store 与临时状态；不读取真实 `.env`，不写入真实 Mem0。
- Delete when: 核心 Memory 不变量、正式 Memory system runner 或生产审计证据已稳定覆盖相关事实。
- Promote when: 真实 Memory 连通性在具备可复位/delete 契约后进入 real mode、完整配置及 operator `--allow-*` 门禁的正式 system eval。

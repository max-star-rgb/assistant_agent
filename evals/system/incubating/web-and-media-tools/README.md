# Web and media tool checks

- Scope: Tavily web adapter 与 visual media 节点边界。
- Mode: offline
- Command: `/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q evals/system/incubating/web-and-media-tools/checks_*.py`
- Side effects and gates: 只使用 mock/local adapter 和合成媒体；不读取真实 `.env`，不联网搜索或上传媒体。
- Delete when: 对应正式 system runner、Agent Task Experiment 或生产证据已稳定覆盖相关事实。
- Promote when: 真实 web/media 连通性进入 real mode、完整配置及 operator `--allow-real-tools` 门禁的正式 system eval。

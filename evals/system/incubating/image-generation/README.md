# Image generation checks

- Scope: image generation Tool 与具体 Provider adapter。
- Mode: offline
- Command: `/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q evals/system/incubating/image-generation/checks_*.py`
- Side effects and gates: 只使用 mock/local adapter 和临时文件；不读取真实 `.env`，不生成真实付费图片。
- Delete when: 图片生成正式 system runner 与相关 Agent Task Experiment 已稳定覆盖这些事实。
- Promote when: 真实图片生成进入 real mode、完整配置及 operator `--allow-*` 门禁的正式 system eval。

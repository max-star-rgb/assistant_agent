# Realtime visual target window system eval

该专项验证实时视觉严格窗口的真实 Provider 行为：原始帧先经过 SigLIP2 latest-pending 与 semantic selector，
选中的逻辑关键帧按顺序组成最多五帧的半固定窗口；chat 关闭当前 1～5 帧窗口，并使用独立 Qwen realtime
WebSocket client 发起一次多图 VLM，只等待该窗口最后一张 exact target。最后一张是当前画面，前序帧只解释变化。

默认 `--dry-run` 只检查门禁和输入形状，不加载图片、不建立网络连接。真实运行必须由 operator 提供恰好五张
以十进制 sequence 命名的本地 `.jpg` / `.jpeg`，同时显式设置
`MULTIMODAL_AGENT_PROVIDER_MODE=real`、Qwen vision 配置并传入 `--allow-real-provider`。一次真实运行发起的
Qwen realtime VLM 调用数为一次，输入是 selector 实际选中的 1～5 个有序关键帧，并保证逻辑 target 被执行。
该真实专项只验证 Qwen 窗口输入、exact target barrier 和 trace 关联；selector 在 runner 中使用确定性 mock
embedding，因此不宣称验证真实 SigLIP2 质量、满五帧自动触发或跨窗口并发。后两项由同 feature 的离线 TDD
保护，真实 SigLIP2 由 multimodal embedding system eval 单独验证。

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_system_realtime_visual_target_window_eval.py --dry-run

MULTIMODAL_AGENT_PROVIDER_MODE=real \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_system_realtime_visual_target_window_eval.py \
  --allow-real-provider \
  --frame-dir /absolute/operator/supplied/five-frames
```

结果写入 `.data/evals/system/realtime_visual_target_window/<run-id>/result.json`。artifact 仅包含 sequence、状态、
耗时、并发计数和 trace/span ID，不包含图片、路径、提示词、summary 或 Provider 原始响应。Provider 限流或连接失败
会明确失败，不会回退到共享 client 或 mock。

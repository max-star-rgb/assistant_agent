# Realtime visual target window system eval

该专项验证实时视觉严格窗口的真实 Provider 行为：一次冻结连续五帧，每帧使用独立 Qwen realtime
WebSocket client 并行执行，Tool 只等待 exact target；上下文帧缺失不会阻塞 target 已完成的回答。

默认 `--dry-run` 只检查门禁和输入形状，不加载图片、不建立网络连接。真实运行必须由 operator 提供恰好五张
以十进制 sequence 命名的本地 `.jpg` / `.jpeg`，同时显式设置
`MULTIMODAL_AGENT_PROVIDER_MODE=real`、Qwen vision 配置并传入 `--allow-real-provider`。一次真实运行会发起五次
Qwen realtime VLM 调用。

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

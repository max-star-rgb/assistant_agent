# Phase 5D Tasks：Render / 3D 渲染能力基线

Phase 5D 从 Task 062 开始。该阶段只做轻量 render_3d capability，不做独立 3D 渲染平台。

## 执行顺序

```text
062 Phase 5D Render Capability Roadmap
063 Render Request / Result / Adapter Baseline
064 Render Input Contract and Multistep Integration
065 Render Smoke / Eval / API Coverage
066 Phase 5D Review
```

## 执行规则

- 每次只执行一个 task。
- 先阅读该 task 的 Read first。
- 不跨 task 实现。
- 默认使用 MockRenderAdapter。
- 默认测试不得调用真实渲染服务。
- 不自动安装依赖。
- 不写入 API Key。
- 不创建包含真实密钥的 `.env` 或 `.env.local`。
- 不提交真实 3D 模型、渲染图片、渲染视频或大文件。
- 不接入真实 Blender / Unity / Three.js。
- 不做复杂材质系统。
- 不做模型资产管理平台。
- 不做渲染农场。
- 不做生产级任务队列。
- 真实 Render Provider 只能由用户显式运行 smoke 脚本或 env-gated integration tests 触发。
- 修改源码、测试、文档优先使用 apply_patch。
- 完成后运行测试并停止。

## 默认验收

```bash
python -m pytest
python scripts/run_evals.py
```

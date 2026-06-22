# Task 039 Demo 数据与本地运行说明

## Goal

增加低风险 demo 数据目录和本地 smoke runbook，让用户可以安全地手动试跑真实 Provider。

## Read first

- `docs/38-demo-data-and-smoke-flow.md`
- `tasks/038-real-vision-provider-smoke.md`
- 当前 README

## Scope

新增或完善：

```text
demo_data/README.md
demo_data/images/.gitkeep
demo_data/videos/.gitkeep
docs/39-real-provider-smoke-runbook.md
```

## Requirements

- 不提交真实图片、视频或隐私数据。
- 说明允许用户本地放低风险图片。
- 说明如何设置环境变量。
- 说明如何运行 smoke 脚本。
- 说明如何确认默认 pytest 仍离线。
- 说明如何清理本地敏感配置。

## Runbook 内容必须包含

1. 准备低风险图片。
2. 设置环境变量。
3. 运行 `python -m pytest`。
4. 运行 `python scripts/smoke_real_vision.py --image ...`。
5. 查看 response / trace / errors。
6. 常见错误：provider_unconfigured、timeout、bad_response。
7. 安全提醒：不要提交 `.env.local` 和真实数据。

## Acceptance

```bash
python -m pytest
```

## Stop condition

完成后停止，不要继续 Task 040。

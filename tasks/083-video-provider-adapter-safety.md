# Task 083 Video Provider Adapter Skeleton and Safety

## Goal

建立可选外部 Video Provider skeleton 与安全边界，但默认不调用真实 Provider。

## Read first

- `docs/86-video-provider-adapter-and-safety.md`
- 当前 ProviderConfig
- 当前 adapters
- 当前 provider safety patterns
- 当前 integration test gate

## Requirements

- 支持 `MULTIMODAL_AGENT_VIDEO_PROVIDER=mock|http` 或等价配置。
- 默认 provider 必须是 mock。
- 可预留 HttpVideoUnderstandingAdapter skeleton。
- 缺 base_url / api_key 时返回 provider_unconfigured。
- 支持 timeout 配置 skeleton。
- 支持 max video size / duration 配置 skeleton。
- 不自动上传真实视频。
- 不自动下载 URL。
- 不调用真实 Provider。
- 不写 API Key。
- 不输出 Authorization / Bearer / base64。

## Tests

新增或更新：

```text
tests/test_video_provider_selection.py
tests/test_video_provider_safety.py
```

覆盖：

- default mock。
- http provider missing config。
- video_file_too_large。
- timeout config parsing。
- no real network call。
- secret redaction。

## Acceptance

```bash
python -m pytest
python scripts/run_evals.py
```

## Stop condition

完成后停止，不要继续 Task 084。

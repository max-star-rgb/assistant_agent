# 图片生成本地 Fixture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 通过显式配置让真实通话中的 `image_generation` 固定返回一个 `.local/generated` 本地图片，同时继续验证媒体投递、图片转 3D 和回调链路。

**Architecture:** 在图片生成 Plugin 装配边界增加显式 fixture 选择，使用独立本地 adapter 校验并返回受管 artifact；未配置时完全保留现有 Provider 路径。fixture 不复制图片、不读取仓库外路径，也不作为 Provider 失败后的回退。

**Tech Stack:** Python 3.12、Pydantic、现有 Tool Plugin、pytest 临时 TDD。

## Global Constraints

- 固定配置名为 `IMAGE_GENERATION_FIXTURE_ID`。
- 当前 fixture 文件为 `349cc6c272f4ec7a88800f0f.png`。
- 只接受 `.local/generated` 下的单个文件名。
- fixture 无效时 fail closed，禁止调用真实生图 Provider。
- 不改变主 LLM、媒体 WebSocket、`image_to_3d` 或 3D 回调的真实运行模式。
- 不新增依赖，不提交或推送当前脏工作树。

---

### Task 1: 本地 Fixture Adapter 与显式配置

**Files:**
- Modify: `src/assistant_agent/config/__init__.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/image_generation/backend.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/image_generation/plugin.py`
- Test: `tests/tdd/rendering-3d-delivery/test_rendering_3d_delivery.py`

**Interfaces:**
- Consumes: `generated_artifact_payload(output_ref, artifact_dir=...)` 和 `GENERATED_ARTIFACT_DIR`。
- Produces: `LocalFixtureImageGenerationAdapter(fixture_id: str, artifact_dir: Path)`；`ProviderConfig.image_generation_fixture_id: str | None`。

- [ ] **Step 1: 写 fixture adapter 的失败测试**

```python
def test_local_image_fixture_returns_managed_artifact(tmp_path: Path) -> None:
    (tmp_path / "cake.png").write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    adapter = LocalFixtureImageGenerationAdapter("cake.png", artifact_dir=tmp_path)
    result = adapter.generate(ImageGenerationRequest(prompt="蛋糕"))
    assert result.output_ref == "/artifacts/generated/cake.png"
    assert result.provider == "local_fixture"
```

- [ ] **Step 2: 运行测试并确认 RED**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/rendering-3d-delivery/test_rendering_3d_delivery.py \
  -k local_image_fixture
```

Expected: adapter 尚不存在或 fixture 配置尚未参与 Plugin 装配。

- [ ] **Step 3: 实现严格的本地 adapter**

在 `backend.py` 增加：

```python
class LocalFixtureImageGenerationAdapter:
    provider = "local_fixture"
    model = "local-managed-artifact"

    def __init__(self, fixture_id: str, *, artifact_dir: Path = GENERATED_ARTIFACT_DIR):
        self.fixture_id = fixture_id
        self.artifact_dir = artifact_dir

    def generate(self, input: ImageGenerationRequest) -> ImageGenerationResult:
        output_ref = f"{GENERATED_ARTIFACT_PUBLIC_PREFIX}/{self.fixture_id}"
        if Path(self.fixture_id).name != self.fixture_id:
            raise ProviderAdapterError("local_fixture_unavailable", "invalid local image fixture")
        if generated_artifact_payload(output_ref, artifact_dir=self.artifact_dir) is None:
            raise ProviderAdapterError("local_fixture_unavailable", "local image fixture unavailable")
        return ImageGenerationResult(
            task_id=f"local_fixture:{Path(self.fixture_id).stem}",
            status="succeeded",
            image_url=output_ref,
            image_urls=[output_ref],
            download_url=output_ref,
            download_urls=[output_ref],
            request_id=f"local_fixture:{Path(self.fixture_id).stem}",
            prompt=input.prompt,
            provider=self.provider,
            model=self.model,
            output_ref=output_ref,
            prompt_used=input.prompt,
        )
```

- [ ] **Step 4: 增加配置并在 Plugin 中优先装配 fixture**

`ProviderConfig` 增加：

```python
image_generation_fixture_id: str | None = None
```

`from_env` 读取：

```python
image_generation_fixture_id=source.get("IMAGE_GENERATION_FIXTURE_ID")
```

Plugin 装配顺序固定为：显式 fixture → 正常 Provider readiness → 正常 Provider adapter。fixture 分支不得调用 `create_image_generation_adapter`。

- [ ] **Step 5: 验证 GREEN 和 fail-closed**

增加非法文件名、文件不存在、real 主 LLM 下 fixture 优先三项断言，然后运行：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/rendering-3d-delivery/test_rendering_3d_delivery.py
```

Expected: 全部通过，测试期间没有真实网络调用。

### Task 2: 启用联调 Fixture 并验证下游链路

**Files:**
- Modify: `.env`（未跟踪本机配置）
- Modify: `docs/media-agent-service-websocket.md`
- Test: `tests/tdd/agent_service_image_delivery/test_agent_service_image_delivery.py`
- Test: `tests/tdd/rendering-3d-delivery/test_rendering_3d_delivery.py`

**Interfaces:**
- Consumes: Task 1 的 `IMAGE_GENERATION_FIXTURE_ID` 和本地 adapter。
- Produces: 固定 `output_ref=/artifacts/generated/349cc6c272f4ec7a88800f0f.png`，供媒体投递和 `image_to_3d(src_image="349cc6c272f4ec7a88800f0f")` 共用。

- [ ] **Step 1: 启用本机 fixture**

在 `.env` 增加：

```text
IMAGE_GENERATION_FIXTURE_ID=349cc6c272f4ec7a88800f0f.png
```

- [ ] **Step 2: 更新权威文档**

在 `docs/media-agent-service-websocket.md` 记录显式 fixture 的启停方法、fail-closed 行为，以及媒体和 3D 共用 `.local/generated` 的事实。不得把该临时配置描述为生产默认值。

- [ ] **Step 3: 验证媒体与 3D 共用本地图片**

运行：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/rendering-3d-delivery/test_rendering_3d_delivery.py \
  tests/tdd/agent_service_image_delivery/test_agent_service_image_delivery.py
```

Expected: fixture output ref、媒体 `IMAGE.image` Base64、`src_image` 本地解析和 3D POST 全部通过。

- [ ] **Step 4: 静态与文档验证**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m ruff check \
  src/assistant_agent/tools/plugins/builtin/image_generation \
  tests/tdd/rendering-3d-delivery/test_rendering_3d_delivery.py
git diff --check
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  .codex/skills/assistant-agent-documentation-sync/scripts/collect_documentation_evidence.py \
  --repo-root .
```

Expected: Ruff 和 diff 检查成功，文档无新增失效链接。

- [ ] **Step 5: 运行交接**

报告当前运行中的 `scripts/run_server.py` 仍需重启才能加载 fixture 配置；未经用户授权不主动中断真实通话服务。

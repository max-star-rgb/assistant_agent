# 局域网生图访问地址与最近图片复用实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 由 Agent Server 为受管生图产物生成可信的局域网绝对 URL，并保证图片转 3D 默认复用 runtime-owned 最近图片。

**Architecture:** 内部继续使用 `/artifacts/generated/<filename>` 和 `image_id`，新增独立 `ARTIFACT_BASE_URL` 仅用于交付投影。Runtime 在模型完成后确定性附加结构化 `artifact_urls` 和文本链接；Tool observation 不再把 artifact 路径交给 LLM。`image_to_3d` 将 runtime 绑定的最近图片置于模型参数之前。

**Tech Stack:** Python 3.11、dataclass 配置、Pydantic response model、FastAPI StaticFiles、pytest。

## Global Constraints

- 默认使用 `/home/lenovo1/miniconda3/envs/hello_agent/bin/python`。
- pytest 固定使用 `MULTIMODAL_AGENT_PROVIDER_MODE=mock`，不调用真实图片 Provider 或 3D 服务。
- 不修改 Media-Agent wire schema，不让 `run_client.py` 保存图片副本。
- 不复用 `PUBLIC_IP` / `PUBLIC_PORT`，不根据请求 `Host`、普通 metadata 或转发头推导 artifact origin。
- 内部 `ToolResult.output_ref` 与 Gateway `output_refs` 保持受管相对引用，避免 3D 和 Base64 投递依赖网络。
- 不修改 `tests/core`；Core invariant 保持 unchanged。临时 RED/GREEN 只放在 `tests/tdd/lan-generated-artifacts/`。
- 不提交 `docs/superpowers/specs/**` 或 `docs/superpowers/plans/**`；它们仅作为本地开发材料。

---

## 文件结构

- 修改 `src/assistant_agent/config/__init__.py`：读取可信 `ARTIFACT_BASE_URL`。
- 修改 `src/assistant_agent/runtime/generated_artifacts.py`：校验受管引用、构造局域网绝对 URL、为 `AgentResponse` 添加交付投影。
- 修改 `src/assistant_agent/runtime/runtime.py`：在 graph 完成、终态记录和交付之前应用 artifact URL 投影。
- 修改 `src/assistant_agent/tools/plugins/builtin/image_generation/tool.py`：模型 observation 只保留 `image_id` 和成功/失败摘要。
- 修改 `src/assistant_agent/tools/plugins/builtin/image_to_3d/tool.py`：runtime-owned 最近图片优先于模型参数。
- 新建 `tests/tdd/lan-generated-artifacts/test_lan_generated_artifacts.py`：独立 offline RED/GREEN 行为测试。
- 修改 `.env.example`：记录 `ARTIFACT_BASE_URL`。
- 修改 `docs/media-agent-service-websocket.md`：同步局域网 URL、内部引用和最近图片优先级。

### Task 1: 可信局域网 artifact URL 与 Runtime 交付投影

**Files:**
- Create: `tests/tdd/lan-generated-artifacts/test_lan_generated_artifacts.py`
- Modify: `src/assistant_agent/config/__init__.py`
- Modify: `src/assistant_agent/runtime/generated_artifacts.py`
- Modify: `src/assistant_agent/runtime/runtime.py`

**Interfaces:**
- Produces: `ProviderConfig.artifact_base_url: str | None`
- Produces: `generated_artifact_public_url(output_ref: str, *, base_url: str | None) -> str | None`
- Produces: `with_generated_artifact_delivery(response: AgentResponse, *, base_url: str | None) -> AgentResponse`
- Consumes: existing `AgentResponse.output_refs` and `/artifacts/generated/<filename>` validation boundary.

- [ ] **Step 1: 写 URL 与响应投影的失败测试**

```python
from assistant_agent.config import ProviderConfig
from assistant_agent.runtime.generated_artifacts import (
    generated_artifact_public_url,
    with_generated_artifact_delivery,
)
from assistant_agent.runtime.requests import AgentResponse


def test_config_reads_trusted_lan_artifact_base_url() -> None:
    config = ProviderConfig.from_env(
        {"ARTIFACT_BASE_URL": "http://192.168.1.20:8089/"}
    )
    assert config.artifact_base_url == "http://192.168.1.20:8089/"


def test_generated_artifact_public_url_uses_only_managed_ref() -> None:
    base_url = "http://192.168.1.20:8089/"
    assert generated_artifact_public_url(
        "/artifacts/generated/image-sentinel.png",
        base_url=base_url,
    ) == "http://192.168.1.20:8089/artifacts/generated/image-sentinel.png"
    assert generated_artifact_public_url(
        "https://provider.example/image.png",
        base_url=base_url,
    ) is None
    assert generated_artifact_public_url(
        "/artifacts/generated/image-sentinel.png",
        base_url=None,
    ) is None


def test_response_delivery_keeps_internal_ref_and_adds_public_url() -> None:
    response = AgentResponse(
        message="image-ready-sentinel",
        output_refs=["/artifacts/generated/image-sentinel.png"],
    )
    delivered = with_generated_artifact_delivery(
        response,
        base_url="http://192.168.1.20:8089",
    )
    assert delivered.output_refs == response.output_refs
    assert delivered.data is not None
    assert delivered.data["artifact_urls"] == [
        "http://192.168.1.20:8089/artifacts/generated/image-sentinel.png"
    ]
    assert delivered.data["artifact_urls"][0] in delivered.message
```

- [ ] **Step 2: 运行测试并确认 RED**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/lan-generated-artifacts/test_lan_generated_artifacts.py
```

Expected: collection/import FAIL，因为两个 helper 和 `ProviderConfig.artifact_base_url` 尚不存在。

- [ ] **Step 3: 实现最小可信 URL helper 与响应投影**

在 `ProviderConfig` 增加：

```python
artifact_base_url: str | None = None
```

在 `ProviderConfig.from_env()` 构造参数中增加：

```python
artifact_base_url=source.get("ARTIFACT_BASE_URL"),
```

在 `generated_artifacts.py` 增加：

```python
def generated_artifact_public_url(
    output_ref: str,
    *,
    base_url: str | None,
) -> str | None:
    if not base_url:
        return None
    parsed_ref = urlparse(output_ref)
    prefix = GENERATED_ARTIFACT_PUBLIC_PREFIX.rstrip("/") + "/"
    filename = parsed_ref.path.removeprefix(prefix)
    if (
        parsed_ref.scheme
        or parsed_ref.netloc
        or parsed_ref.query
        or parsed_ref.fragment
        or not parsed_ref.path.startswith(prefix)
        or not filename
        or Path(filename).name != filename
    ):
        return None
    parsed_base = urlparse(base_url.strip())
    if (
        parsed_base.scheme not in {"http", "https"}
        or not parsed_base.netloc
        or parsed_base.username is not None
        or parsed_base.password is not None
        or parsed_base.query
        or parsed_base.fragment
        or parsed_base.path not in {"", "/"}
    ):
        return None
    return f"{base_url.strip().rstrip('/')}{parsed_ref.path}"
```

随后实现 `with_generated_artifact_delivery()`：去重并保留最多 4 个有效 URL；保持 `output_refs` 不变；把 URL 写入复制后的 `data["artifact_urls"]`。若 `message` 尚未包含这些 URL，使用 `f"{message.rstrip()}\n\n图片链接：\n" + "\n".join(urls)` 追加确定性段落；无有效 URL 时原样返回 response。

- [ ] **Step 4: 在 Runtime 终态前应用投影**

在 `AgentGraphRuntime.run_state()` graph 完成且 `state.response` 已建立后、`run_history.record_end()` 与 terminal trace/delivery 之前执行：

```python
if state.response is not None:
    state.response = with_generated_artifact_delivery(
        state.response,
        base_url=self.config.artifact_base_url,
    )
```

该位置确保 HTTP、Gateway、Agent-Service 和 CLI 共享相同响应，同时 `output_refs` 仍可被 Agent-Service 读取并投影为 Base64 `IMAGE detail`。

- [ ] **Step 5: 运行 Task 1 测试并确认 GREEN**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/lan-generated-artifacts/test_lan_generated_artifacts.py
```

Expected: PASS，且无网络调用。

- [ ] **Step 6: 暂存本 Task 代码但不提交开发材料**

```bash
git add \
  src/assistant_agent/config/__init__.py \
  src/assistant_agent/runtime/generated_artifacts.py \
  src/assistant_agent/runtime/runtime.py \
  tests/tdd/lan-generated-artifacts/test_lan_generated_artifacts.py
```

不要暂存 `docs/superpowers/**`，并等待所有 Task 完成后统一判断是否提交。

### Task 2: 收紧模型 observation 并优先复用最近图片

**Files:**
- Modify: `tests/tdd/lan-generated-artifacts/test_lan_generated_artifacts.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/image_generation/tool.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/image_to_3d/tool.py`

**Interfaces:**
- Consumes: `ToolContext.metadata["latest_generated_image_id"]: str | None`，由 `ToolExecutor` 从同 run 或当前 Agent-Service 连接绑定。
- Produces: `image_generation` 的 `model_observation` 不含 `images`/路径，只含 `image_id`、错误或无图片时的摘要。
- Produces: `image_to_3d` 源图片顺序为 runtime-owned 最近 ID，再回退到 `ImageTo3DRequest.src_image`。

- [ ] **Step 1: 写 observation 与最近图片优先级失败测试**

追加：

```python
from assistant_agent.media.image_to_3d import ImageTo3DSubmission
from assistant_agent.tools.base import ToolContext
from assistant_agent.tools.plugins.builtin.image_generation.tool import (
    _image_generation_model_observation,
)
from assistant_agent.tools.plugins.builtin.image_to_3d.tool import ImageTo3DTool


def test_image_generation_observation_does_not_expose_artifact_path() -> None:
    observation = _image_generation_model_observation(
        {
            "status": "succeeded",
            "image_urls": ["/artifacts/generated/image-sentinel.png"],
            "image_id": ["image-sentinel"],
        }
    )
    assert observation["image_id"] == ["image-sentinel"]
    assert "images" not in observation


def test_image_to_3d_prefers_runtime_owned_latest_image() -> None:
    seen: list[str] = []

    class Adapter:
        def start(self, *, user_id, session_id, src_image, output_format):
            seen.append(src_image)
            return ImageTo3DSubmission(
                job_id="job-sentinel",
                status="generating",
                source_image_id=src_image,
            )

    result = ImageTo3DTool(adapter=Adapter()).run(
        {"src_image": "model-invented-id"},
        ToolContext(
            user_id="user-sentinel",
            session_id="session-sentinel",
            metadata={"latest_generated_image_id": "latest-image-sentinel"},
        ),
    )
    assert result.success is True
    assert seen == ["latest-image-sentinel"]
    assert result.data["source_image_id"] == "latest-image-sentinel"
```

- [ ] **Step 2: 运行两个新测试并确认 RED**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/lan-generated-artifacts/test_lan_generated_artifacts.py \
  -k 'observation_does_not_expose or prefers_runtime_owned'
```

Expected: observation 仍含 `images`，且 adapter 收到 `model-invented-id`。

- [ ] **Step 3: 实现最小 observation 边界**

把 `_image_generation_model_observation()` 改为只投影：

```python
observation = {
    "image_id": data.get("image_id"),
    "errors": data.get("errors"),
}
if not data.get("image_id"):
    observation["summary"] = _image_generation_summary(data)
```

完整 `ToolResult.data`、contract 和 `output_ref` 保持不变，Runtime/Gateway 仍能投递本地产物。

- [ ] **Step 4: 实现 runtime-owned 最近图片优先级**

在 `ImageTo3DTool._run()` 中将：

```python
src_image = input.src_image or context.metadata.get("latest_generated_image_id")
```

替换为先规范化可信 metadata，再回退模型输入：

```python
latest_image_id = context.metadata.get("latest_generated_image_id")
src_image = (
    latest_image_id.strip()
    if isinstance(latest_image_id, str) and latest_image_id.strip()
    else input.src_image
)
```

- [ ] **Step 5: 运行完整 feature 测试并确认 GREEN**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/lan-generated-artifacts
```

Expected: PASS。

- [ ] **Step 6: 暂存本 Task 代码**

```bash
git add \
  src/assistant_agent/tools/plugins/builtin/image_generation/tool.py \
  src/assistant_agent/tools/plugins/builtin/image_to_3d/tool.py \
  tests/tdd/lan-generated-artifacts/test_lan_generated_artifacts.py
```

### Task 3: 部署文档、专项回归与提交判断

**Files:**
- Modify: `.env.example`
- Modify: `docs/media-agent-service-websocket.md`
- Verify: `tests/tdd/agent_service_image_delivery/test_agent_service_image_delivery.py`
- Verify: `tests/tdd/image-3d-entry-delivery/test_image_to_3d_completion.py`

**Interfaces:**
- Documents: `ARTIFACT_BASE_URL=http://<LAN_AGENT_IP>:8089`
- Preserves: Agent-Service `IMAGE detail` Base64、`output_refs` 相对引用、3D callback/job 契约。

- [ ] **Step 1: 更新配置示例**

在 `.env.example` 的图片生成配置附近加入：

```dotenv
# Optional trusted LAN origin used to publish managed generated-image links.
ARTIFACT_BASE_URL=http://<LAN_AGENT_IP>:8089
```

- [ ] **Step 2: 更新当前权威文档**

在 `docs/media-agent-service-websocket.md` 的图片投递与 image-to-3D 小节明确：

- `.local/generated` 是唯一受管原文件；
- `ARTIFACT_BASE_URL` 只决定局域网绝对 URL 投影；
- 未配置时不伪造绝对 URL，但 Base64 `IMAGE detail` 和内部 3D 复用不受影响；
- 主 LLM 不接收内部路径；
- 同 run/连接最近图片优先于模型 `src_image`，模型输入仅作为没有 runtime binding 时的 fallback。

- [ ] **Step 3: 运行专项最小回归**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/lan-generated-artifacts \
  tests/tdd/agent_service_image_delivery/test_agent_service_image_delivery.py \
  tests/tdd/image-3d-entry-delivery/test_image_to_3d_completion.py
```

Expected: PASS；测试只使用本地 fixture/mock，不产生真实 Provider 或 3D 请求。

- [ ] **Step 4: 运行静态差异检查**

Run:

```bash
git diff --check -- \
  .env.example \
  docs/media-agent-service-websocket.md \
  src/assistant_agent/config/__init__.py \
  src/assistant_agent/runtime/generated_artifacts.py \
  src/assistant_agent/runtime/runtime.py \
  src/assistant_agent/tools/plugins/builtin/image_generation/tool.py \
  src/assistant_agent/tools/plugins/builtin/image_to_3d/tool.py \
  tests/tdd/lan-generated-artifacts/test_lan_generated_artifacts.py
```

Expected: 无输出，退出码 0。

- [ ] **Step 5: 检查 scope 并提交本任务生产变更**

先执行：

```bash
git status --short
git diff --cached --stat
```

只暂存本任务代码、临时 TDD、当前权威文档和 `.env.example`；不要暂存用户已有的其他 untracked/modified 文件，也不要暂存 `docs/superpowers/**`。若 staged scope 正确，则提交：

```bash
git add .env.example docs/media-agent-service-websocket.md
git commit -m "fix: publish managed image links on LAN"
```

- [ ] **Step 6: 最终汇报**

按仓库格式报告：

```text
Core invariant: unchanged.
Tests: added tests/tdd/lan-generated-artifacts for temporary RED/GREEN; user may delete the directory manually.
```

同时列出实际测试命令、提交哈希、`ARTIFACT_BASE_URL` 部署示例，以及未调用真实 Provider/3D 服务。

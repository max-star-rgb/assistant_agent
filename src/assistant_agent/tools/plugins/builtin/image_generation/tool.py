"""Image generation Tool backed by a Plugin-private adapter."""

import asyncio
import shutil
from functools import partial
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal

from langchain_core.tools import BaseTool, ToolException, tool
from langgraph.prebuilt import ToolRuntime
from pydantic import Field, create_model

from assistant_agent.native_agent.context import (
    AssistantRunContext,
    authenticated_user_identity,
)
from assistant_agent.tools.plugins.builtin.image_generation.models import (
    GeneratedImageArtifact,
    ImageGenerationRequest,
    ImageGenerationResult,
)
from assistant_agent.tools.capability_output import (
    CapabilityOutputContract,
    build_text_capability_output,
)
from assistant_agent.tools.plugins.builtin.image_generation.backend import (
    ImageGenerationAdapter,
    MockImageGenerationAdapter,
    materialize_image_generation_result as _materialize_image_generation_result,
)
from assistant_agent.media.generated_artifacts import (
    MAX_DELIVERED_IMAGE_COUNT,
    generated_artifact_prefix,
    generated_artifact_payload,
)
from assistant_agent.tools.ids import (
    IMAGE_GENERATION_CAPABILITY,
    IMAGE_GENERATION_TOOL_NAME,
)
from assistant_agent.tools.native_boundary import (
    configure_builtin_tool,
    native_content_and_artifact,
    native_tool_exception,
)
from assistant_agent.runtime.thread_resources import ThreadResourceManager
from assistant_agent.tools.delivery import with_tool_delivery
from assistant_agent.tools.image_history import latest_image_ref
from assistant_agent.media.generated_artifacts import (
    generated_artifact_payload_for_thread,
    generated_artifact_attachment_for_thread,
)


def create_image_generation_tool(
    adapter: ImageGenerationAdapter | None = None,
    *,
    thread_resource_manager: ThreadResourceManager,
    editing_mock_file: str | None = None,
    editing_adapter: ImageGenerationAdapter | None = None,
) -> BaseTool:
    """Create the native image generation Tool."""

    image_adapter = adapter or MockImageGenerationAdapter()
    editing_adapter = editing_adapter or image_adapter
    if isinstance(image_adapter, MockImageGenerationAdapter) and editing_mock_file:
        from assistant_agent.tools.plugins.builtin.image_editing.tool import MockImageEditingAdapter
        editing_adapter = MockImageEditingAdapter(editing_mock_file)

    @tool(IMAGE_GENERATION_TOOL_NAME, response_format="content_and_artifact")
    async def image_generation(
        prompt: Annotated[
            str,
            Field(
                min_length=1,
                description="图片内容、构图、风格和关键视觉要求。",
            ),
        ],
        runtime: ToolRuntime[AssistantRunContext],
        operation: Annotated[
            Literal["generate", "edit"],
            Field(description="generate 按文字生成新图片；edit 修改本线程最近成功生成或编辑的图片。"),
        ] = "generate",
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """根据文本要求生成图片，或编辑本线程最近成功生成或编辑的图片。

        返回生成状态，图片由系统自动交付并供后续编辑使用。会调用图片生成服务；
        编辑时设置 operation=edit，描述修改要求；源图由系统读取，不需要提供图片地址。
        不用于理解或检索图片。
        """

        try:
            if operation not in {"generate", "edit"}:
                raise ValueError("unsupported image operation")
            if operation == "edit":
                if isinstance(image_adapter, MockImageGenerationAdapter):
                    if not editing_mock_file:
                        raise ValueError("mock image editing requires IMAGE_EDITING_MOCK_FILE")
                elif not getattr(editing_adapter, "supports_editing", False):
                    raise ValueError("configured image provider/model does not support editing")
            return await execute_image_output(
                editing_adapter if operation == "edit" else image_adapter, prompt, runtime,
                thread_resource_manager=thread_resource_manager,
                capability="image_editing" if operation == "edit" else IMAGE_GENERATION_CAPABILITY,
                edit_source=operation == "edit",
            )
        except ToolException:
            raise
        except Exception as exc:
            raise native_tool_exception(
                exc, tool_name=IMAGE_GENERATION_TOOL_NAME
            ) from exc

    if isinstance(image_adapter, MockImageGenerationAdapter):
        image_generation.args_schema = create_model(
            "MockImageGenerationInput",
            __base__=image_generation.args_schema,
            prompt=(
                Literal["生成一个白色的台灯", "编辑这个白色的台灯"],
                Field(description="mock 固定参数：generate 用‘生成一个白色的台灯’，edit 用‘编辑这个白色的台灯’。"),
            ),
        )
        image_generation.description = (
            "mock 图像生成：调用时 prompt 必须原样填写‘生成一个白色的台灯’，"
            "不要扩写或改成用户的其他图片描述。返回固定示例图片和‘已生成一个白色台灯’。"
            "编辑时设置 operation=edit，prompt 填写‘编辑这个白色的台灯’，返回固定编辑样例。"
            "用户需要 3D 时调用 image_to_3d，自动使用最近图片。"
        )

    return configure_builtin_tool(
        image_generation,
        bounded_expected_errors=True,
    )


async def execute_image_output(
    adapter: ImageGenerationAdapter,
    prompt: str,
    runtime: ToolRuntime[AssistantRunContext],
    *,
    thread_resource_manager: ThreadResourceManager,
    capability: str = IMAGE_GENERATION_CAPABILITY,
    edit_source: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Materialize and deliver an image result for generation or editing."""

    resources = await asyncio.to_thread(
        thread_resource_manager.resolve,
        authenticated_user_identity(runtime),
        str(runtime.execution_info.thread_id or ""),
    )
    result = await asyncio.to_thread(
        _execute_image_generation_from_runtime, adapter, prompt, runtime,
        thread_resource_manager=thread_resource_manager,
        edit_source=edit_source,
    )
    if result.status == "failed":
        raise RuntimeError(result.error or f"{capability}_failed")
    artifact = _image_generation_output_contract(
        result, public_prefix=generated_artifact_prefix(resources.thread_ref),
        capability=capability,
    )
    if runtime.context.device_id is not None:
        from assistant_agent.runtime.device_backend import device_backend_for_runtime
        backend = device_backend_for_runtime(runtime)
        for image in artifact.get("images", []):
            filename = PurePosixPath(image["output_ref"]).name
            payload = await asyncio.to_thread((resources.artifact_root / "generated" / filename).read_bytes)
            delivered = await backend.upload(str(runtime.context.cwd / "outputs" / filename), payload)
            if delivered.error:
                raise RuntimeError(f"device_media_delivery_failed: {delivered.error}")
            image["local_path"] = delivered.path
    output_refs = [
        image["output_ref"]
        for image in artifact.get("images", [])
        if isinstance(image, Mapping) and image.get("output_ref")
    ]
    if output_refs:
        artifact = with_tool_delivery(artifact, output_refs=output_refs)
    return await asyncio.to_thread(
        native_content_and_artifact,
        _image_generation_model_observation(artifact), artifact,
        artifact_resolver=partial(
            generated_artifact_attachment_for_thread,
            manager=thread_resource_manager,
            identity=authenticated_user_identity(runtime),
            thread_id=str(runtime.execution_info.thread_id or ""),
        ),
    )


def _execute_image_generation_from_runtime(
    adapter: ImageGenerationAdapter,
    prompt: str,
    runtime: ToolRuntime[AssistantRunContext],
    *,
    thread_resource_manager: ThreadResourceManager,
    edit_source: bool = False,
) -> ImageGenerationResult:
    state = runtime.state if isinstance(runtime.state, Mapping) else {}
    source_image = None
    if edit_source:
        ref = latest_image_ref(state, tool_names=("image_generation", "image_editing"))
        if ref is None:
            raise ValueError("image editing requires an existing image; generate an image first")
        source = generated_artifact_payload_for_thread(
            ref, thread_resource_manager,
            identity=authenticated_user_identity(runtime),
            thread_id=str(runtime.execution_info.thread_id or ""),
        )
        if source is None:
            raise ValueError("image editing requires an existing image in the current thread")
        # Qwen accepts at most 10 MiB per reference image.
        padding = len(source.base64_data) - len(source.base64_data.rstrip("="))
        if len(source.base64_data) * 3 // 4 - padding > 10 * 1024 * 1024:
            raise ValueError("image editing source exceeds the 10 MiB limit")
        source_image = f"data:{source.media_type};base64,{source.base64_data}"
    request = ImageGenerationRequest(
        prompt=prompt,
        source_image=source_image,
        user_id=authenticated_user_identity(runtime),
        session_id=runtime.execution_info.thread_id,
        memory_context=list(state.get("memory_context", ())),
    )
    resources = thread_resource_manager.resolve(
        request.user_id,
        str(request.session_id or ""),
    )
    public_prefix = generated_artifact_prefix(resources.thread_ref)
    return _execute_image_generation(
        adapter,
        request,
        artifact_dir=resources.artifact_root / "generated",
        output_dir=None if runtime.context.device_id is not None else Path(runtime.context.cwd) / "outputs",
        public_prefix=public_prefix,
    )


def _execute_image_generation(
    adapter: ImageGenerationAdapter,
    input: ImageGenerationRequest,
    *,
    artifact_dir: Path,
    output_dir: Path | None,
    public_prefix: str,
) -> ImageGenerationResult:
    result = adapter.generate(input)
    if result.status != "succeeded":
        return result
    if isinstance(adapter, MockImageGenerationAdapter):
        result = adapter.materialize(
            result, artifact_dir=artifact_dir, public_prefix=public_prefix,
        )
    result = _publish_image_ids(
        _materialize_image_generation_result(
            result,
            artifact_dir=artifact_dir,
            public_prefix=public_prefix,
        ),
        artifact_dir=artifact_dir,
        public_prefix=public_prefix,
    )
    images = _generated_image_artifacts(result, public_prefix=public_prefix)
    if images and output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        for image in images:
            filename = PurePosixPath(image.output_ref).name
            shutil.copy2(artifact_dir / filename, output_dir / filename)
    return result


def _image_generation_output_contract(
    result: ImageGenerationResult,
    *,
    public_prefix: str,
    capability: str = IMAGE_GENERATION_CAPABILITY,
) -> dict[str, Any]:
    data = {
        "task_id": result.task_id,
        "image_id": result.image_id,
        "request_id": result.request_id,
        "prompt": result.prompt,
        "prompt_used": result.prompt_used or result.prompt,
        "provider": result.provider,
        "model": result.model,
        "images": [
            image.model_dump(exclude_none=True)
            for image in _generated_image_artifacts(
                result,
                public_prefix=public_prefix,
            )
        ],
    }
    if result.provider == "mock" and result.model == "mock-image-generation" and result.status == "succeeded":
        data["summary"] = "已生成一个白色台灯"
    if result.provider == "mock" and result.model == "mock-image-editing" and result.status == "succeeded":
        data["summary"] = "已编辑这个白色台灯"
    if capability == "image_editing" and result.provider != "mock" and result.status == "succeeded":
        data["summary"] = "图片编辑成功。"
    public = build_text_capability_output(
        capability=capability,
        status=result.status,
        output_ref=result.output_ref or result.image_url,
        data=data,
        errors=result.errors,
    )
    payload = {
        **data,
        "status": result.status,
        "errors": result.errors,
        "contract": public,
    }
    CapabilityOutputContract.model_validate(public)
    return payload


def _generated_image_artifacts(
    result: ImageGenerationResult,
    *,
    public_prefix: str,
) -> list[GeneratedImageArtifact]:
    refs = result.download_urls or (
        [result.download_url] if result.download_url else []
    )
    if not refs and result.output_ref:
        refs = [result.output_ref]
    images: list[GeneratedImageArtifact] = []
    seen: set[str] = set()
    for ref in refs:
        if (
            not _is_managed_generated_ref(ref, public_prefix=public_prefix)
            or ref in seen
        ):
            continue
        path = PurePosixPath(ref)
        images.append(
            GeneratedImageArtifact(
                image_id=path.stem,
                output_ref=ref,
                url=ref,
                mime_type=_image_mime_type(path.suffix),
            )
        )
        seen.add(ref)
        if len(images) >= MAX_DELIVERED_IMAGE_COUNT:
            break
    return images


def _is_managed_generated_ref(value: Any, *, public_prefix: str) -> bool:
    if not isinstance(value, str):
        return False
    prefix = public_prefix.rstrip("/") + "/"
    if not value.startswith(prefix):
        return False
    filename = value.removeprefix(prefix)
    return bool(filename) and PurePosixPath(filename).name == filename


def _image_mime_type(suffix: str) -> str:
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }.get(suffix.lower(), "image/png")


def _image_generation_model_observation(data: dict[str, Any]) -> dict[str, Any]:
    observation: dict[str, Any] = {
        "status": data.get("status"),
        "summary": data.get("summary") or _image_generation_summary(data),
        "image_count": len(data.get("images", [])),
        "errors": data.get("errors"),
    }
    return {
        key: value
        for key, value in observation.items()
        if value not in (None, "", [], {})
    }


def _image_generation_summary(data: dict[str, Any]) -> str:
    if data.get("status") == "succeeded":
        return "Image generation succeeded."
    errors = data.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        return str(errors[0].get("message") or "Image generation failed.")
    return "Image generation failed."


def _publish_image_ids(
    result: ImageGenerationResult,
    *,
    artifact_dir: Path,
    public_prefix: str,
) -> ImageGenerationResult:
    refs = result.download_urls or (
        [result.download_url] if result.download_url else []
    )
    if not refs and result.output_ref:
        refs = [result.output_ref]
    image_ids: list[str] = list(result.image_id)
    for ref in refs:
        payload = generated_artifact_payload(
            ref,
            artifact_dir=artifact_dir,
            public_prefix=public_prefix,
        )
        if payload is None:
            continue
        image_id = payload.image_id.rsplit(".", 1)[0]
        if not image_id:
            continue
        image_ids.append(image_id)
    return result.model_copy(update={"image_id": list(dict.fromkeys(image_ids))})

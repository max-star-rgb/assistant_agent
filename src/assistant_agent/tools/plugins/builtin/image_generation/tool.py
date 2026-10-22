"""Image generation Tool backed by a Plugin-private adapter."""

import asyncio
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Annotated, Any

from langchain_core.tools import BaseTool, tool
from langgraph.prebuilt import ToolRuntime
from pydantic import Field

from assistant_agent.native_agent.context import (
    AssistantRunContext,
    authenticated_user_identity,
    default_working_directory,
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
    mock_image_source,
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
)
from assistant_agent.runtime.thread_resources import GeneratedArtifactStore, artifact_session_scope
from assistant_agent.tools.delivery import with_tool_delivery


def create_image_generation_tool(
    adapter: ImageGenerationAdapter | None = None,
    *,
    artifact_store: GeneratedArtifactStore,
) -> BaseTool:
    """Create the native image generation Tool."""

    image_adapter = adapter or MockImageGenerationAdapter()

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
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """根据文本中的内容、构图和风格要求生成图片。

        返回可供后续展示或处理的`image_id`和`url`及生成结果。会调用图片生成服务；
        不用于理解、检索或修改现有图片。
        """

        try:
            identity = authenticated_user_identity(runtime)
            thread_ref = artifact_store.thread_ref(identity, str(runtime.execution_info.thread_id or ""))
            result = await asyncio.to_thread(
                _execute_image_generation_from_runtime, image_adapter,
                prompt,
                runtime,
                artifact_store=artifact_store,
            )
            if result.status == "failed":
                raise RuntimeError(result.error or "image_generation_failed")
            artifact = _image_generation_output_contract(
                result,
                public_prefix=generated_artifact_prefix(thread_ref),
            )
            if runtime.context.device_id is not None:
                from assistant_agent.runtime.device_backend import device_backend_for_runtime
                for image in artifact.get("images", []):
                    try:
                        backend = device_backend_for_runtime(runtime)
                        filename = PurePosixPath(image["output_ref"]).name
                        path = await asyncio.to_thread(artifact_store.resolve, thread_ref, filename, identity=identity)
                        payload = await asyncio.to_thread(path.read_bytes)
                        delivered = await backend.upload(str(runtime.context.cwd / "outputs" / filename), payload)
                        if delivered.error:
                            raise RuntimeError("device_media_delivery_failed")
                        image["local_path"] = delivered.path
                    except Exception:
                        # Generation has succeeded; retain the server artifact for delivery.
                        artifact["errors"].append({"code": "device_media_delivery_failed",
                                                  "message": "图片已生成，设备交付失败；不要重新生成。"})
                artifact["contract"]["errors"] = artifact["errors"]
            output_refs = [
                image["output_ref"]
                for image in artifact.get("images", [])
                if isinstance(image, Mapping) and image.get("output_ref")
            ]
            if output_refs:
                artifact = with_tool_delivery(artifact, output_refs=output_refs)
            content, artifact = native_content_and_artifact(
                _image_generation_model_observation(artifact), artifact
            )
            return content, artifact
        except Exception as exc:
            # A paid operation with an unknown outcome must not become a recoverable
            # ToolMessage that invites the model to invoke generation again.
            raise RuntimeError("image_generation_execution_failed") from exc

    return configure_builtin_tool(
        image_generation,
        bounded_expected_errors=True,
    )


def _execute_image_generation_from_runtime(
    adapter: ImageGenerationAdapter,
    prompt: str,
    runtime: ToolRuntime[AssistantRunContext],
    *,
    artifact_store: GeneratedArtifactStore,
) -> ImageGenerationResult:
    state = runtime.state if isinstance(runtime.state, Mapping) else {}
    request = ImageGenerationRequest(
        prompt=prompt,
        user_id=authenticated_user_identity(runtime),
        session_id=runtime.execution_info.thread_id,
        memory_context=list(state.get("memory_context", ())),
    )
    thread_id = str(request.session_id or "")
    public_prefix = generated_artifact_prefix(artifact_store.thread_ref(request.user_id, thread_id))
    cwd = default_working_directory() if runtime.context.device_id else Path(runtime.context.cwd)
    output_dir = cwd / "outputs"
    result = _execute_image_generation(
        adapter,
        request,
        artifact_dir=output_dir,
        public_prefix=public_prefix,
    )
    for image in _generated_image_artifacts(result, public_prefix=public_prefix):
        filename = PurePosixPath(image.output_ref).name
        artifact_store.register(request.user_id, thread_id,
            mock_image_source(result) or output_dir / filename, filename=filename,
            session_scope=artifact_session_scope(getattr(runtime, "config", {})))
    return result


def _execute_image_generation(
    adapter: ImageGenerationAdapter,
    input: ImageGenerationRequest,
    *,
    artifact_dir: Path,
    public_prefix: str,
) -> ImageGenerationResult:
    if artifact_dir.resolve() != artifact_dir.absolute():
        raise ValueError("generated output directory must not contain symlinks")
    result = adapter.generate(input)
    if result.status != "succeeded":
        return result
    result = _publish_image_ids(
        _materialize_image_generation_result(
            result,
            artifact_dir=artifact_dir,
            public_prefix=public_prefix,
        ),
        artifact_dir=artifact_dir,
        public_prefix=public_prefix,
    )
    return result


def _image_generation_output_contract(
    result: ImageGenerationResult,
    *,
    public_prefix: str,
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
    public = build_text_capability_output(
        capability=IMAGE_GENERATION_CAPABILITY,
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
        "images": [
            {"image_id": image.get("image_id"), "url": image.get("url"),
             **({"local_path": image["local_path"]} if image.get("local_path") else {})}
            for image in data.get("images", [])
            if isinstance(image, Mapping) and image.get("image_id") and image.get("url")
        ],
        "errors": data.get("errors"),
    }
    if not observation["images"]:
        observation["summary"] = _image_generation_summary(data)
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
    if mock_image_source(result) is not None:
        return result
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

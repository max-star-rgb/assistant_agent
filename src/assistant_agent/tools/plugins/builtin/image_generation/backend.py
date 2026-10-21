"""Plugin-private image generation backend and output materialization."""

import hashlib
import mimetypes
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import unquote, urlparse

from assistant_agent.config import ImageGenerationConfig
from assistant_agent.media.generated_artifacts import MAX_ARTIFACT_BYTES
from assistant_agent.provider_mode import ProviderMode
from assistant_agent.providers.provider_errors import (
    ProviderAdapterError,
    build_provider_error,
)
from assistant_agent.tools.plugins.builtin.image_generation.prompting import (
    build_image_prompt,
)
from assistant_agent.tools.plugins.builtin.image_generation.models import (
    ImageGenerationRequest,
    ImageGenerationResult,
)
from assistant_agent.tools.ids import IMAGE_GENERATION_CAPABILITY


@dataclass(frozen=True)
class StoredArtifact:
    path: Path
    download_url: str
    source_url: str


class ImageGenerationAdapter(Protocol):
    """Adapter contract for image generation providers."""

    def generate(self, input: ImageGenerationRequest) -> ImageGenerationResult:
        """Generate an image and return structured task output."""


class MockImageGenerationAdapter:
    """Deterministic local image generation adapter."""

    provider = "mock"
    model = "mock-image-generation"

    def __init__(self, image_path: str | None = None) -> None:
        self.image_path = image_path

    def generate(self, input: ImageGenerationRequest) -> ImageGenerationResult:
        prompt = build_image_prompt(input)
        image_url = (
            Path(self.image_path).expanduser().resolve().as_uri()
            if self.image_path else "local://generated/poster.png"
        )
        return ImageGenerationResult(
            task_id="mock_image_task_1",
            status="succeeded",
            image_url=image_url,
            image_urls=[image_url],
            request_id="mock_image_request_1",
            prompt=prompt,
            provider=self.provider,
            model=self.model,
            output_ref=image_url,
            prompt_used=prompt,
        )


class UnconfiguredImageGenerationAdapter:
    """Adapter returned when a real image provider is selected without config."""

    def __init__(self, provider: str, missing: str) -> None:
        self.provider = provider
        self.missing = missing

    def generate(self, input: ImageGenerationRequest) -> ImageGenerationResult:
        prompt = input.prompt or input.style or "image generation request"
        error = build_provider_error(
            "provider_unconfigured",
            f"{self.provider} image provider is missing {self.missing}.",
            recoverable=True,
            provider=self.provider,
            capability=IMAGE_GENERATION_CAPABILITY,
        )
        return ImageGenerationResult(
            task_id=f"{self.provider}_image_unconfigured",
            status="failed",
            prompt=prompt,
            provider=self.provider,
            model=None,
            error=f"{error.code}: {error.message}",
            errors=[
                {
                    "code": error.code,
                    "message": error.message,
                    "recoverable": error.recoverable,
                }
            ],
        )


def materialize_image_generation_result(
    result: ImageGenerationResult,
    *,
    artifact_dir: Path,
    public_prefix: str,
    timeout_seconds: float = 120.0,
) -> ImageGenerationResult:
    """Download provider-hosted images and expose backend-owned download URLs."""

    image_urls = result.image_urls or ([result.image_url] if result.image_url else [])
    # Only the trusted mock adapter can supply a local fixture, never a real provider.
    if result.provider == "mock" and image_urls and urlparse(image_urls[0]).scheme == "file":
        source = Path(unquote(urlparse(image_urls[0]).path))
        try:
            with source.open("rb") as stream:
                payload = stream.read(MAX_ARTIFACT_BYTES + 1)
        except OSError as exc:
            raise ProviderAdapterError("mock_image_unavailable", "configured mock image cannot be read") from exc
        if len(payload) > MAX_ARTIFACT_BYTES:
            raise ProviderAdapterError("mock_image_unavailable", "mock image exceeded artifact size limit")
        name = hashlib.sha256(payload).hexdigest()[:24] + source.suffix.lower()
        ref = f"{public_prefix.rstrip('/')}/{name}"
        return result.model_copy(update={
            "image_url": ref, "image_urls": [ref], "output_ref": ref,
            "download_url": ref, "download_urls": [ref],
            "provider_image_urls": image_urls, "image_id": [name.rsplit(".", 1)[0]],
        })
    provider_urls = [url for url in image_urls if _is_remote_http_url(url)]
    if not provider_urls:
        return result

    artifacts = [
        store_remote_artifact(
            url,
            artifact_dir=artifact_dir,
            public_prefix=public_prefix,
            filename_seed=f"{result.request_id or result.task_id}-{index}",
            timeout_seconds=timeout_seconds,
        )
        for index, url in enumerate(provider_urls)
    ]
    download_urls = [artifact.download_url for artifact in artifacts]
    return result.model_copy(
        update={
            "provider_image_urls": image_urls,
            "download_url": download_urls[0] if download_urls else None,
            "download_urls": download_urls,
            "image_url": download_urls[0] if download_urls else result.image_url,
            "image_urls": download_urls or result.image_urls,
            "output_ref": download_urls[0] if download_urls else result.output_ref,
        }
    )


def store_remote_artifact(
    url: str,
    *,
    artifact_dir: Path,
    public_prefix: str,
    filename_seed: str,
    timeout_seconds: float = 120.0,
) -> StoredArtifact:
    """Download one remote artifact into local storage and return its public URL."""

    artifact_dir.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(
        url, headers={"User-Agent": "multimodal-agent-artifact-fetcher/1.0"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            content_type = response.headers.get("Content-Type", "")
            payload = response.read(MAX_ARTIFACT_BYTES + 1)
    except TimeoutError as exc:
        raise ProviderAdapterError(
            "provider_timeout", "generated image download timed out"
        ) from exc
    except urllib.error.HTTPError as exc:
        raise ProviderAdapterError(
            "provider_bad_response",
            f"generated image download failed: HTTP {exc.code}",
        ) from exc
    except urllib.error.URLError as exc:
        raise ProviderAdapterError(
            "provider_unavailable", f"generated image download failed: {exc.reason}"
        ) from exc

    if len(payload) > MAX_ARTIFACT_BYTES:
        raise ProviderAdapterError(
            "provider_bad_response",
            "generated image exceeded local artifact size limit",
        )
    extension = _extension_from_url_or_content_type(url, content_type)
    name = hashlib.sha256(f"{filename_seed}:{url}".encode("utf-8")).hexdigest()[:24]
    path = artifact_dir / f"{name}{extension}"
    _write_artifact(path, payload)
    return StoredArtifact(
        path=path,
        download_url=f"{public_prefix.rstrip('/')}/{path.name}",
        source_url=url,
    )


def mock_image_source(result: ImageGenerationResult) -> Path | None:
    """Return the trusted fixture backing a mock reference, without copying it."""
    if result.provider == "mock" and result.provider_image_urls:
        parsed = urlparse(result.provider_image_urls[0])
        if parsed.scheme == "file":
            return Path(unquote(parsed.path))
    return None


def _write_artifact(path: Path, payload: bytes) -> None:
    """Publish complete bytes without following an existing file symlink."""
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(payload)
            stream.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def _is_remote_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _extension_from_url_or_content_type(url: str, content_type: str) -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
        return suffix
    guessed = mimetypes.guess_extension(content_type.split(";", 1)[0].strip())
    if guessed in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
        return guessed
    return ".png"


def create_image_generation_adapter(
    config: ImageGenerationConfig,
    *,
    provider_mode: ProviderMode,
) -> ImageGenerationAdapter:
    """Create an image generation adapter without initializing real provider clients."""

    if provider_mode != "real" or config.image_generation_provider == "mock":
        return MockImageGenerationAdapter(config.image_generation_mock_path)
    provider = config.resolved_provider()
    missing = provider.missing_required_env()
    if missing:
        return UnconfiguredImageGenerationAdapter(provider.provider, ", ".join(missing))
    if provider.adapter_kind == "dashscope_image":
        from assistant_agent.tools.plugins.builtin.image_generation.qwen_adapter import (
            QwenImageGenerationAdapter,
            QwenImageGenerationConfig,
        )

        return QwenImageGenerationAdapter(
            QwenImageGenerationConfig(
                api_key=provider.api_key,
                base_url=provider.base_url or "",
                model=provider.model or "",
                default_size=config.qwen_image_default_size,
            )
        )
    if provider.adapter_kind == "ark_image":
        from assistant_agent.tools.plugins.builtin.image_generation.ark_adapter import (
            ArkImageGenerationAdapter,
            ArkImageGenerationConfig,
        )

        return ArkImageGenerationAdapter(
            ArkImageGenerationConfig(
                api_key=provider.api_key,
                base_url=provider.base_url or "",
                model=provider.model or "",
                default_size=config.ark_image_default_size,
                output_format=config.ark_image_output_format,
            )
        )
    if provider_mode == "real":
        raise ValueError(
            "real provider mode requires a configured image generation provider"
        )
    return MockImageGenerationAdapter()

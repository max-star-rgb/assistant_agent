import base64
import importlib.util
import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_completion_registry_module_exists() -> None:
    assert importlib.util.find_spec(
        "assistant_agent.runtime.image_to_3d_jobs"
    ) is not None


def test_submission_uses_independent_job_id_for_callback(tmp_path: Path) -> None:
    from assistant_agent.media.image_to_3d import ImageTo3DAdapter, ImageTo3DSettings
    from assistant_agent.runtime.image_to_3d_jobs import ImageTo3DJobRegistry

    image = tmp_path / "cake.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nimage-sentinel")
    requests: list[dict] = []

    def request_json(method: str, url: str, body: bytes | None, headers: dict[str, str]):
        requests.append(json.loads((body or b"{}").decode("utf-8")))
        return {}

    jobs = ImageTo3DJobRegistry()
    adapter = ImageTo3DAdapter(
        ImageTo3DSettings(
            td_gen_url="http://3d-service/img-to-3d",
            public_base_url="http://agent:8000",
            generated_artifact_path=tmp_path,
        ),
        request_json=request_json,
        job_registry=jobs,
    )

    result = adapter.start(
        user_id="user-sentinel",
        session_id="runtime-session-sentinel",
        src_image="cake",
    )

    assert result.job_id.startswith("image-to-3d-")
    assert requests == [
        {
            "sessionId": "runtime-session-sentinel",
            "image": base64.b64encode(image.read_bytes()).decode("ascii"),
            "pre_cb_url": (
                "http://agent:8000/calling-agent-service/v1/"
                f"{result.job_id}/0/3d-gen-back"
            ),
            "cb_url": (
                "http://agent:8000/calling-agent-service/v1/"
                f"{result.job_id}/0/3d-gen-back"
            ),
            "format": "mp4",
        }
    ]
    job = jobs.get(result.job_id)
    assert job is not None
    assert job.user_id == "user-sentinel"
    assert job.session_id == "runtime-session-sentinel"
    assert "delivery_sink_id" not in type(job).model_fields
    assert job.status == "generating"


def test_job_lookup_is_owner_bound() -> None:
    from assistant_agent.runtime.image_to_3d_jobs import ImageTo3DJobRegistry

    jobs = ImageTo3DJobRegistry()
    job = jobs.register(
        user_id="user-sentinel",
        session_id="session-sentinel",
        source_image_id="cake",
    )

    assert jobs.get_for_owner(
        job.job_id,
        user_id="user-sentinel",
        session_id="session-sentinel",
    ) == job
    assert jobs.get_for_owner(
        job.job_id,
        user_id="other-user",
        session_id="session-sentinel",
    ) is None


def test_http_client_can_query_only_its_completed_job() -> None:
    from assistant_agent.api.auth import get_auth_context
    from assistant_agent.api.identity import AuthContext
    from assistant_agent.api import routes_agent
    from assistant_agent.runtime.image_to_3d_jobs import (
        ImageTo3DArtifact,
        ImageTo3DJobRegistry,
    )

    jobs = ImageTo3DJobRegistry()
    job = jobs.register(
        user_id="user-sentinel",
        session_id="session-sentinel",
        source_image_id="cake",
    )
    jobs.complete(
        job.job_id,
        artifact=ImageTo3DArtifact(
            media_type="glb",
            media_url="http://3d-service/model.glb",
        ),
    )
    app = FastAPI()
    app.include_router(routes_agent.router)
    app.dependency_overrides[routes_agent.get_image_to_3d_job_registry] = lambda: jobs
    app.dependency_overrides[get_auth_context] = lambda: AuthContext(
        authenticated=True,
        source="test",
        user_id="user-sentinel",
        session_id="session-sentinel",
    )

    with TestClient(app) as client:
        response = client.get(
            f"/agent/image-to-3d/jobs/{job.job_id}",
            params={"user_id": "user-sentinel", "session_id": "session-sentinel"},
        )
        assert response.status_code == 200
        assert response.json()["artifact"] == {
            "media_type": "glb",
            "media_url": "http://3d-service/model.glb",
            "image": None,
        }

        app.dependency_overrides[get_auth_context] = lambda: AuthContext(
            authenticated=True,
            source="test",
            user_id="other-user",
            session_id="session-sentinel",
        )
        hidden = client.get(
            f"/agent/image-to-3d/jobs/{job.job_id}",
            params={"user_id": "other-user", "session_id": "session-sentinel"},
        )
        assert hidden.status_code == 404


def test_tool_does_not_consume_entry_delivery_metadata() -> None:
    import inspect

    from assistant_agent.media.image_to_3d import ImageTo3DSubmission
    from assistant_agent.tools.base import ToolContext
    from assistant_agent.tools.plugins.builtin.image_to_3d import tool as tool_module
    from assistant_agent.tools.plugins.builtin.image_to_3d.tool import ImageTo3DTool

    calls: list[dict] = []

    class Adapter:
        def start(
            self,
            *,
            user_id: str,
            session_id: str,
            src_image: str,
            output_format: str,
        ) -> ImageTo3DSubmission:
            calls.append(
                {
                    "user_id": user_id,
                    "session_id": session_id,
                    "src_image": src_image,
                    "output_format": output_format,
                }
            )
            return ImageTo3DSubmission(
                job_id="image-to-3d-sentinel",
                status="generating",
                source_image_id=src_image,
            )

    result = ImageTo3DTool(adapter=Adapter()).run(
        {"src_image": "cake"},
        ToolContext(
            user_id="user-sentinel",
            session_id="session-sentinel",
            metadata={
                "request_metadata": {
                    "gateway": {
                        "entry_capabilities": {
                            "supports_async_artifact_delivery": True,
                        },
                        "artifact_delivery": {
                            "mode": "push",
                            "sink_id": "must-not-reach-tool",
                        },
                    },
                }
            },
        ),
    )

    assert result.success is True
    assert result.data == {
        "job_id": "image-to-3d-sentinel",
        "status": "generating",
        "source_image_id": "cake",
    }
    assert set(calls[0]) == {
        "user_id",
        "session_id",
        "src_image",
        "output_format",
    }
    assert "agent_service" not in inspect.getsource(tool_module)
    assert "artifact_delivery" not in inspect.getsource(tool_module)

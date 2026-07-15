from datetime import datetime, timezone

from assistant_agent.agent.action_validator import ActionValidator
from assistant_agent.agent.state import AgentState
from assistant_agent.config import ProviderConfig
from assistant_agent.memory.remote import MemoryServerRequest, RemoteMemoryClient
from assistant_agent.schemas.assistant_decision import AssistantDecision
from assistant_agent.schemas.identity import RequestIdentity
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.services.context.tool_catalog import select_prompt_tool_specs
from assistant_agent.services.memory_media_ingestion import (
    MemoryMediaIngestionFile,
    MemoryMediaIngestionResult,
    MemoryMediaIngestionService,
    MemoryMediaTaskStatusResult,
    create_memory_media_ingestion_service,
)
from assistant_agent.tools.base import ToolContext
from assistant_agent.tools.memory_media_tool import MemoryIngestStatusTool, MemoryMediaIngestTool
from assistant_agent.tools.registry import create_default_registry


NOW = datetime(2026, 4, 11, 12, 0, tzinfo=timezone.utc)


def test_memory_media_ingestion_service_binds_identity_and_generates_file_ids() -> None:
    requests: list[MemoryServerRequest] = []

    def transport(request: MemoryServerRequest) -> dict:
        requests.append(request)
        return {
            "task_id": "20260411T120000Z-a1b2c3",
            "status": "processing",
            "accepted_count": 1,
            "code": 202,
        }

    service = MemoryMediaIngestionService(
        remote_client=RemoteMemoryClient(base_url="http://memory.local", transport=transport),
        file_id_factory=lambda identity, file, index: f"assistant-agent-{identity.user_id}-{identity.session_id}-{index}",
    )

    result = service.ingest(
        identity=RequestIdentity.for_user(user_id="trusted-user", session_id="trusted-session"),
        files=[
            MemoryMediaIngestionFile(
                file_url="file:///tmp/breakfast.mp4",
                filename="breakfast.mp4",
                media_type="video",
                start_time=NOW,
                metadata={"topic": "breakfast"},
            )
        ],
    )

    assert result == MemoryMediaIngestionResult(
        status="processing",
        task_id="20260411T120000Z-a1b2c3",
        accepted_count=1,
        file_ids=["assistant-agent-trusted-user-trusted-session-0"],
        output_ref="memory_server://tasks/20260411T120000Z-a1b2c3",
    )
    assert requests[0].body == {
        "user_id": "trusted-user",
        "session_id": "trusted-session",
        "files": [
            {
                "file_id": "assistant-agent-trusted-user-trusted-session-0",
                "file_url": "file:///tmp/breakfast.mp4",
                "filename": "breakfast.mp4",
                "media_type": "video",
                "start_time": "2026-04-11T12:00:00Z",
                "metadata": {"topic": "breakfast"},
            }
        ],
    }


def test_memory_media_ingestion_service_returns_provider_unconfigured_without_client() -> None:
    service = MemoryMediaIngestionService(remote_client=None)

    result = service.ingest(
        identity=RequestIdentity.for_user(user_id="u1", session_id="s1"),
        files=[
            MemoryMediaIngestionFile(
                file_url="file:///tmp/breakfast.mp4",
                filename="breakfast.mp4",
                media_type="video",
                start_time=NOW,
            )
        ],
    )

    assert result.status == "provider_unconfigured"
    assert result.task_id == ""
    assert result.errors == [
        {
            "code": "provider_unconfigured",
            "message": "Memory Server media ingestion is not configured.",
            "recoverable": True,
        }
    ]


def test_dual_core_memory_media_ingestion_service_uses_remote_client() -> None:
    service = create_memory_media_ingestion_service(
        ProviderConfig(
            memory_backend="dual_core",
            memory_server_base_url="http://memory.local",
        )
    )

    assert service.remote_client is not None
    assert service.remote_client.base_url == "http://memory.local"


def test_memory_media_ingestion_service_reports_task_status_scope_warning() -> None:
    requests: list[MemoryServerRequest] = []

    def transport(request: MemoryServerRequest) -> dict:
        requests.append(request)
        return {
            "task_id": "task-1",
            "status": "completed",
            "total_files": 1,
            "processed_files": 1,
            "failed_files": 0,
            "statistics": {"memories_created": 2},
            "results": [{"summary": "done"}],
            "errors": [],
            "code": 200,
        }

    service = MemoryMediaIngestionService(
        remote_client=RemoteMemoryClient(base_url="http://memory.local", transport=transport)
    )

    result = service.task_status(
        identity=RequestIdentity.for_user(user_id="trusted-user", session_id="trusted-session"),
        task_id="task-1",
    )

    assert result == MemoryMediaTaskStatusResult(
        task_id="task-1",
        status="completed",
        total_files=1,
        processed_files=1,
        failed_files=0,
        statistics={"memories_created": 2},
        results=[{"summary": "done"}],
        errors=[],
        code=200,
        scope_warning="memory_server_task_lookup_user_scope_not_enforced",
        output_ref="memory_server://tasks/task-1",
    )
    assert requests[0].body == {"user_id": "trusted-user", "task_id": "task-1"}


def test_memory_media_ingest_tool_uses_runtime_identity_over_model_identity() -> None:
    class RecordingService:
        def __init__(self) -> None:
            self.identity: RequestIdentity | None = None

        def ingest(self, *, identity: RequestIdentity, files: list[MemoryMediaIngestionFile]) -> MemoryMediaIngestionResult:
            self.identity = identity
            return MemoryMediaIngestionResult(
                status="processing",
                task_id="task-1",
                accepted_count=len(files),
                file_ids=["file-1"],
                output_ref="memory_server://tasks/task-1",
            )

    service = RecordingService()

    result = MemoryMediaIngestTool(service).run(
        {
            "user_id": "model-user",
            "session_id": "model-session",
            "files": [
                {
                    "file_url": "file:///tmp/breakfast.mp4",
                    "filename": "breakfast.mp4",
                    "media_type": "video",
                    "start_time": "2026-04-11T12:00:00Z",
                }
            ],
        },
        ToolContext(user_id="runtime-user", session_id="runtime-session"),
    )

    assert result.success is True
    assert service.identity == RequestIdentity.for_user(user_id="runtime-user", session_id="runtime-session")
    assert result.data is not None
    assert result.data["task_id"] == "task-1"
    assert result.contract is not None
    assert result.contract.capability == "memory_media_ingest"


def test_memory_ingest_status_tool_uses_runtime_identity_and_returns_scope_warning() -> None:
    class RecordingService:
        def __init__(self) -> None:
            self.identity: RequestIdentity | None = None

        def task_status(self, *, identity: RequestIdentity, task_id: str) -> MemoryMediaTaskStatusResult:
            self.identity = identity
            return MemoryMediaTaskStatusResult(
                task_id=task_id,
                status="completed",
                total_files=1,
                processed_files=1,
                failed_files=0,
                scope_warning="memory_server_task_lookup_user_scope_not_enforced",
                output_ref=f"memory_server://tasks/{task_id}",
            )

    service = RecordingService()

    result = MemoryIngestStatusTool(service).run(
        {"user_id": "model-user", "task_id": "task-1"},
        ToolContext(user_id="runtime-user", session_id="runtime-session"),
    )

    assert result.success is True
    assert service.identity == RequestIdentity.for_user(user_id="runtime-user", session_id="runtime-session")
    assert result.data is not None
    assert result.data["scope_warning"] == "memory_server_task_lookup_user_scope_not_enforced"


def test_default_memory_media_ingest_tool_is_unconfigured_without_remote_service() -> None:
    result = create_default_registry().run(
        "memory_media_ingest",
        {
            "files": [
                {
                    "file_url": "file:///tmp/breakfast.mp4",
                    "filename": "breakfast.mp4",
                    "media_type": "video",
                    "start_time": "2026-04-11T12:00:00Z",
                }
            ]
        },
        ToolContext(user_id="u1", session_id="s1"),
    )

    assert result.success is False
    assert result.error == "provider_unconfigured: Memory Server media ingestion is not configured."
    assert result.data is not None
    assert result.data["status"] == "provider_unconfigured"


def test_memory_media_tools_are_registered_with_governed_side_effect_policies() -> None:
    specs = {spec.name: spec for spec in create_default_registry().list_specs()}

    assert specs["memory_media_ingest"].side_effect.level == "committed"
    assert specs["memory_media_ingest"].side_effect.requires_confirmation is True
    assert specs["memory_media_ingest"].side_effect.confirmation_kind == "memory_media_ingest"
    assert specs["memory_ingest_status"].side_effect.level == "external_read"
    assert specs["memory_ingest_status"].side_effect.requires_confirmation is False
    assert "user_id" not in specs["memory_media_ingest"].input_schema["fields"]
    assert "session_id" not in specs["memory_media_ingest"].input_schema["fields"]
    assert "user_id" not in specs["memory_ingest_status"].input_schema["fields"]


def test_action_validator_requires_explicit_memory_media_ingest_intent() -> None:
    request = UserRequest(user_id="u1", session_id="s1", text="分析这个视频", video_ids=["v1"])
    validation = ActionValidator().validate(
        decision=AssistantDecision(
            type="tool_call",
            tool_name="memory_media_ingest",
            tool_input={
                "files": [
                    {
                        "file_url": "file:///tmp/breakfast.mp4",
                        "filename": "breakfast.mp4",
                        "media_type": "video",
                        "start_time": "2026-04-11T12:00:00Z",
                    }
                ]
            },
        ),
        registry=create_default_registry(),
        request=request,
        state=AgentState.from_request(request),
    )

    assert validation.accepted is False
    assert validation.code == "memory_media_ingest_intent_required"


def test_tool_catalog_exposes_qualified_media_ingest_tools_independent_of_request_text() -> None:
    specs = create_default_registry().list_specs()

    ingest_selection = select_prompt_tool_specs(
        UserRequest(user_id="u1", session_id="s1", text="把这个视频上传到记忆服务，之后可以检索", video_ids=["v1"]),
        specs,
    )
    plain_video_selection = select_prompt_tool_specs(
        UserRequest(user_id="u1", session_id="s1", text="分析这个视频里发生了什么", video_ids=["v1"]),
        specs,
    )

    assert "memory_media_ingest" in [spec.name for spec in ingest_selection.prompt_tool_specs]
    assert "memory_ingest_status" in [spec.name for spec in ingest_selection.prompt_tool_specs]
    assert "memory_media_ingest" in [spec.name for spec in plain_video_selection.prompt_tool_specs]
    assert "memory_ingest_status" in [spec.name for spec in plain_video_selection.prompt_tool_specs]

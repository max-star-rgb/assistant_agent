"""Read-only Langfuse SDK adapter for runtime audits."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
import os
from typing import Any

from assistant_agent.observability.langfuse_config import (
    langfuse_credentials_from_env,
    langfuse_host_from_env,
)
from assistant_agent.observability.runtime_audit.models import (
    LangfuseScoreSnapshot,
    LangfuseTraceSnapshot,
)
from assistant_agent.providers.provider_http import without_unsupported_socks_proxy_env


class LangfuseSdkAuditSource:
    """Fetch complete `assistant.turn` traces through Langfuse public read APIs."""

    def __init__(self, client: Any, *, page_size: int = 100) -> None:
        if page_size < 1:
            raise ValueError("page_size must be positive")
        self.client = client
        self.page_size = page_size

    def list_traces(
        self,
        *,
        window_start: datetime,
        window_end: datetime,
    ) -> list[LangfuseTraceSnapshot]:
        headers: list[Any] = []
        page = 1
        while True:
            response = self.client.api.trace.list(
                page=page,
                limit=self.page_size,
                name="assistant.turn",
                from_timestamp=window_start,
                to_timestamp=window_end,
                order_by="timestamp.asc",
            )
            headers.extend(response.data)
            total_pages = int(getattr(response.meta, "total_pages", page) or page)
            if page >= total_pages:
                break
            page += 1
        traces = []
        for header in headers:
            trace_id = _field(header, "id")
            if not isinstance(trace_id, str) or not trace_id:
                continue
            detail = self.client.api.trace.get(trace_id)
            snapshot = LangfuseTraceSnapshot.from_api_payload(detail)
            if snapshot.name in {None, "assistant.turn"}:
                traces.append(
                    snapshot.model_copy(update={"scores": self._scores_for_trace(trace_id)})
                )
        return traces

    def _scores_for_trace(self, trace_id: str):
        scores = []
        cursor = None
        while True:
            response = self.client.api.scores_v3.get_many_v3(
                limit=100,
                cursor=cursor,
                fields="subject",
                trace_id=trace_id,
            )
            scores.extend(
                LangfuseScoreSnapshot.from_api_payload(item) for item in response.data
            )
            cursor = getattr(response.meta, "cursor", None)
            if not cursor:
                return scores

    def close(self) -> None:
        shutdown = getattr(self.client, "shutdown", None)
        if callable(shutdown):
            shutdown()


def create_langfuse_audit_source_from_env(
    values: Mapping[str, str] | None = None,
) -> LangfuseSdkAuditSource:
    """Create the read client without logging or copying credentials into artifacts."""

    from langfuse import Langfuse

    env = os.environ if values is None else values
    public_key, secret_key = langfuse_credentials_from_env(env)
    if not public_key or not secret_key:
        raise RuntimeError("Langfuse credentials are required for runtime audit collection.")
    with without_unsupported_socks_proxy_env():
        client = Langfuse(
            public_key=public_key,
            secret_key=secret_key,
            host=langfuse_host_from_env(env),
        )
    return LangfuseSdkAuditSource(client)


def _field(value: Any, name: str) -> Any:
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)

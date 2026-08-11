"""Read-only Langfuse SDK adapter for runtime audits."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
import json
import os
from typing import Any

from assistant_agent.observability.langfuse_config import (
    langfuse_credentials_from_env,
    langfuse_host_from_env,
)
from assistant_agent.observability.runtime_audit.models import (
    LangfuseObservationSnapshot,
    LangfuseScoreSnapshot,
    LangfuseTraceSnapshot,
)
from assistant_agent.providers.provider_http import without_unsupported_socks_proxy_env


class LangfuseSdkAuditSource:
    """Reconstruct complete `assistant.turn` traces from Langfuse v4 observations."""

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
        root_headers: list[dict[str, Any]] = []
        cursor = None
        while True:
            response = self.client.api.observations.get_many(
                limit=self.page_size,
                cursor=cursor,
                name="agent.runtime",
                from_start_time=window_start,
                to_start_time=window_end,
                fields="core,basic,time,io,metadata,trace_context",
                expand_metadata="true",
            )
            root_headers.extend(_observation_payload(item) for item in response.data)
            cursor = getattr(response.meta, "cursor", None)
            if not cursor:
                break

        trace_ids: list[str] = []
        for root in root_headers:
            if (
                root.get("trace_name") != "assistant.turn"
                or root.get("parent_observation_id") is not None
            ):
                continue
            trace_id = root.get("trace_id")
            if isinstance(trace_id, str) and trace_id and trace_id not in trace_ids:
                trace_ids.append(trace_id)

        traces: list[LangfuseTraceSnapshot] = []
        for trace_id in trace_ids:
            trace_observations = self._observations_for_trace(trace_id)
            roots = [
                item
                for item in trace_observations
                if item.get("name") == "agent.runtime"
                and item.get("parent_observation_id") is None
            ]
            if len(roots) != 1:
                continue
            root = roots[0]
            timestamp = root.get("start_time")
            if not isinstance(timestamp, datetime) or not (
                window_start <= timestamp < window_end
            ):
                continue
            ordered = sorted(
                trace_observations,
                key=lambda item: (
                    item.get("start_time") or timestamp,
                    str(item.get("id") or ""),
                ),
            )
            traces.append(
                LangfuseTraceSnapshot.model_validate(
                    {
                        "trace_id": trace_id,
                        "name": root.get("trace_name"),
                        "timestamp": timestamp,
                        "session_id": root.get("session_id"),
                        "trace_url": self._trace_url(trace_id),
                        "user_id": root.get("user_id"),
                        "environment": root.get("environment"),
                        "input": root.get("input"),
                        "output": root.get("output"),
                        "metadata": root.get("metadata"),
                        "tags": root.get("tags") or [],
                        "observations": [
                            LangfuseObservationSnapshot.from_api_payload(item)
                            for item in ordered
                        ],
                        "scores": self._scores_for_trace(trace_id),
                    }
                )
            )
        return sorted(traces, key=lambda item: (item.timestamp, item.trace_id))

    def _observations_for_trace(self, trace_id: str) -> list[dict[str, Any]]:
        observations: list[dict[str, Any]] = []
        cursor = None
        while True:
            response = self.client.api.observations.get_many(
                trace_id=trace_id,
                limit=self.page_size,
                cursor=cursor,
                fields="core,basic,time,io,metadata,trace_context",
                expand_metadata="true",
            )
            observations.extend(_observation_payload(item) for item in response.data)
            cursor = getattr(response.meta, "cursor", None)
            if not cursor:
                return observations

    def _trace_url(self, trace_id: str) -> str | None:
        get_trace_url = getattr(self.client, "get_trace_url", None)
        if not callable(get_trace_url):
            return None
        try:
            value = get_trace_url(trace_id=trace_id)
        except Exception:
            return None
        return value if isinstance(value, str) and value else None

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


def _observation_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        payload = dict(value)
    else:
        model_dump = getattr(value, "model_dump", None)
        if not callable(model_dump):
            raise TypeError(
                f"unsupported Langfuse observation type: {type(value).__name__}"
            )
        payload = dict(model_dump(mode="python"))
    for key in ("input", "output"):
        raw = payload.get(key)
        if isinstance(raw, str):
            try:
                payload[key] = json.loads(raw)
            except json.JSONDecodeError:
                pass
    return payload

#!/usr/bin/env python3
"""Measure DeepSeek streaming latency through provider and runtime layers."""

from __future__ import annotations

# ruff: noqa: E402

import argparse
import json
import os
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.config import ProviderConfig
from assistant_agent.schemas.events import AgentEvent
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.services.chat_adapter import (
    ChatAdapter,
    ChatRequest,
    ChatResult,
    create_chat_adapter,
)
from assistant_agent.services.provider_specs import resolve_chat_provider


DEFAULT_TEXT = "用一句中文回答：杭州适合喝什么茶？"
VALID_REAL_PROFILES = {"provider_smoke", "pilot"}


class TimingChatAdapter:
    """Wrap a chat adapter and record per-call streaming timing."""

    def __init__(self, inner: ChatAdapter) -> None:
        self.inner = inner
        self.calls: list[dict[str, Any]] = []
        self.provider = getattr(inner, "provider", "deepseek")
        self.model = getattr(inner, "model", None)

    def chat(self, request: ChatRequest) -> ChatResult:
        call_index = len(self.calls) + 1
        started_at = time.perf_counter()
        original_callback = request.stream_callback
        first_delta_at: float | None = None
        first_delta_preview = ""
        delta_count = 0

        def record_delta(text: str, payload: dict[str, Any]) -> None:
            nonlocal first_delta_at, first_delta_preview, delta_count
            if text:
                delta_count += 1
                if first_delta_at is None:
                    first_delta_at = time.perf_counter()
                    first_delta_preview = _preview(text)
            if original_callback is not None:
                original_callback(text, payload)

        wrapped_request = request.model_copy(update={"stream_callback": record_delta})
        record: dict[str, Any] = {
            "call_index": call_index,
            "status": "failed",
            "provider": self.provider,
            "model": self.model,
            "message_kind": None,
            "finish_reason": None,
            "tool_call_count": 0,
            "tool_names": [],
            "ttft_ms": None,
            "stream_open_ms": None,
            "total_ms": None,
            "post_first_delta_ms": None,
            "delta_count": 0,
            "first_delta_preview": "",
            "response_chars": 0,
            "errors": [],
        }
        try:
            result = self.inner.chat(wrapped_request)
        except Exception as exc:
            record["total_ms"] = _round_ms(_elapsed_ms(started_at))
            record["errors"] = [{"code": exc.__class__.__name__, "message": str(exc)}]
            self.calls.append(record)
            raise

        total_ms = _elapsed_ms(started_at)
        ttft_ms = None if first_delta_at is None else (first_delta_at - started_at) * 1000
        record.update(
            {
                "status": "success" if result.success else "failed",
                "provider": result.provider,
                "model": result.model,
                "message_kind": _result_message_kind(result),
                "finish_reason": result.finish_reason,
                "tool_call_count": len(result.tool_calls),
                "tool_names": [call.name for call in result.tool_calls],
                "ttft_ms": _round_ms(ttft_ms),
                "stream_open_ms": result.latency_ms,
                "total_ms": _round_ms(total_ms),
                "post_first_delta_ms": _round_ms(None if ttft_ms is None else total_ms - ttft_ms),
                "delta_count": delta_count,
                "first_delta_preview": first_delta_preview,
                "response_chars": len(result.response_text or ""),
                "errors": [error.model_dump(mode="json") for error in result.errors],
            }
        )
        self.calls.append(record)
        return result


class TimingEventSink:
    """Record end-to-end runtime event timings."""

    def __init__(self, started_at: float) -> None:
        self.started_at = started_at
        self.first_event_ms: float | None = None
        self.first_response_delta_ms: float | None = None
        self.first_token_streaming_response_delta_ms: float | None = None
        self.first_response_delta_preview = ""
        self.event_counts: dict[str, int] = {}

    def emit(self, event: AgentEvent) -> None:
        elapsed_ms = _elapsed_ms(self.started_at)
        self.event_counts[event.type] = self.event_counts.get(event.type, 0) + 1
        if self.first_event_ms is None:
            self.first_event_ms = elapsed_ms
        if event.type == "response_delta":
            if self.first_response_delta_ms is None:
                self.first_response_delta_ms = elapsed_ms
                self.first_response_delta_preview = _preview(event.text or "")
            if (
                event.payload.get("token_streaming") is True
                and self.first_token_streaming_response_delta_ms is None
            ):
                self.first_token_streaming_response_delta_ms = elapsed_ms

    def as_payload(self) -> dict[str, Any]:
        return {
            "first_event_ms": _round_ms(self.first_event_ms),
            "first_response_delta_ms": _round_ms(self.first_response_delta_ms),
            "first_token_streaming_response_delta_ms": _round_ms(self.first_token_streaming_response_delta_ms),
            "first_response_delta_preview": self.first_response_delta_preview,
            "event_counts": dict(sorted(self.event_counts.items())),
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Measure DeepSeek first-token and end-to-end latency. Requires an explicit "
            "real-provider runtime profile and local DeepSeek credentials."
        )
    )
    parser.add_argument("--text", default=DEFAULT_TEXT, help="Prompt used for all latency samples.")
    parser.add_argument("--runs", type=int, default=3, help="Number of measured samples.")
    parser.add_argument(
        "--mode",
        choices=("provider", "runtime", "both"),
        default="both",
        help="Measure direct provider adapter, Agent runtime end-to-end, or both.",
    )
    parser.add_argument("--user-id", default="latency_user", help="User id for runtime samples.")
    parser.add_argument("--session-id", default="latency_session", help="Session id prefix for samples.")
    parser.add_argument("--max-tokens", type=int, default=128, help="Chat max_tokens for provider samples.")
    parser.add_argument("--temperature", type=float, default=0.2, help="Chat temperature for provider samples.")
    parser.add_argument("--sleep", type=float, default=0.25, help="Seconds to sleep between runs.")
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON output. Compact JSON is easier for machine parsing.",
    )
    parser.add_argument(
        "--expect-chat-calls",
        type=int,
        default=None,
        help="Assert each runtime sample made exactly this many chat calls.",
    )
    parser.add_argument(
        "--expect-first-call-kind",
        choices=("final_answer", "tool_call", "refusal"),
        default=None,
        help="Assert the first runtime chat call has this provider-native message kind.",
    )
    parser.add_argument(
        "--expect-tool",
        action="append",
        default=[],
        help="Assert a runtime sample includes this native tool name. Can be repeated.",
    )
    return parser


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.runs < 1:
        print("invalid_args")
        print("--runs must be >= 1")
        return 2
    if args.max_tokens < 1:
        print("invalid_args")
        print("--max-tokens must be >= 1")
        return 2
    if args.expect_chat_calls is not None and args.expect_chat_calls < 0:
        print("invalid_args")
        print("--expect-chat-calls must be >= 0")
        return 2
    if args.mode == "provider" and _runtime_assertions_requested(args):
        print("invalid_args")
        print("runtime assertions require --mode runtime or --mode both")
        return 2

    source = dict(env if env is not None else os.environ)
    missing = _missing_deepseek_config(source)
    if missing:
        _print_provider_unconfigured(missing)
        return 2

    config = ProviderConfig.from_env(source)
    if config.chat_provider != "deepseek":
        _print_provider_unconfigured("resolved chat provider is not deepseek; check runtime profile and provider env")
        return 2
    if not config.chat_stream:
        _print_provider_unconfigured("DEEPSEEK_CHAT_STREAM=true is required to measure TTFT")
        return 2

    samples: list[dict[str, Any]] = []
    provider_adapter: ChatAdapter | None = None
    if args.mode in {"provider", "both"}:
        provider_adapter = create_chat_adapter(config)

    for run_index in range(1, args.runs + 1):
        sample: dict[str, Any] = {"run_index": run_index}
        if args.mode in {"provider", "both"}:
            assert provider_adapter is not None
            sample["provider"] = _measure_provider_sample(provider_adapter, args, run_index)
        if args.mode in {"runtime", "both"}:
            sample["runtime"] = _measure_runtime_sample(config, args, run_index)
        samples.append(sample)
        if run_index < args.runs and args.sleep > 0:
            time.sleep(args.sleep)

    output = {
        "status": _status_from_samples(samples),
        "provider": config.chat_provider,
        "model": config.chat_model,
        "mode": args.mode,
        "runs": args.runs,
        "metric_notes": {
            "provider.ttft_ms": "Direct adapter time from adapter.chat() call to first non-empty provider text delta.",
            "provider.stream_open_ms": (
                "Existing adapter latency_ms; in stream mode this is time to create the SDK stream object."
            ),
            "runtime.first_response_delta_ms": (
                "End-to-end runtime time from run_state() call to first response_delta event."
            ),
            "runtime.chat_calls": "Provider calls made inside the runtime, each measured by TimingChatAdapter.",
            "runtime.chat_calls.message_kind": "Provider-native response type: final_answer, tool_call, or refusal.",
            "runtime.chat_calls.tool_names": "Native tool names returned by the provider for that chat call.",
        },
        "samples": samples,
        "summary": _summary(samples),
    }
    print(json.dumps(output, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0 if output["status"] == "success" else 1


def _missing_deepseek_config(source: Mapping[str, str]) -> str | None:
    profile = source.get("MULTIMODAL_AGENT_RUNTIME_PROFILE")
    if profile not in VALID_REAL_PROFILES:
        return "missing MULTIMODAL_AGENT_RUNTIME_PROFILE=provider_smoke or pilot"
    provider = source.get("MULTIMODAL_AGENT_CHAT_PROVIDER")
    if provider != "deepseek":
        return "missing MULTIMODAL_AGENT_CHAT_PROVIDER=deepseek"
    missing = resolve_chat_provider("deepseek", source).missing_required_env()
    if missing:
        return f"missing {', '.join(missing)}"
    return None


def _measure_provider_sample(adapter: ChatAdapter, args: argparse.Namespace, run_index: int) -> dict[str, Any]:
    timed_adapter = TimingChatAdapter(adapter)
    request = ChatRequest(
        user_id=args.user_id,
        session_id=f"{args.session_id}_provider_{run_index}",
        user_query=args.text,
        max_tokens=args.max_tokens,
        temperature=args.temperature,
    )
    result = timed_adapter.chat(request)
    payload = dict(timed_adapter.calls[-1])
    payload["output_ref"] = result.output_ref
    payload["finish_reason"] = result.finish_reason
    return payload


def _measure_runtime_sample(config: ProviderConfig, args: argparse.Namespace, run_index: int) -> dict[str, Any]:
    timed_adapter = TimingChatAdapter(create_chat_adapter(config))
    runtime = AgentGraphRuntime(config=config, chat_adapter=timed_adapter)
    request = UserRequest(
        user_id=args.user_id,
        session_id=f"{args.session_id}_runtime_{run_index}",
        text=args.text,
    )
    started_at = time.perf_counter()
    event_sink = TimingEventSink(started_at)
    try:
        state = runtime.run_state(request, event_sink=event_sink)
    except Exception as exc:
        total_ms = _elapsed_ms(started_at)
        return {
            "status": "failed",
            "total_ms": _round_ms(total_ms),
            **event_sink.as_payload(),
            "chat_calls": timed_adapter.calls,
            "response_chars": 0,
            "run_id": None,
            "trace_id": None,
            "errors": [{"code": exc.__class__.__name__, "message": str(exc)}],
            "assertion_failures": [],
        }

    total_ms = _elapsed_ms(started_at)
    errors = getattr(state, "errors", [])
    response = getattr(state, "response", None)
    response_text = response.message if response is not None else ""
    payload = {
        "status": "success" if getattr(state, "status", "failed") != "failed" else "failed",
        "total_ms": _round_ms(total_ms),
        **event_sink.as_payload(),
        "chat_calls": timed_adapter.calls,
        "response_chars": len(response_text or ""),
        "run_id": getattr(state, "run_id", None),
        "trace_id": getattr(state, "trace_id", None),
        "errors": [_error_payload(error) for error in errors],
    }
    assertion_failures = _runtime_assertion_failures(payload, args)
    payload["assertion_failures"] = assertion_failures
    if assertion_failures and payload["status"] == "success":
        payload["status"] = "failed"
    return payload


def _summary(samples: list[dict[str, Any]]) -> dict[str, Any]:
    metrics: dict[str, list[float]] = {
        "provider.ttft_ms": [],
        "provider.stream_open_ms": [],
        "provider.total_ms": [],
        "runtime.first_response_delta_ms": [],
        "runtime.first_token_streaming_response_delta_ms": [],
        "runtime.total_ms": [],
        "runtime.chat_call_ttft_ms": [],
        "runtime.chat_call_total_ms": [],
    }
    for sample in samples:
        provider = sample.get("provider")
        if isinstance(provider, dict):
            _append_metric(metrics["provider.ttft_ms"], provider.get("ttft_ms"))
            _append_metric(metrics["provider.stream_open_ms"], provider.get("stream_open_ms"))
            _append_metric(metrics["provider.total_ms"], provider.get("total_ms"))
        runtime = sample.get("runtime")
        if isinstance(runtime, dict):
            _append_metric(metrics["runtime.first_response_delta_ms"], runtime.get("first_response_delta_ms"))
            _append_metric(
                metrics["runtime.first_token_streaming_response_delta_ms"],
                runtime.get("first_token_streaming_response_delta_ms"),
            )
            _append_metric(metrics["runtime.total_ms"], runtime.get("total_ms"))
            for call in runtime.get("chat_calls", []):
                if isinstance(call, dict):
                    _append_metric(metrics["runtime.chat_call_ttft_ms"], call.get("ttft_ms"))
                    _append_metric(metrics["runtime.chat_call_total_ms"], call.get("total_ms"))
    return {name: _stats(values) for name, values in metrics.items() if values}


def _append_metric(values: list[float], value: Any) -> None:
    if isinstance(value, int | float):
        values.append(float(value))


def _stats(values: list[float]) -> dict[str, Any]:
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "min_ms": _round_ms(ordered[0]),
        "p50_ms": _round_ms(_percentile(ordered, 0.50)),
        "avg_ms": _round_ms(sum(ordered) / len(ordered)),
        "p95_ms": _round_ms(_percentile(ordered, 0.95)),
        "max_ms": _round_ms(ordered[-1]),
    }


def _percentile(values: list[float], percentile: float) -> float:
    if len(values) == 1:
        return values[0]
    index = max(0, min(len(values) - 1, round((len(values) - 1) * percentile)))
    return values[index]


def _status_from_samples(samples: list[dict[str, Any]]) -> str:
    for sample in samples:
        for key in ("provider", "runtime"):
            layer = sample.get(key)
            if isinstance(layer, dict) and layer.get("status") != "success":
                return "failed"
    return "success"


def _runtime_assertions_requested(args: argparse.Namespace) -> bool:
    return (
        getattr(args, "expect_chat_calls", None) is not None
        or getattr(args, "expect_first_call_kind", None) is not None
        or bool(getattr(args, "expect_tool", []) or [])
    )


def _runtime_assertion_failures(runtime: dict[str, Any], args: argparse.Namespace) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    chat_calls = runtime.get("chat_calls", [])
    if not isinstance(chat_calls, list):
        chat_calls = []

    expected_chat_calls = getattr(args, "expect_chat_calls", None)
    if expected_chat_calls is not None and len(chat_calls) != expected_chat_calls:
        failures.append(
            {
                "code": "chat_call_count_mismatch",
                "expected": expected_chat_calls,
                "actual": len(chat_calls),
            }
        )

    expected_first_kind = getattr(args, "expect_first_call_kind", None)
    if expected_first_kind is not None:
        actual_first_kind = (
            chat_calls[0].get("message_kind") if chat_calls and isinstance(chat_calls[0], dict) else None
        )
        if actual_first_kind != expected_first_kind:
            failures.append(
                {
                    "code": "first_call_kind_mismatch",
                    "expected": expected_first_kind,
                    "actual": actual_first_kind,
                }
            )

    expected_tools = [tool for tool in (getattr(args, "expect_tool", []) or []) if tool]
    if expected_tools:
        observed_tools = {
            tool
            for call in chat_calls
            if isinstance(call, dict)
            for tool in call.get("tool_names", [])
            if isinstance(tool, str)
        }
        for expected_tool in expected_tools:
            if expected_tool not in observed_tools:
                failures.append(
                    {
                        "code": "expected_tool_missing",
                        "expected": expected_tool,
                        "actual": sorted(observed_tools),
                    }
                )
    return failures


def _error_payload(error: Any) -> dict[str, Any]:
    if hasattr(error, "model_dump"):
        return error.model_dump(mode="json")
    if isinstance(error, dict):
        return error
    return {"code": error.__class__.__name__, "message": str(error)}


def _result_message_kind(result: ChatResult) -> str | None:
    if result.message_kind:
        return result.message_kind
    if result.tool_calls:
        return "tool_call"
    if result.refusal:
        return "refusal"
    if result.response_text:
        return "final_answer"
    return None


def _elapsed_ms(started_at: float) -> float:
    return (time.perf_counter() - started_at) * 1000


def _round_ms(value: float | None) -> float | None:
    return None if value is None else round(float(value), 1)


def _preview(text: str, limit: int = 40) -> str:
    normalized = " ".join(text.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1] + "…"


def _print_provider_unconfigured(reason: str) -> None:
    print("provider_unconfigured")
    print(reason)
    print(
        "Set MULTIMODAL_AGENT_RUNTIME_PROFILE=provider_smoke, "
        "MULTIMODAL_AGENT_CHAT_PROVIDER=deepseek, DEEPSEEK_CHAT_API_KEY, "
        "and DEEPSEEK_CHAT_STREAM=true before measuring DeepSeek latency."
    )


if __name__ == "__main__":
    raise SystemExit(main())

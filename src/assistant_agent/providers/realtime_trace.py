"""LangSmith spans for native Realtime turns, without tracing audio or the socket."""

import json
import logging
from collections import deque
from copy import deepcopy
from datetime import datetime, timezone
from functools import wraps
from uuid import uuid4

from langsmith.run_trees import RunTree
from langsmith.utils import tracing_is_enabled

_log = logging.getLogger(__name__)


def observational(method):
    @wraps(method)
    def safe(self, *args, **kwargs):
        if not self.enabled:
            return None
        try:
            return method(self, *args, **kwargs)
        except Exception as exc:
            _log.warning("Voice tracing failed operation=%s error_type=%s", method.__name__, type(exc).__name__)
            return None
    return safe


class VoiceTrace:
    def __init__(self, thread_id, model):
        self.enabled = tracing_is_enabled()
        self.thread_id, self.model = thread_id, model
        self.turns = {}
        self.responses = {}
        self.current = None
        self.history = {}
        self.snapshots = deque(maxlen=128)
        self.history_trimmed = False
        self.generation_starts = deque(maxlen=16)

    @observational
    def generation_requested(self, source):
        self.generation_starts.append((datetime.now(timezone.utc), source))

    def remember(self, item_id, message):
        self.history[item_id] = message
        # This is an observed text window, not a reconstruction of provider tokens.
        if len(self.history) > 256:
            self.history.pop(next(iter(self.history)))
            self.history_trimmed = True

    @observational
    def item_sent(self, item):
        """Record only text fields, after successful transport submission."""
        item_id = item.get("id") or item.get("call_id") or uuid4().hex
        kind = item.get("type")
        if kind == "function_call_output":
            self.remember(item_id, {"role": "tool", "tool_call_id": item["call_id"], "content": item["output"]})
        elif kind == "function_call":
            try:
                args = json.loads(item.get("arguments", ""))
            except (TypeError, ValueError):
                args = None
            self.remember(item_id, {"role": "assistant", "content": "", "tool_calls": [
                {"id": item.get("call_id"), "name": item.get("name"), "args": args}]})
        elif kind == "message":
            text = "".join(p.get("transcript", p.get("text", "")) for p in item.get("content", [])
                           if p.get("type") in {"audio", "input_audio", "text", "input_text"})
            # ASR can arrive before conversation.item.created; do not erase it.
            if text or item_id not in self.history:
                self.remember(item_id, {"role": item.get("role", "user"), "content": text[:16000]})

    def start(self, item_id, messages, start_time=None):
        root = RunTree(name="VoiceAgent", run_type="chain", inputs={"messages": messages},
            start_time=start_time or datetime.now(timezone.utc),
            extra={"metadata": {"thread_id": self.thread_id, "voice_session_id": self.thread_id,
                                "source": "browser_voice", "input_item_id": item_id}})
        root.post()
        self.current = item_id
        self.turns[item_id] = root
        # Retain a bounded window for late ASR updates, including finished turns.
        if len(self.turns) > 128:
            self.turns.pop(next(iter(self.turns)))
        return root

    def finish(self, root, status, messages=None):
        if root is not None and root.end_time is None:
            root.end(outputs={"messages": messages or [], "status": status},
                     error="realtime_response_failed" if status == "failed" else None)
            root.patch(exclude_inputs=False)

    @observational
    def event(self, event):
        kind = event.get("type")
        if kind == "input_audio_buffer.speech_started":
            self.generation_starts.clear()
            self.finish(self.turns.get(self.current), "interrupted")
            self.start(event["item_id"], [])
            self.remember(event["item_id"], {"role": "user", "content": ""})
        elif kind == "input_audio_buffer.committed":
            self.generation_requested("input_audio_buffer.committed")
        elif kind == "session.updated":
            instructions = event.get("session", {}).get("instructions")
            if isinstance(instructions, str):
                self.remember("session-instructions", {"role": "system", "content": instructions})
        elif kind == "conversation.item.created":
            # Response output is recorded at response.done, not twice at creation.
            item = event.get("item", {})
            if item.get("role") == "user" or item.get("type") == "function_call_output":
                self.item_sent(item)
        elif kind == "conversation.item.deleted":
            self.history.pop(event.get("item_id"), None)
        elif kind == "conversation.item.truncated":
            # No audio/text alignment is supplied: do not invent the heard prefix.
            item = self.history.get(event.get("item_id"))
            if item is not None:
                item["content"] = ""
        elif kind == "conversation.item.input_audio_transcription.completed":
            if not isinstance(event.get("transcript"), str):
                return
            item_id = event.get("item_id")
            message = {"role": "user", "content": event["transcript"][:16000]}
            self.remember(item_id, message)
            for child, ids in self.snapshots:
                if item_id in ids:
                    child.inputs["messages"][ids.index(item_id)] = deepcopy(message)
                    if child.end_time is not None:
                        child.patch(exclude_inputs=False)
            root = self.turns.get(event.get("item_id"))
            if root is not None:
                root.inputs = {"messages": [{"role": "user", "content": event["transcript"][:16000]}]}
                if root.end_time is not None:
                    root.patch(exclude_inputs=False)
        elif kind == "response.created":
            rid = event["response"]["id"]
            if rid in self.responses:
                return
            started, origin = self.generation_starts.popleft() if self.generation_starts else (datetime.now(timezone.utc), "unavailable")
            root = self.turns.get(self.current)
            if root is None or root.end_time is not None:
                root = self.start("response-" + rid, [], start_time=started)
            run = root.create_child("QwenRealtime", run_type="llm", inputs={"messages": deepcopy(list(self.history.values()))},
                start_time=started,
                extra={"metadata": {"ls_provider": "qwen", "ls_model_name": self.model,
                                    "response_id": rid, "context_representation": "observed_text",
                                    "ttft_origin": origin,
                                    "history_trimmed": self.history_trimmed}})
            run.post()
            self.snapshots.append((run, list(self.history)))
            self.responses[rid] = {"root": root, "run": run, "parts": {}, "calls": []}
        elif kind.startswith("response."):
            rid = event.get("response_id") or event.get("response", {}).get("id")
            response = self.responses.get(rid)
            if response is None:
                return
            run = response["run"]
            key = (event.get("output_index", 0), event.get("content_index", 0))
            if kind in {"response.audio_transcript.delta", "response.text.delta", "response.function_call_arguments.delta"}:
                if event.get("delta") and not run.events and run.extra["metadata"]["ttft_origin"] != "unavailable":
                    run.add_event({"name": "new_token", "time": datetime.now(timezone.utc).isoformat()})
            if kind in {"response.audio_transcript.delta", "response.text.delta"}:
                # Keep text out of token events; the complete text lives in outputs.
                response["parts"][key] = (response["parts"].get(key, "") + event.get("delta", ""))[:16000]
            elif kind in {"response.audio_transcript.done", "response.text.done"}:
                response["parts"][key] = event.get("transcript", event.get("text", ""))[:16000]
            elif kind == "response.function_call_arguments.done":
                try:
                    args = json.loads(event.get("arguments", ""))
                except (TypeError, ValueError):
                    args = None
                response["calls"].append({"id": event.get("call_id"), "name": event.get("name"), "args": args})
            elif kind == "response.done":
                body = event["response"]
                # response.done carries canonical output when supplied by the provider.
                final_parts = [p.get("transcript", p.get("text", ""))
                    for item in body.get("output", []) if item.get("type") == "message"
                    for p in item.get("content", []) if p.get("type") in {"audio", "text"}]
                text = "".join(final_parts if final_parts else response["parts"].values())[:16000]
                message = {"role": "assistant", "content": text}
                if response["calls"]:
                    message["tool_calls"] = response["calls"]
                if body.get("output"):
                    for item in body["output"]:
                        self.item_sent(item)
                else:
                    self.remember(rid, deepcopy(message))
                status = body.get("status", "unknown")
                run.end(outputs={"messages": [message]}, metadata={"response_status": status},
                        error="realtime_response_failed" if status == "failed" else None)
                run.patch(exclude_inputs=False)
                root = response["root"]
                if root.end_time is not None:
                    root.outputs["messages"] = [message]
                    root.patch(exclude_inputs=False)
                if not response["calls"] or status != "completed":
                    self.finish(root, status, [message])
                self.responses.pop(rid)

    @observational
    def tool_start(self, turn_id, call_id, name, arguments):
        root = self.turns.get(turn_id) or self.turns.get(self.current)
        if root is None:
            return None
        run = root.create_child(name or "unknown_tool", run_type="tool", inputs={"arguments": arguments},
                                extra={"metadata": {"call_id": call_id}})
        run.post()
        return run

    @observational
    def tool_end(self, run, result):
        if run is not None:
            run.end(outputs=result if isinstance(result, dict) else {"output": result})
            run.patch()

    @observational
    def injected(self, items):
        root = self.turns.get(self.current)
        if root is not None and root.end_time is None:
            return
        messages = []
        for item in items:
            if item.get("type") == "function_call_output":
                messages.append({"role": "tool", "tool_call_id": item["call_id"], "content": item["output"]})
            elif item.get("type") == "message":
                messages.append({"role": item["role"], "content": item["content"]})
        if messages:
            self.start("notification-" + uuid4().hex, messages)

    @observational
    def interrupt(self):
        self.finish(self.turns.get(self.current), "interrupted")

    @observational
    def close(self):
        for response in self.responses.values():
            response["run"].end(outputs={"status": "connection_closed"})
            response["run"].patch()
        self.responses.clear()
        for root in self.turns.values():
            self.finish(root, "connection_closed")

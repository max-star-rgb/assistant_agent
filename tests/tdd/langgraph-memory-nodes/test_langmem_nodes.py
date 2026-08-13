from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest
from langgraph.store.memory import InMemoryStore

from assistant_agent.memory.backends.langmem import (
    LangMemConfigurationError,
    build_langmem_memory_bundle,
    create_langmem_memory_bundle,
    langmem_namespace,
)
from assistant_agent.memory.commit_ledger import SQLiteMemoryCommitLedger
from assistant_agent.runtime.assistant_graph_state import (
    assistant_turn_state_from_request,
)
from assistant_agent.runtime.requests import UserRequest


class FakeLangMemManager:
    def __init__(self, store: InMemoryStore) -> None:
        self.store = store
        self.calls = []

    def invoke(self, value, *, config):
        self.calls.append((value, config))
        user_id = config["configurable"]["langgraph_user_id"]
        self.store.put(
            ("assistant_agent", user_id),
            "learned",
            {"kind": "Memory", "content": {"content": "learned preference"}},
        )
        return {"status": "ok"}


def _runtime(store, kind="invoke"):
    return SimpleNamespace(
        store=store,
        context=SimpleNamespace(invocation_kind=kind, refresh_memory=False),
    )


def _completed_state():
    state = assistant_turn_state_from_request(
        UserRequest(
            user_id="trusted-user",
            session_id="trusted-session",
            text="I prefer concise answers",
        ),
        run_id="origin-run",
        trace_id="trace-1",
        agent_id="agent-1",
    )
    state["run"]["status"] = "completed"
    state["final_response"] = {
        "message": "I will keep it concise.",
        "followup_question": None,
        "output_refs": [],
        "citations": [],
    }
    state["response_publish"] = {
        "status": "published",
        "final_fact_id": "fact-1",
        "issue_code": None,
    }
    return state


def _bundle(tmp_path, store, manager):
    return build_langmem_memory_bundle(
        manager=manager,
        store=store,
        ledger=SQLiteMemoryCommitLedger(tmp_path / "memory.sqlite3"),
    )


def test_langmem_recall_reads_runtime_store_and_normalizes_items(tmp_path) -> None:
    store = InMemoryStore()
    manager = FakeLangMemManager(store)
    namespace = langmem_namespace(user_id="trusted-user", agent_id="agent-1")
    store.put(
        namespace,
        "profile",
        {"kind": "Memory", "content": {"content": "prefers concise answers"}},
    )
    bundle = _bundle(tmp_path, store, manager)

    recalled = bundle.recall_node(_completed_state(), _runtime(store))

    assert bundle.store is store
    assert recalled["memory_context"]["status"] == "ready"
    assert recalled["memory_context"]["items"][0]["text"] == ("prefers concise answers")
    assert recalled["memory_context"]["items"][0]["source"] == "langmem"


def test_langmem_commit_uses_manager_and_durable_ledger_once(tmp_path) -> None:
    store = InMemoryStore()
    manager = FakeLangMemManager(store)
    bundle = _bundle(tmp_path, store, manager)

    first = bundle.commit_node(_completed_state(), _runtime(store))
    duplicate = bundle.commit_node(_completed_state(), _runtime(store, "resume"))

    assert first["memory_commit"]["status"] == "succeeded"
    assert duplicate["memory_commit"] == first["memory_commit"]
    assert len(manager.calls) == 1
    messages = manager.calls[0][0]["messages"]
    assert messages == [
        {"role": "user", "content": "I prefer concise answers"},
        {"role": "assistant", "content": "I will keep it concise."},
    ]
    configured_id = manager.calls[0][1]["configurable"]["langgraph_user_id"]
    assert "trusted-user" not in configured_id


def test_langmem_nodes_fail_closed_when_runtime_store_differs(tmp_path) -> None:
    store = InMemoryStore()
    other_store = InMemoryStore()
    bundle = _bundle(tmp_path, store, FakeLangMemManager(store))

    with pytest.raises(LangMemConfigurationError, match="runtime.store"):
        bundle.recall_node(_completed_state(), _runtime(other_store))


def test_langmem_recall_degrades_without_leaking_store_error(tmp_path) -> None:
    class BrokenStore(InMemoryStore):
        def search(self, *args, **kwargs):
            raise RuntimeError("private store failure")

    store = BrokenStore()
    bundle = _bundle(tmp_path, store, FakeLangMemManager(store))

    recalled = bundle.recall_node(_completed_state(), _runtime(store))

    assert recalled["memory_context"]["status"] == "degraded"
    assert recalled["memory_context"]["issue_codes"] == ["langmem_recall_failed"]
    assert "private store failure" not in str(recalled)


def test_explicit_langmem_configuration_fails_closed_when_package_missing(
    tmp_path, monkeypatch
) -> None:
    real_import = importlib.import_module

    def import_without_langmem(name, package=None):
        if name == "langmem":
            raise ModuleNotFoundError("langmem intentionally absent")
        return real_import(name, package)

    monkeypatch.setattr(importlib, "import_module", import_without_langmem)

    with pytest.raises(LangMemConfigurationError, match="optional dependency"):
        create_langmem_memory_bundle(
            model="fake:model",
            store=InMemoryStore(),
            ledger=SQLiteMemoryCommitLedger(tmp_path / "memory.sqlite3"),
        )

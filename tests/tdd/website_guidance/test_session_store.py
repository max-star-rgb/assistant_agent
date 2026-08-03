from __future__ import annotations

import base64

import pytest

from assistant_agent.tools.plugins.builtin.website_guidance.session_store import (
    BrowserElementDescriptor,
    BrowserExplorationStore,
)


class _Clock:
    def __init__(self) -> None:
        self.value = 10.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _first_descriptor() -> BrowserElementDescriptor:
    return BrowserElementDescriptor(
        ref="e1",
        kind="navigate",
        role="link",
        name="First",
        href="https://public.example/first",
    )


def test_create_returns_a_128_bit_opaque_id_owned_by_its_run_and_session() -> None:
    store = BrowserExplorationStore()

    first = store.create(
        run_id="run-a",
        session_id="session-a",
        start_url="https://public.example/",
    )
    second = store.create(
        run_id="run-a",
        session_id="session-a",
        start_url="https://public.example/next",
    )

    assert len(base64.urlsafe_b64decode(first.browser_session_id + "==")) >= 16
    assert first.browser_session_id != second.browser_session_id
    assert store.get_owned(
        first.browser_session_id,
        run_id="run-a",
        session_id="session-a",
    ) == first


def test_get_and_append_reject_cross_run_or_session_access() -> None:
    store = BrowserExplorationStore()
    record = store.create(
        run_id="run-a",
        session_id="session-a",
        start_url="https://public.example/",
    )

    assert (
        store.get_owned(
            record.browser_session_id,
            run_id="run-b",
            session_id="session-a",
        )
        is None
    )
    assert (
        store.get_owned(
            record.browser_session_id,
            run_id="run-a",
            session_id="session-b",
        )
        is None
    )
    assert (
        store.append_action(
            record.browser_session_id,
            run_id="run-b",
            session_id="session-a",
            action="click",
            element_ref="e1",
            snapshot_version=2,
            selected_element=_first_descriptor(),
        )
        is None
    )
    assert store.get_owned(
        record.browser_session_id,
        run_id="run-a",
        session_id="session-a",
    ).actions == ()


def test_records_expire_after_the_configured_ttl() -> None:
    clock = _Clock()
    store = BrowserExplorationStore(ttl_seconds=30, clock=clock)
    record = store.create(
        run_id="run-a",
        session_id="session-a",
        start_url="https://public.example/",
    )

    clock.advance(30)

    assert (
        store.get_owned(
            record.browser_session_id,
            run_id="run-a",
            session_id="session-a",
        )
        is None
    )


def test_append_action_returns_new_snapshot_with_only_safe_navigation_facts() -> None:
    store = BrowserExplorationStore()
    record = store.create(
        run_id="run-a",
        session_id="session-a",
        start_url="https://public.example/",
    )

    updated = store.append_action(
        record.browser_session_id,
        run_id="run-a",
        session_id="session-a",
        action="click",
        element_ref="e1",
        snapshot_version=2,
        selected_element=_first_descriptor(),
    )

    assert record.actions == ()
    assert updated is not None
    assert updated.actions[0].action == "click"
    assert updated.actions[0].element_ref == "e1"
    assert updated.actions[0].snapshot_version == 2
    with pytest.raises(ValueError):
        store.append_action(
            record.browser_session_id,
            run_id="run-a",
            session_id="session-a",
            action="navigate",
            element_ref=None,
            snapshot_version=3,
        )


def test_delete_run_removes_only_that_runs_records() -> None:
    store = BrowserExplorationStore()
    deleted_first = store.create(
        run_id="run-a",
        session_id="session-a",
        start_url="https://public.example/one",
    )
    deleted_second = store.create(
        run_id="run-a",
        session_id="session-b",
        start_url="https://public.example/two",
    )
    retained = store.create(
        run_id="run-b",
        session_id="session-a",
        start_url="https://public.example/three",
    )

    assert store.delete_run("run-a") == 2
    assert (
        store.get_owned(
            deleted_first.browser_session_id,
            run_id="run-a",
            session_id="session-a",
        )
        is None
    )
    assert (
        store.get_owned(
            deleted_second.browser_session_id,
            run_id="run-a",
            session_id="session-b",
        )
        is None
    )
    assert store.get_owned(
        retained.browser_session_id,
        run_id="run-b",
        session_id="session-a",
    ) == retained


def test_store_binds_click_ref_to_displayed_safe_descriptor_and_next_snapshot() -> None:
    first = BrowserElementDescriptor(
        ref="e1",
        kind="navigate",
        role="link",
        name="First",
        href="https://public.example/first",
    )
    changed = BrowserElementDescriptor(
        ref="e1",
        kind="navigate",
        role="link",
        name="Changed",
        href="https://public.example/changed",
    )
    store = BrowserExplorationStore()
    record = store.create(
        run_id="run-a",
        session_id="session-a",
        start_url="https://public.example/",
        snapshot_url="https://public.example/first",
        snapshot_elements=(first,),
    )

    updated = store.append_action(
        record.browser_session_id,
        run_id="run-a",
        session_id="session-a",
        action="click",
        element_ref="e1",
        selected_element=first,
        snapshot_url="https://public.example/changed",
        snapshot_elements=(changed,),
        snapshot_version=2,
    )

    assert record.snapshot_elements == (first,)
    assert record.snapshot_url == "https://public.example/first"
    assert updated is not None
    assert updated.actions[0].selected_element == first
    assert updated.snapshot_elements == (changed,)
    assert updated.snapshot_url == "https://public.example/changed"
    with pytest.raises(ValueError):
        BrowserElementDescriptor(
            ref="e1",
            kind="expand",
            role="button",
            name="Mismatch",
            href="https://public.example/not-allowed",
        )

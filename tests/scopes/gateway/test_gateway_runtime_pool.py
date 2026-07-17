from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from threading import Event, Lock
from typing import Any

import pytest

from assistant_agent.gateway.runtime_pool import GatewayRuntimePool


@dataclass(frozen=True)
class FakeRuntime:
    runtime_id: int


def test_runtime_pool_reuses_idle_runtime() -> None:
    created: list[FakeRuntime] = []

    def runtime_factory() -> FakeRuntime:
        runtime = FakeRuntime(len(created) + 1)
        created.append(runtime)
        return runtime

    def run_request(request: Any, *, runtime: FakeRuntime, **kwargs: Any) -> int:
        return runtime.runtime_id

    pool = GatewayRuntimePool(
        max_runtime_instances=3,
        runtime_factory=runtime_factory,
        run_request=run_request,
    )
    try:
        assert pool.run_request(object()) == 1
        assert pool.run_request(object()) == 1
        assert pool.created_count == 1
        assert pool.idle_count == 1
    finally:
        pool.close()


def test_runtime_pool_bounds_concurrent_runtime_creation() -> None:
    created: list[FakeRuntime] = []
    lock = Lock()
    active = 0
    max_active = 0
    two_active = Event()
    release = Event()

    def runtime_factory() -> FakeRuntime:
        runtime = FakeRuntime(len(created) + 1)
        created.append(runtime)
        return runtime

    def run_request(request: Any, *, runtime: FakeRuntime, **kwargs: Any) -> int:
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
            if active == 2:
                two_active.set()
        try:
            assert release.wait(timeout=1.0)
            return runtime.runtime_id
        finally:
            with lock:
                active -= 1

    pool = GatewayRuntimePool(
        max_runtime_instances=2,
        runtime_factory=runtime_factory,
        run_request=run_request,
    )
    try:
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(pool.run_request, object()) for _ in range(4)]
            assert two_active.wait(timeout=1.0)
            assert pool.created_count == 2
            assert max_active == 2
            release.set()
            results = [future.result(timeout=1.0) for future in futures]
    finally:
        pool.close()

    assert set(results) <= {1, 2}
    assert len(created) == 2


def test_runtime_pool_close_cleans_created_runtimes_once() -> None:
    created: list[FakeRuntime] = []
    cleaned: list[int] = []

    def runtime_factory() -> FakeRuntime:
        runtime = FakeRuntime(len(created) + 1)
        created.append(runtime)
        return runtime

    def run_request(request: Any, *, runtime: FakeRuntime, **kwargs: Any) -> int:
        return runtime.runtime_id

    pool = GatewayRuntimePool(
        max_runtime_instances=2,
        runtime_factory=runtime_factory,
        run_request=run_request,
        runtime_cleanup=lambda runtime: cleaned.append(runtime.runtime_id),
    )

    assert pool.run_request(object()) == 1
    pool.close()
    pool.close()

    assert cleaned == [1]


def test_runtime_pool_rejects_invalid_instance_limits() -> None:
    for value in (0, True, 1.5, "2"):
        with pytest.raises(ValueError, match="max_runtime_instances must be a positive integer"):
            GatewayRuntimePool(max_runtime_instances=value)  # type: ignore[arg-type]

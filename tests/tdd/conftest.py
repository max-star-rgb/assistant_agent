from __future__ import annotations

import os


PROVIDER_MODE_ENV = "MULTIMODAL_AGENT_PROVIDER_MODE"


def pytest_configure(config) -> None:
    os.environ[PROVIDER_MODE_ENV] = "mock"

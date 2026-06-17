import pytest

from multimodal_agent.runtime_profile import (
    DEFAULT_RUNTIME_PROFILE,
    PROFILE_ENV_VAR,
    RUNTIME_PROFILES,
    RuntimeProfile,
    RuntimeProfileError,
    get_runtime_profile,
)


def test_default_runtime_profile_is_local_demo() -> None:
    profile = RuntimeProfile.from_env({})

    assert DEFAULT_RUNTIME_PROFILE == "local_demo"
    assert profile.name == "local_demo"
    assert profile.allows_real_providers is False
    assert profile.allows_network_provider_calls is False
    assert profile.requires_explicit_provider_config is False
    assert profile.default_provider_mode == "mock"


def test_runtime_profiles_define_expected_boundaries() -> None:
    assert set(RUNTIME_PROFILES) == {"local_demo", "offline_eval", "provider_smoke", "pilot"}
    assert RUNTIME_PROFILES["offline_eval"].allows_real_providers is False
    assert RUNTIME_PROFILES["offline_eval"].default_provider_mode == "mock"
    assert RUNTIME_PROFILES["provider_smoke"].allows_real_providers is True
    assert RUNTIME_PROFILES["provider_smoke"].requires_explicit_provider_config is True
    assert RUNTIME_PROFILES["provider_smoke"].default_provider_mode == "explicit"
    assert RUNTIME_PROFILES["pilot"].allows_network_provider_calls is True
    assert RUNTIME_PROFILES["pilot"].requires_explicit_provider_config is True


def test_runtime_profile_reads_environment_value() -> None:
    profile = RuntimeProfile.from_env({PROFILE_ENV_VAR: "offline_eval"})

    assert profile.name == "offline_eval"
    assert profile.allows_real_providers is False


def test_runtime_profile_rejects_unknown_value_with_clear_error() -> None:
    with pytest.raises(RuntimeProfileError) as exc_info:
        get_runtime_profile("production")

    message = str(exc_info.value)
    assert PROFILE_ENV_VAR in message
    assert "production" in message
    assert "local_demo" in message
    assert "provider_smoke" in message

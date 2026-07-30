"""Offline integration coverage for the FlyAI lodging provider boundary."""

from __future__ import annotations

import json
import subprocess
from datetime import date

import pytest

from assistant_agent.config import ProviderConfig
from assistant_agent.tools.plugins.builtin.lodging.backend import (
    FlyAILodgingSearchAdapter,
)
from assistant_agent.tools.plugins.builtin.lodging.models import LodgingSearchRequest
from assistant_agent.tools.plugins.builtin.lodging.plugin import LodgingToolPlugin
from assistant_agent.tools.plugins.contracts import ToolPluginContext


def _request() -> LodgingSearchRequest:
    return LodgingSearchRequest(
        destination="杭州",
        check_in=date(2026, 8, 1),
        check_out=date(2026, 8, 3),
        keywords="安静",
        nearby_poi="西湖",
        hotel_types=["酒店"],
        star_ratings=[4, 5],
        bed_types=["大床房"],
        max_nightly_price=800,
        sort="rate_desc",
    )


def _success_payload() -> dict[str, object]:
    return {
        "status": 0,
        "message": "success",
        "systemMessage": "价格和库存以飞猪页面为准",
        "data": {
            "itemList": [
                {
                    "address": "环城西路2号",
                    "brandName": "雷迪森",
                    "decorationTime": "2014",
                    "interestsPoi": "近杭州西湖风景名胜区",
                    "latitude": "30.259204",
                    "longitude": "120.159246",
                    "mainPic": "https://images.example.test/wanghu.jpg",
                    "detailUrl": "https://hotels.example.test/wanghu",
                    "name": "杭州望湖宾馆",
                    "price": "¥618",
                    "review": "西湖边的位置，家庭出游首选",
                    "score": "5.0",
                    "scoreDesc": "超棒",
                    "shId": "10021423",
                    "star": "豪华型",
                }
            ]
        },
    }


def test_flyai_adapter_normalizes_official_hotel_result_and_booking_link() -> None:
    commands: list[list[str]] = []

    def runner(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(_success_payload(), ensure_ascii=False),
            stderr="",
        )

    result = FlyAILodgingSearchAdapter(
        cli_path="/opt/flyai/bin/flyai",
        timeout_seconds=12,
        runner=runner,
    ).search(_request())

    assert result.success is True
    assert result.provider == "flyai"
    assert "价格和库存以飞猪页面为准" in result.provider_notice
    assert "入住人数和房型" in result.provider_notice
    assert len(result.offers) == 1
    assert result.offers[0].property_name == "杭州望湖宾馆"
    assert result.offers[0].nightly_price == 618
    assert result.offers[0].total_price == 1236
    assert result.offers[0].price_basis == "nightly_estimate"
    assert result.offers[0].booking_url == "https://hotels.example.test/wanghu"
    assert result.offers[0].latitude == 30.259204
    assert result.offers[0].refundable is None
    assert commands == [
        [
            "/opt/flyai/bin/flyai",
            "search-hotel",
            "--dest-name",
            "杭州",
            "--check-in-date",
            "2026-08-01",
            "--check-out-date",
            "2026-08-03",
            "--key-words",
            "安静",
            "--poi-name",
            "西湖",
            "--hotel-types",
            "酒店",
            "--hotel-stars",
            "4,5",
            "--hotel-bed-types",
            "大床房",
            "--max-price",
            "800",
            "--sort",
            "rate_desc",
        ]
    ]


def test_flyai_adapter_normalizes_masked_experience_price_to_lower_bound() -> None:
    payload = _success_payload()
    payload["data"]["itemList"][0]["price"] = "¥4xx"

    result = FlyAILodgingSearchAdapter(
        cli_path="/opt/flyai/bin/flyai",
        runner=lambda command, **_kwargs: subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(payload, ensure_ascii=False),
            stderr="",
        ),
    ).search(_request())

    assert result.success is True
    assert result.offers[0].nightly_price == 400
    assert result.offers[0].total_price == 800


@pytest.mark.parametrize(
    ("runner_result", "expected_code"),
    [
        (
            subprocess.CompletedProcess(
                ["flyai"],
                2,
                stdout="",
                stderr="upstream unavailable",
            ),
            "provider_unavailable",
        ),
        (
            subprocess.CompletedProcess(
                ["flyai"],
                0,
                stdout="{not-json",
                stderr="",
            ),
            "provider_bad_response",
        ),
        (
            subprocess.CompletedProcess(
                ["flyai"],
                0,
                stdout=json.dumps(
                    {"status": 1, "message": "query rejected", "data": {}}
                ),
                stderr="",
            ),
            "provider_unavailable",
        ),
    ],
)
def test_flyai_adapter_returns_structured_failures(
    runner_result: subprocess.CompletedProcess[str],
    expected_code: str,
) -> None:
    result = FlyAILodgingSearchAdapter(
        cli_path="/opt/flyai/bin/flyai",
        runner=lambda *_args, **_kwargs: runner_result,
    ).search(_request())

    assert result.success is False
    assert result.error_code == expected_code
    assert result.error_message
    assert result.offers == []


def test_flyai_adapter_normalizes_timeout() -> None:
    def timeout_runner(
        *_args: object, **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=["flyai"], timeout=5)

    result = FlyAILodgingSearchAdapter(
        cli_path="/opt/flyai/bin/flyai",
        timeout_seconds=5,
        runner=timeout_runner,
    ).search(_request())

    assert result.success is False
    assert result.error_code == "provider_timeout"


def test_flyai_adapter_rejects_non_http_booking_links() -> None:
    payload = _success_payload()
    payload["data"]["itemList"][0]["detailUrl"] = "javascript:alert(1)"

    result = FlyAILodgingSearchAdapter(
        cli_path="/opt/flyai/bin/flyai",
        runner=lambda command, **_kwargs: subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(payload, ensure_ascii=False),
            stderr="",
        ),
    ).search(_request())

    assert result.success is False
    assert result.error_code == "provider_bad_response"


def test_real_lodging_plugin_registers_only_complete_flyai_configuration(
    tmp_path,
) -> None:
    cli_path = tmp_path / "flyai"
    cli_path.write_text("#!/bin/sh\n", encoding="utf-8")
    cli_path.chmod(0o700)
    configured = LodgingToolPlugin().build_tools(
        ToolPluginContext(
            config=ProviderConfig(
                provider_mode="real",
                chat_provider="openai",
                chat_adapter_kind="openai",
                openai_api_key="test-only",
                lodging_provider="flyai",
                flyai_cli_path=str(cli_path),
            ),
            mcp_server_configs=[],
        )
    )
    unconfigured = LodgingToolPlugin().build_tools(
        ToolPluginContext(
            config=ProviderConfig(
                provider_mode="real",
                chat_provider="openai",
                chat_adapter_kind="openai",
                openai_api_key="test-only",
                lodging_provider="flyai",
                flyai_cli_path=None,
            ),
            mcp_server_configs=[],
        )
    )
    missing_executable = LodgingToolPlugin().build_tools(
        ToolPluginContext(
            config=ProviderConfig(
                provider_mode="real",
                chat_provider="openai",
                chat_adapter_kind="openai",
                openai_api_key="test-only",
                lodging_provider="flyai",
                flyai_cli_path="/missing/flyai",
            ),
            mcp_server_configs=[],
        )
    )

    assert [tool.name for tool in configured] == ["lodging_search"]
    assert unconfigured == []
    assert missing_executable == []


def test_lodging_provider_environment_is_real_only_and_explicit() -> None:
    real = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_PROVIDER_MODE": "real",
            "MULTIMODAL_AGENT_CHAT_PROVIDER": "openai",
            "OPENAI_API_KEY": "test-only",
            "MULTIMODAL_AGENT_LODGING_PROVIDER": "flyai",
            "FLYAI_CLI_PATH": "/opt/flyai/bin/flyai",
            "FLYAI_TIMEOUT_SECONDS": "15",
        }
    )
    mock = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_PROVIDER_MODE": "mock",
            "MULTIMODAL_AGENT_LODGING_PROVIDER": "flyai",
            "FLYAI_CLI_PATH": "/opt/flyai/bin/flyai",
        }
    )

    assert real.lodging_provider == "flyai"
    assert real.flyai_cli_path == "/opt/flyai/bin/flyai"
    assert real.flyai_timeout_seconds == 15
    assert mock.lodging_provider == "mock"

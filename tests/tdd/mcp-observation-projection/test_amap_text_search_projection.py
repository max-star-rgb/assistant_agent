from assistant_agent.mcp.config import MCPServerConfig
from assistant_agent.mcp.sdk_client import _tool_result_from_sdk_response


def _server(name: str = "amap_maps") -> MCPServerConfig:
    return MCPServerConfig(
        server_name=name,
        command=["mcp-sentinel"],
        allowed_tools=["maps_text_search"],
        read_only_tools=["maps_text_search"],
    )


def _response() -> dict:
    return {
        "structuredContent": {
            "pois": [
                {
                    "id": f"poi-{index}",
                    "name": f"name-{index}",
                    "address": f"address-{index}",
                    "location": f"120.{index},30.{index}",
                    "typecode": "100100",
                    "photos": [{"url": f"https://img.invalid/{index}.jpg"}],
                }
                for index in range(20)
            ]
        },
        "content": [],
        "isError": False,
    }


def test_amap_text_search_projects_a_bounded_complete_poi_view() -> None:
    result = _tool_result_from_sdk_response(
        server=_server(),
        tool_name="maps_text_search",
        namespaced_tool_name="mcp.amap_maps.maps_text_search",
        response=_response(),
    )

    assert result.success is True
    assert result.model_observation == {
        "pois": [
            {
                "id": f"poi-{index}",
                "name": f"name-{index}",
                "address": f"address-{index}",
                "location": f"120.{index},30.{index}",
                "typecode": "100100",
            }
            for index in range(5)
        ],
        "total_count": 20,
        "returned_count": 5,
        "truncated": True,
    }
    assert len(result.data["structured_content"]["pois"]) == 20
    assert "photos" in result.data["structured_content"]["pois"][0]


def test_other_mcp_tools_keep_the_generic_structured_projection() -> None:
    response = _response()
    result = _tool_result_from_sdk_response(
        server=_server("other_server"),
        tool_name="maps_text_search",
        namespaced_tool_name="mcp.other_server.maps_text_search",
        response=response,
    )

    assert result.model_observation == response["structuredContent"]

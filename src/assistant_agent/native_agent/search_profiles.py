"""Closed provider-native search policies for planning workers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from assistant_agent.native_agent.models import ProviderSearchProfile


class SearchProfileCapabilityError(ValueError):
    """Raised before a request when a search profile is not supported."""


@dataclass(frozen=True)
class SearchProfilePolicy:
    """Provider-native search configuration selected from a trusted registry."""

    profile: ProviderSearchProfile
    enable_search: bool
    search_strategy: Literal["turbo"] | None
    forced_search: bool
    assigned_site_list: tuple[str, ...]
    prompt_intervene: str | None


_POLICIES: dict[ProviderSearchProfile, SearchProfilePolicy] = {
    "none": SearchProfilePolicy("none", False, None, False, (), None),
    "rail_official": SearchProfilePolicy(
        "rail_official",
        True,
        "turbo",
        True,
        ("12306.cn",),
        "仅检索中国铁路12306官方公开信息",
    ),
    "guide_xiaohongshu": SearchProfilePolicy(
        "guide_xiaohongshu",
        True,
        "turbo",
        True,
        ("xiaohongshu.com",),
        "仅检索公开可索引的小红书旅行内容",
    ),
    "flight_official": SearchProfilePolicy(
        "flight_official",
        True,
        "turbo",
        True,
        ("caac.gov.cn",),
        "仅检索民航主管部门、机场或航空公司的官方公开信息",
    ),
    "guide_official": SearchProfilePolicy(
        "guide_official",
        True,
        "turbo",
        True,
        (),
        "仅检索文旅主管部门或景区运营主体的官方公开信息",
    ),
    "travel_general": SearchProfilePolicy(
        "travel_general",
        True,
        "turbo",
        True,
        (),
        "仅检索与用户目的地和日期直接相关的公开旅行信息",
    ),
}


def resolve_search_profile(
    profile: str,
    *,
    protocol: str,
    model_name: str,
) -> SearchProfilePolicy:
    """Resolve a trusted profile after validating provider capability boundaries."""

    del model_name
    if protocol != "dashscope":
        raise SearchProfileCapabilityError(
            "provider search profiles require the dashscope protocol"
        )

    try:
        policy = _POLICIES[profile]  # type: ignore[index]
    except KeyError as error:
        raise SearchProfileCapabilityError(
            f"unsupported provider search profile: {profile}"
        ) from error

    if len(policy.assigned_site_list) > 25:
        raise SearchProfileCapabilityError(
            "provider search profiles support at most 25 assigned sites"
        )
    return policy


__all__ = [
    "SearchProfileCapabilityError",
    "SearchProfilePolicy",
    "resolve_search_profile",
]

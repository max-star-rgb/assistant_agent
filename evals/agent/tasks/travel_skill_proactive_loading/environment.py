"""Controlled Environment for proactive travel Skill loading."""

from __future__ import annotations

from datetime import date, datetime, timezone

from assistant_agent.tools.plugins.builtin.lodging.models import (
    LodgingOffer,
    LodgingSearchRequest,
    LodgingSearchResult,
)
from assistant_agent.tools.plugins.builtin.lodging.tool import LodgingSearchTool
from assistant_agent.tools.registry import ToolRegistry
from evals.agent.contracts import AssertionResult
from evals.agent.environment_base import ControlledTaskEnvironment
from evals.agent.grading import rule_assertion
from evals.agent.task_support import build_controlled_registry


OBSERVED_AT = datetime(2026, 7, 31, 8, 0, tzinfo=timezone.utc)


class _SimpleTravelLodgingAdapter:
    provider = "eval:controlled-travel-skill-lodging-v1"

    def search(self, request: LodgingSearchRequest) -> LodgingSearchResult:
        matches = (
            request.destination == "苏州"
            and request.check_in == date(2026, 8, 14)
            and request.check_out == date(2026, 8, 16)
            and request.adults == 1
            and request.rooms == 1
            and request.max_nightly_price == 600
        )
        offers = (
            [
                LodgingOffer(
                    offer_id="eval-suzhou-garden",
                    property_name="苏州园景酒店",
                    nightly_price=568.0,
                    total_price=1136.0,
                    currency="CNY",
                    price_basis="nightly_estimate",
                    refundable=True,
                    source_ref="eval://travel-skill/lodging/suzhou-garden",
                    address="苏州市姑苏区临顿路88号",
                    booking_url=(
                        "https://example.test/hotel/suzhou-garden"
                    ),
                )
            ]
            if matches
            else []
        )
        return LodgingSearchResult(
            success=True,
            provider=self.provider,
            offers=offers,
            observed_at=OBSERVED_AT,
            output_ref="eval://travel-skill/lodging/search",
            provider_notice=(
                "总价按展示每晚价乘2晚估算；库存、税费和退改条件"
                "以跳转后的 OTA 页面为准。"
            ),
        )


class TravelSkillProactiveLoadingEnvironment(ControlledTaskEnvironment):
    """Read-only lodging fixture that also requires the local Skill loader."""

    dependency_label = "controlled:travel-skill-proactive-loading-v1"
    tool_catalog_label = "default_complete_registry_with_skill_loader"

    def setup(self) -> None:
        self._adapter = _SimpleTravelLodgingAdapter()

    def required_successes(self) -> tuple[str, ...]:
        return ("load_skill", "lodging_search")

    def task_validation_checks(
        self,
        registry: ToolRegistry,
    ) -> dict[str, AssertionResult]:
        fixture = self._adapter.search(
            LodgingSearchRequest(
                destination="苏州",
                check_in=date(2026, 8, 14),
                check_out=date(2026, 8, 16),
                max_nightly_price=600,
            )
        )
        return {
            "skill_and_lodging_tools_registered": rule_assertion(
                {"load_skill", "lodging_search"}.issubset(registry.list()),
                f"registered_tools={registry.list()}",
                label="完整目录包含 Skill 加载与住宿工具",
            ),
            "controlled_lodging_fixture": rule_assertion(
                fixture.success
                and len(fixture.offers) == 1
                and fixture.offers[0].nightly_price == 568.0
                and fixture.offers[0].total_price == 1136.0
                and fixture.offers[0].price_basis == "nightly_estimate",
                (
                    "offers="
                    f"{[item.model_dump(mode='json') for item in fixture.offers]}"
                ),
                label="受控苏州住宿候选与价格口径完整",
            ),
            "isolated_state_boundary": rule_assertion(
                registry.get_spec("load_skill").category == "read"
                and registry.get_spec("lodging_search").category == "read",
                "writes=False, state=in-memory-per-run",
                label="Skill 与住宿工具均只读且状态隔离",
            ),
        }

    def build_registry(self) -> ToolRegistry:
        return build_controlled_registry(
            replacements={
                "lodging_search": LodgingSearchTool(self._adapter),
            }
        )

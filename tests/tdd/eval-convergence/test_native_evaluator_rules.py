from types import SimpleNamespace

from assistant_agent.observability.runtime_audit.online_evaluators import (
    configure_native_online_evaluators,
)


EVALUATOR_NAMES = [
    "assistant_agent.quality.response_quality",
    "assistant_agent.quality.grounding",
    "assistant_agent.quality.tool_result_quality",
    "assistant_agent.quality.memory_extraction",
    "assistant_agent.quality.memory_recall",
]


class Resource:
    def __init__(self, existing: list[object] | None = None) -> None:
        self.existing = list(existing or [])
        self.created: list[object] = []
        self.updated: list[tuple[str, dict[str, object]]] = []

    def list(self):
        return SimpleNamespace(data=self.existing)

    def create(self, *, request):
        self.created.append(request)

    def update(self, rule_id: str, **changes: object) -> None:
        self.updated.append((rule_id, changes))


def _client(*, evaluators: Resource, rules: Resource) -> SimpleNamespace:
    return SimpleNamespace(
        api=SimpleNamespace(
            unstable=SimpleNamespace(
                evaluators=evaluators,
                evaluation_rules=rules,
            )
        )
    )


def test_configuration_creates_live_and_experiment_rules_for_shared_evaluators() -> None:
    evaluators = Resource()
    rules = Resource()

    result = configure_native_online_evaluators(
        _client(evaluators=evaluators, rules=rules),
        apply=True,
        model_provider="qwen",
        model="qwen3.6-flash",
    )

    assert [request.name for request in evaluators.created] == EVALUATOR_NAMES
    assert [request.name for request in rules.created] == [
        "assistant_agent.quality.response_quality.live",
        "assistant_agent.quality.response_quality.experiment",
        "assistant_agent.quality.grounding.live",
        "assistant_agent.quality.grounding.experiment",
        "assistant_agent.quality.tool_result_quality.live",
        "assistant_agent.quality.memory_extraction.live",
        "assistant_agent.quality.memory_recall.live",
    ]
    assert [request.target.value for request in rules.created] == [
        "observation",
        "experiment",
        "observation",
        "experiment",
        "observation",
        "observation",
        "observation",
    ]
    assert all(request.enabled is True for request in rules.created)
    assert all(request.sampling == 1.0 for request in rules.created)
    assert result.created_evaluators == 5
    assert result.created_rules == 7


def test_reconcile_preserves_ui_owned_enabled_and_sampling_state() -> None:
    evaluators = Resource(
        [SimpleNamespace(name=name, scope="project") for name in EVALUATOR_NAMES]
    )
    existing_rules = [
        SimpleNamespace(
            id=f"rule-{index}",
            name=name,
            enabled=False,
            sampling=0.05,
        )
        for index, name in enumerate(
            [
                "assistant_agent.quality.response_quality.live",
                "assistant_agent.quality.response_quality.experiment",
                "assistant_agent.quality.grounding.live",
                "assistant_agent.quality.grounding.experiment",
                "assistant_agent.quality.tool_result_quality.live",
                "assistant_agent.quality.memory_extraction.live",
                "assistant_agent.quality.memory_recall.live",
            ]
        )
    ]
    rules = Resource(existing_rules)

    result = configure_native_online_evaluators(
        _client(evaluators=evaluators, rules=rules),
        apply=True,
        model_provider="qwen",
        model="qwen3.6-flash",
    )

    assert len(rules.updated) == 7
    assert all("enabled" not in changes for _, changes in rules.updated)
    assert all("sampling" not in changes for _, changes in rules.updated)
    assert result.existing_rules == 7
    assert result.updated_rules == 7


def test_canonical_pre_convergence_rules_are_renamed_to_live_rules() -> None:
    evaluators = Resource(
        [SimpleNamespace(name=name, scope="project") for name in EVALUATOR_NAMES]
    )
    rules = Resource(
        [
            SimpleNamespace(id=f"legacy-{index}", name=name)
            for index, name in enumerate(EVALUATOR_NAMES)
        ]
    )

    configure_native_online_evaluators(
        _client(evaluators=evaluators, rules=rules),
        apply=True,
        model_provider="qwen",
        model="qwen3.6-flash",
    )

    assert [changes["name"] for _, changes in rules.updated] == [
        f"{name}.live" for name in EVALUATOR_NAMES
    ]
    assert [request.name for request in rules.created] == [
        "assistant_agent.quality.response_quality.experiment",
        "assistant_agent.quality.grounding.experiment",
    ]

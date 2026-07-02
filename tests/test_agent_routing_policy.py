from assistant_agent.schemas.agent_communication import (
    DEFAULT_AGENT_ID,
    AgentDirectoryConfig,
    AgentInstance,
    AgentInstanceConfig,
)
from assistant_agent.agent_routing import AgentRouteRequest
from assistant_agent.services.agent_directory import AgentDirectory, default_agent_instance
from assistant_agent.services.agent_routing_policy import AgentRoutingPolicy


def test_routing_policy_explicit_target_wins_over_routing_table() -> None:
    directory = AgentDirectory(
        [
            default_agent_instance(),
            _instance("agent.worker", ["worker_specialist"]),
            _instance("agent.other", ["worker_specialist"]),
        ]
    )
    policy = AgentRoutingPolicy(routing_table={"worker_specialist": "agent.other"})

    decision = policy.resolve(
        AgentRouteRequest(
            user_id="u1",
            session_id="s1",
            text="route explicitly",
            target_agent_id="agent.worker",
            capability="worker_specialist",
        ),
        directory=directory,
    )

    assert decision.route.status == "routed"
    assert decision.route.instance is not None
    assert decision.route.instance.agent_id == "agent.worker"
    assert decision.reason == "explicit_target_agent_id"


def test_routing_policy_routes_unique_capability_match() -> None:
    directory = AgentDirectory(
        [
            default_agent_instance(),
            _instance("agent.worker", ["worker_specialist"]),
        ]
    )

    decision = AgentRoutingPolicy().resolve(
        AgentRouteRequest(
            user_id="u1",
            session_id="s1",
            text="route by capability",
            capability="worker_specialist",
        ),
        directory=directory,
    )

    assert decision.route.status == "routed"
    assert decision.route.instance is not None
    assert decision.route.instance.agent_id == "agent.worker"
    assert decision.reason == "capability_match"


def test_routing_policy_returns_ambiguous_capability_without_table() -> None:
    directory = AgentDirectory(
        [
            default_agent_instance(),
            _instance("agent.worker_a", ["worker_specialist"]),
            _instance("agent.worker_b", ["worker_specialist"]),
        ]
    )

    decision = AgentRoutingPolicy().resolve(
        AgentRouteRequest(
            user_id="u1",
            session_id="s1",
            text="ambiguous",
            capability="worker_specialist",
        ),
        directory=directory,
    )

    assert decision.route.status == "failed"
    assert decision.route.error is not None
    assert decision.route.error.code == "agent_route_ambiguous"
    assert decision.reason == "capability_match"


def test_routing_policy_routing_table_resolves_ambiguous_capability() -> None:
    directory = AgentDirectory(
        [
            default_agent_instance(),
            _instance("agent.worker_a", ["worker_specialist"]),
            _instance("agent.worker_b", ["worker_specialist"]),
        ]
    )
    policy = AgentRoutingPolicy(routing_table={"worker_specialist": "agent.worker_b"})

    decision = policy.resolve(
        AgentRouteRequest(
            user_id="u1",
            session_id="s1",
            text="route through table",
            capability="worker_specialist",
        ),
        directory=directory,
    )

    assert decision.route.status == "routed"
    assert decision.route.instance is not None
    assert decision.route.instance.agent_id == "agent.worker_b"
    assert decision.reason == "routing_table"


def test_routing_policy_routing_table_target_must_be_enabled() -> None:
    directory = AgentDirectory(
        [
            default_agent_instance(),
            _instance("agent.worker", ["worker_specialist"], enabled=False),
        ]
    )
    policy = AgentRoutingPolicy(routing_table={"worker_specialist": "agent.worker"})

    decision = policy.resolve(
        AgentRouteRequest(
            user_id="u1",
            session_id="s1",
            text="disabled",
            capability="worker_specialist",
        ),
        directory=directory,
    )

    assert decision.route.status == "failed"
    assert decision.route.error is not None
    assert decision.route.error.code == "agent_disabled"
    assert decision.reason == "routing_table"


def test_routing_policy_default_and_controller_fallbacks() -> None:
    directory = AgentDirectory()
    policy = AgentRoutingPolicy()

    default_decision = policy.resolve(
        AgentRouteRequest(user_id="u1", session_id="s1", text="default"),
        directory=directory,
    )
    controller_decision = policy.resolve(
        AgentRouteRequest(
            user_id="u1",
            session_id="s1",
            text="delegate",
            collaboration_mode="controller_delegate",
        ),
        directory=directory,
    )

    assert default_decision.route.status == "routed"
    assert default_decision.reason == "default_agent"
    assert default_decision.use_controller_runtime is False
    assert controller_decision.route.status == "routed"
    assert controller_decision.reason == "controller_delegate_default"
    assert controller_decision.use_controller_runtime is True


def test_directory_and_policy_can_be_built_from_config() -> None:
    config = AgentDirectoryConfig(
        instances=[
            AgentInstanceConfig(
                agent_id=DEFAULT_AGENT_ID,
                display_name="Default Agent",
                capabilities=["chat"],
                transports=["local"],
                metadata={"default": True},
            ),
            AgentInstanceConfig(
                agent_id="agent.worker",
                display_name="Worker Agent",
                capabilities=["worker_specialist"],
                transports=["local"],
            ),
        ],
        routing_table={"worker_specialist": "agent.worker"},
    )

    directory = AgentDirectory.from_config(config)
    policy = AgentRoutingPolicy.from_config(config)
    decision = policy.resolve(
        AgentRouteRequest(
            user_id="u1",
            session_id="s1",
            text="config route",
            capability="worker_specialist",
        ),
        directory=directory,
    )

    assert decision.route.status == "routed"
    assert decision.route.instance is not None
    assert decision.route.instance.agent_id == "agent.worker"
    assert decision.reason == "routing_table"


def _instance(agent_id: str, capabilities: list[str], *, enabled: bool = True) -> AgentInstance:
    return AgentInstance(
        agent_id=agent_id,
        display_name=agent_id,
        capabilities=capabilities,
        enabled=enabled,
        transports=["local"],
    )

"""Stable schema contracts for proactive wake services."""

from assistant_agent.schemas.proactive_wake import (
    AttentionDecision,
    AttentionOutcome,
    DeliveryResult,
    DeliveryStatus,
    NotificationEnvelope,
    ProactiveWakeRunResult,
    QuietHours,
    Severity,
    WakeAttentionSpec,
    WakeConditionMode,
    WakeConditionSpec,
    WakeDecision,
    WakeDecisionOutcome,
    WakeEvidence,
    WakeOwner,
    WakeProbeSpec,
    WakeRule,
    WakeRuleState,
    WakeRun,
    WakeRunStatus,
    WakeSignal,
    WakeSignalKind,
    WakeTriggerSpec,
)
from assistant_agent.services.proactive_wake.store import (
    ProactiveWakeStoreError,
    SQLiteProactiveWakeStore,
)
from assistant_agent.services.proactive_wake.change_detector import (
    build_wake_evidence,
    evidence_fingerprint,
)
from assistant_agent.services.proactive_wake.probe import (
    GovernedProbeRunner,
    ProbeObservation,
    ProactiveRuleValidation,
    ProactiveRuleValidator,
)
from assistant_agent.services.proactive_wake.policy import (
    AttentionPolicy,
    DeterministicWakeEvaluator,
    build_notification_envelope,
)
from assistant_agent.services.proactive_wake.activity import (
    GatewayUserActivityReader,
    NullUserActivityReader,
    UserActivityReader,
)
from assistant_agent.services.proactive_wake.coordinator import (
    ProactiveWakeCoordinator,
    ProactiveWakeError,
)
from assistant_agent.services.proactive_wake.delivery import (
    MockProactiveNotificationTransport,
    NotificationDeliveryWorker,
    ProactiveNotificationTransport,
)

__all__ = [
    "AttentionDecision",
    "AttentionOutcome",
    "AttentionPolicy",
    "DeliveryResult",
    "DeliveryStatus",
    "DeterministicWakeEvaluator",
    "GovernedProbeRunner",
    "GatewayUserActivityReader",
    "MockProactiveNotificationTransport",
    "NotificationEnvelope",
    "NotificationDeliveryWorker",
    "NullUserActivityReader",
    "ProbeObservation",
    "ProactiveWakeCoordinator",
    "ProactiveWakeError",
    "ProactiveWakeStoreError",
    "ProactiveNotificationTransport",
    "ProactiveWakeRunResult",
    "ProactiveRuleValidation",
    "ProactiveRuleValidator",
    "QuietHours",
    "SQLiteProactiveWakeStore",
    "Severity",
    "UserActivityReader",
    "WakeAttentionSpec",
    "WakeConditionMode",
    "WakeConditionSpec",
    "WakeDecision",
    "WakeDecisionOutcome",
    "WakeEvidence",
    "WakeOwner",
    "WakeProbeSpec",
    "WakeRule",
    "WakeRuleState",
    "WakeRun",
    "WakeRunStatus",
    "WakeSignal",
    "WakeSignalKind",
    "WakeTriggerSpec",
    "build_wake_evidence",
    "build_notification_envelope",
    "evidence_fingerprint",
]

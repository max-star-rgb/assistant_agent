import json

import pytest

from assistant_agent.services.agent_service_delivery import (
    AgentServiceDeliveryError,
    AgentServiceDeliveryRegistry,
    JsonlAgentServiceDeliveryAudit,
)


def test_delivery_moves_from_accepted_to_sent_to_acked(tmp_path) -> None:
    path = tmp_path / "delivery.jsonl"
    registry = AgentServiceDeliveryRegistry(JsonlAgentServiceDeliveryAudit(path))

    delivery = registry.accept("phone-private", "chat-private", expects_ack=True)
    registry.mark_processing(delivery.delivery_id)
    registry.mark_sent(delivery.delivery_id, run_id="run-1", trace_id="trace-1")
    acked = registry.ack(delivery.delivery_id, chat_index="chat-private")

    assert acked.status == "acked"
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert [record["event_type"] for record in records] == ["accepted", "processing", "sent", "acked"]
    assert records[-1]["run_id"] == "run-1"
    assert "phone-private" not in path.read_text()
    assert "chat-private" not in path.read_text()


def test_delivery_ack_rejects_unknown_duplicate_and_mismatch(tmp_path) -> None:
    registry = AgentServiceDeliveryRegistry(JsonlAgentServiceDeliveryAudit(tmp_path / "audit.jsonl"))
    delivery = registry.accept("s1", "chat-1", expects_ack=True)
    registry.mark_sent(delivery.delivery_id)

    with pytest.raises(AgentServiceDeliveryError, match="chatIndex mismatch"):
        registry.ack(delivery.delivery_id, chat_index="chat-2")
    registry.ack(delivery.delivery_id, chat_index="chat-1")
    with pytest.raises(AgentServiceDeliveryError, match="already acknowledged"):
        registry.ack(delivery.delivery_id, chat_index="chat-1")
    with pytest.raises(AgentServiceDeliveryError, match="unknown deliveryId"):
        registry.ack("missing", chat_index="chat-1")


@pytest.mark.parametrize(
    ("sent", "expected"),
    [(False, "disconnected_before_send"), (True, "disconnected_before_ack")],
)
def test_delivery_disconnect_state_distinguishes_send_boundary(tmp_path, sent, expected) -> None:
    registry = AgentServiceDeliveryRegistry(JsonlAgentServiceDeliveryAudit(tmp_path / "audit.jsonl"))
    delivery = registry.accept("s1", "chat-1", expects_ack=True)
    if sent:
        registry.mark_sent(delivery.delivery_id)

    disconnected = registry.mark_disconnected(delivery.delivery_id, close_code=1001, close_reason="client gone")

    assert disconnected.status == expected
    record = json.loads((tmp_path / "audit.jsonl").read_text().splitlines()[-1])
    assert record["close_code"] == 1001
    assert record["close_reason_category"] == "client_disconnect"


def test_delivery_audit_never_writes_response_content(tmp_path) -> None:
    path = tmp_path / "audit.jsonl"
    registry = AgentServiceDeliveryRegistry(JsonlAgentServiceDeliveryAudit(path))
    delivery = registry.accept("13800138000", "secret-chat", expects_ack=False)
    registry.mark_failed(delivery.delivery_id, error_code="send_failed")

    raw = path.read_text()
    assert "13800138000" not in raw
    assert "secret-chat" not in raw
    assert "response" not in raw.lower()

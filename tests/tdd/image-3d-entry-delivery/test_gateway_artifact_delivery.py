import asyncio


def test_delivery_hub_keeps_only_current_session_subscriber() -> None:
    from assistant_agent.gateway.artifact_delivery import (
        ArtifactCompleted,
        GatewayArtifactDeliveryHub,
    )

    delivered: list[tuple[str, str]] = []

    async def first_sender(event: ArtifactCompleted) -> None:
        delivered.append(("first", event.artifact_id))

    async def second_sender(event: ArtifactCompleted) -> None:
        delivered.append(("second", event.artifact_id))

    async def exercise() -> None:
        hub = GatewayArtifactDeliveryHub()
        await hub.register(
            session_id="session-sentinel",
            subscriber_id="connection-old",
            sender=first_sender,
        )
        await hub.register(
            session_id="session-sentinel",
            subscriber_id="connection-current",
            sender=second_sender,
        )

        # 旧连接的 finally 不能误删同 session 的新连接。
        await hub.unregister(
            session_id="session-sentinel",
            subscriber_id="connection-old",
        )
        delivered_now = await hub.publish(
            ArtifactCompleted(
                artifact_id="artifact-sentinel",
                user_id="user-sentinel",
                session_id="session-sentinel",
                media_type="glb",
                uri="http://3d-service/model.glb",
            )
        )

        assert delivered_now is True
        assert delivered == [("second", "artifact-sentinel")]

        await hub.unregister(
            session_id="session-sentinel",
            subscriber_id="connection-current",
        )
        assert (
            await hub.publish(
                ArtifactCompleted(
                    artifact_id="artifact-after-disconnect",
                    user_id="user-sentinel",
                    session_id="session-sentinel",
                    media_type="glb",
                    uri="http://3d-service/model.glb",
                )
            )
            is False
        )

    asyncio.run(exercise())

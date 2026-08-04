"""Prompt-safe capability metadata for Gateway entry adapters."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EntryAdapterCapabilities:
    """Small prompt-safe capability declaration for one Gateway entry adapter."""

    supports_text_streaming: bool = True
    supports_interrupt: bool = True
    supports_tts_state: bool = False
    supports_realtime_task_state: bool = False
    supports_audio_refs: bool = False
    supports_image_refs: bool = False
    supports_video_refs: bool = False
    supports_raw_media: bool = False
    supports_tts_edge_events: bool = False
    supports_semantic_interrupt: bool = False
    supports_shopping_detail_v1: bool = False
    supports_generated_media_delivery: bool = False

    def to_metadata(self) -> dict[str, bool]:
        return {
            "supports_text_streaming": self.supports_text_streaming,
            "supports_interrupt": self.supports_interrupt,
            "supports_tts_state": self.supports_tts_state,
            "supports_realtime_task_state": self.supports_realtime_task_state,
            "supports_audio_refs": self.supports_audio_refs,
            "supports_image_refs": self.supports_image_refs,
            "supports_video_refs": self.supports_video_refs,
            "supports_raw_media": self.supports_raw_media,
            "supports_tts_edge_events": self.supports_tts_edge_events,
            "supports_semantic_interrupt": self.supports_semantic_interrupt,
            "supports_shopping_detail_v1": self.supports_shopping_detail_v1,
            "supports_generated_media_delivery": self.supports_generated_media_delivery,
        }


GATEWAY_WEBSOCKET_CAPABILITIES = EntryAdapterCapabilities(
    supports_audio_refs=True,
    supports_image_refs=True,
    supports_video_refs=True,
    supports_shopping_detail_v1=True,
)

HTTP_AGENT_ENTRY_CAPABILITIES = EntryAdapterCapabilities(
    supports_image_refs=True,
    supports_video_refs=True,
    supports_shopping_detail_v1=True,
)

AGENT_SERVICE_ENTRY_CAPABILITIES = EntryAdapterCapabilities(
    supports_realtime_task_state=True,
    supports_video_refs=True,
    supports_raw_media=True,
    supports_shopping_detail_v1=True,
    supports_generated_media_delivery=True,
)

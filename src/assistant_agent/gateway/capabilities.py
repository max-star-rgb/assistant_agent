"""Prompt-safe capability metadata for Gateway entry adapters."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EntryAdapterCapabilities:
    """Small prompt-safe capability declaration for one Gateway entry adapter."""

    supports_text_streaming: bool = True
    supports_interrupt: bool = True
    supports_audio_refs: bool = False
    supports_image_refs: bool = False
    supports_video_refs: bool = False
    supports_raw_media: bool = False
    supports_tts_edge_events: bool = False

    def to_metadata(self) -> dict[str, bool]:
        return {
            "supports_text_streaming": self.supports_text_streaming,
            "supports_interrupt": self.supports_interrupt,
            "supports_audio_refs": self.supports_audio_refs,
            "supports_image_refs": self.supports_image_refs,
            "supports_video_refs": self.supports_video_refs,
            "supports_raw_media": self.supports_raw_media,
            "supports_tts_edge_events": self.supports_tts_edge_events,
        }


GATEWAY_WEBSOCKET_CAPABILITIES = EntryAdapterCapabilities(
    supports_audio_refs=True,
    supports_image_refs=True,
    supports_video_refs=True,
)

REALTIME_MEDIA_ENTRY_CAPABILITIES = EntryAdapterCapabilities(
    supports_audio_refs=True,
    supports_image_refs=True,
    supports_video_refs=True,
    supports_tts_edge_events=True,
)

AGENT_SERVICE_ENTRY_CAPABILITIES = EntryAdapterCapabilities(
    supports_text_streaming=False,
    supports_interrupt=False,
)

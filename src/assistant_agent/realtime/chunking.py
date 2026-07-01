"""Text chunking helpers for realtime final-response delivery."""

from __future__ import annotations


DEFAULT_RESPONSE_CHUNK_MAX_CHARS = 240


def chunk_response_text(
    text: str | None,
    *,
    max_chars: int = DEFAULT_RESPONSE_CHUNK_MAX_CHARS,
) -> list[str]:
    """Split completed response text into bounded chunks.

    This helper chunks already-composed final text for delivery convenience. It
    does not represent provider token streaming.
    """

    if max_chars < 1:
        msg = "max_chars must be greater than zero"
        raise ValueError(msg)

    if text is None:
        return []

    remaining = text.strip()
    if not remaining:
        return []

    chunks: list[str] = []
    cursor = 0
    text_length = len(remaining)
    while cursor < text_length:
        hard_end = min(cursor + max_chars, text_length)
        split_at = _find_split_point(remaining, cursor, hard_end)
        chunk = remaining[cursor:split_at].strip()
        if chunk:
            chunks.append(chunk)
        cursor = split_at
        while cursor < text_length and remaining[cursor].isspace():
            cursor += 1

    return chunks


def _find_split_point(text: str, start: int, hard_end: int) -> int:
    if hard_end >= len(text):
        return len(text)

    separators = (
        "\n\n",
        "\n",
        "。",
        "！",
        "？",
        ". ",
        "! ",
        "? ",
        "；",
        "; ",
        "，",
        ", ",
        " ",
    )
    for separator in separators:
        index = text.rfind(separator, start + 1, hard_end + 1)
        if index > start:
            return index + len(separator)

    return hard_end

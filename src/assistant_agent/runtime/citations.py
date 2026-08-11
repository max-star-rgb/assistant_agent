"""Provider-neutral URL citation annotations for terminal delivery."""

from __future__ import annotations

from collections.abc import Sequence
import re
from typing import Literal, Protocol
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, model_validator


_CITATION_PATTERN = re.compile(
    r"(?<!\[)\[(?:ref_)?(?P<index>[1-9][0-9]*)\](?!\()"
)


class CitationSource(Protocol):
    """Structural source contract accepted from provider adapters."""

    index: int
    title: str
    url: str


class UrlCitationAnnotation(BaseModel):
    """One URL citation occurrence inside an unchanged response string."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["url_citation"] = "url_citation"
    start_index: int = Field(ge=0)
    end_index: int = Field(gt=0)
    source_id: str = Field(pattern=r"^source_[1-9][0-9]*$")
    title: str = Field(min_length=1)
    url: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_span(self) -> "UrlCitationAnnotation":
        if self.end_index <= self.start_index:
            raise ValueError("end_index must be greater than start_index")
        return self


def build_url_citation_annotations(
    response_text: str,
    sources: Sequence[CitationSource],
) -> list[UrlCitationAnnotation]:
    """Map provider-owned inline markers to safe terminal URL annotations."""

    by_index = {
        source.index: source
        for source in sources
        if _is_public_web_url(source.url)
    }
    annotations: list[UrlCitationAnnotation] = []
    for match in _CITATION_PATTERN.finditer(response_text):
        index = int(match.group("index"))
        source = by_index.get(index)
        if source is None:
            continue
        annotations.append(UrlCitationAnnotation(
            start_index=match.start(),
            end_index=match.end(),
            source_id=f"source_{index}",
            title=source.title,
            url=source.url,
        ))
    return annotations


def _is_public_web_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc)

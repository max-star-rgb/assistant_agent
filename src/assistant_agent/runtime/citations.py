"""Provider-neutral URL citation annotations for terminal delivery."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


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

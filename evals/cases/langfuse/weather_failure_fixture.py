"""Controlled weather dependency failures for Langfuse Agent evals."""

from typing import Literal

from pydantic import BaseModel, Field

from assistant_agent.tools.plugins.builtin.calendar_weather_contacts.models import (
    WeatherRequest,
    WeatherResult,
)


class WeatherFailureFixture(BaseModel):
    """One hidden, deterministic weather dependency failure."""

    schema_version: Literal["weather_failure_v1"] = "weather_failure_v1"
    error_code: Literal["provider_timeout"]
    message: str = Field(min_length=1)
    provider: Literal["eval:simulated-weather"] = "eval:simulated-weather"
    recoverable: bool = True


class SimulatedWeatherFailureAdapter:
    """Return the approved failure through the production WeatherAdapter boundary."""

    location_input_language: Literal["any"] = "any"

    def __init__(self, fixture: WeatherFailureFixture) -> None:
        self.fixture = fixture

    def lookup(self, request: WeatherRequest) -> WeatherResult:
        return WeatherResult(
            success=False,
            location=request.location,
            query_used=request.location,
            forecast=[],
            summary=self.fixture.message,
            provider=self.fixture.provider,
            output_ref=(
                f"eval://weather/failed/{self.fixture.error_code}"
            ),
            errors=[
                {
                    "code": self.fixture.error_code,
                    "message": self.fixture.message,
                    "recoverable": self.fixture.recoverable,
                }
            ],
        )

"""Controlled Environment for missing weather location."""

from evals.agent.batch_cases import environment_type

WeatherMissingLocationEnvironment = environment_type(
    "weather_missing_location_clarification",
    "WeatherMissingLocationEnvironment",
)

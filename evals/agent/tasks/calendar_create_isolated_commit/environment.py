"""Temporary-SQLite calendar Environment."""

from evals.agent.batch_cases import environment_type

CalendarCreateEnvironment = environment_type(
    "calendar_create_isolated_commit",
    "CalendarCreateEnvironment",
)

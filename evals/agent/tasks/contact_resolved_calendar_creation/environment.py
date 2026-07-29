"""Controlled contacts plus temporary-SQLite Environment."""

from evals.agent.batch_cases import environment_type

ContactCalendarEnvironment = environment_type(
    "contact_resolved_calendar_creation",
    "ContactCalendarEnvironment",
)

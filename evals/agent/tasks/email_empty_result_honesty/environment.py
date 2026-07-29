"""Environment wrapper for empty email search honesty."""

from evals.agent.batch_cases import environment_type

EmailEmptyResultEnvironment = environment_type(
    "email_empty_result_honesty",
    "EmailEmptyResultEnvironment",
)

"""Controlled untrusted-email Environment."""

from evals.agent.batch_cases import environment_type

EmailInjectionEnvironment = environment_type(
    "email_prompt_injection_resistance",
    "EmailInjectionEnvironment",
)

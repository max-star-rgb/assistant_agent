"""Controlled frozen-memory Environment."""

from evals.agent.batch_cases import environment_type

MemoryPrecedenceEnvironment = environment_type(
    "memory_current_request_precedence",
    "MemoryPrecedenceEnvironment",
)

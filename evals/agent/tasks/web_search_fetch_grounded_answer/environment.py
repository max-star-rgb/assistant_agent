"""Controlled web search/fetch Environment."""

from evals.agent.batch_cases import environment_type

WebSearchFetchEnvironment = environment_type(
    "web_search_fetch_grounded_answer",
    "WebSearchFetchEnvironment",
)

"""Agent HTTP routes."""

from fastapi import APIRouter

from multimodal_agent.agent.runtime import AgentGraphRuntime
from multimodal_agent.schemas.api import AgentRunResponse, agent_run_response_from_state
from multimodal_agent.schemas.requests import UserRequest


router = APIRouter()


def get_agent_runtime() -> AgentGraphRuntime:
    return AgentGraphRuntime()


@router.post("/agent/run", response_model=AgentRunResponse)
def run_agent(request: UserRequest) -> AgentRunResponse:
    state = get_agent_runtime().run_state(request)
    return agent_run_response_from_state(state)

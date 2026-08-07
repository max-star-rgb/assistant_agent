"""Opaque identity binding retained for operator compatibility."""

from __future__ import annotations

import hashlib

from assistant_agent.identity import RequestIdentity
from assistant_agent.memory.mem0.models import Mem0Identity


def bind_mem0_identity(
    identity: RequestIdentity,
    *,
    namespace: str,
) -> Mem0Identity:
    """Map trusted application identity to opaque Mem0 identity filters."""

    if not identity.session_id:
        raise ValueError("session_id is required for Mem0 run identity")

    def digest(kind: str, value: str) -> str:
        payload = f"assistant_agent:{namespace}:{kind}:{value}".encode()
        return hashlib.sha256(payload).hexdigest()[:32]

    run_seed = "\x1f".join(
        (identity.user_id, identity.agent_id, identity.session_id)
    )
    return Mem0Identity(
        user_id=f"usr_{digest('user', identity.user_id)}",
        agent_id=f"agt_{digest('agent', identity.agent_id)}",
        run_id=f"run_{digest('run', run_seed)}",
    )

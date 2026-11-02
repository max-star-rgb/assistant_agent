"""Project declared Tool artifacts onto the current native LangSmith Tool run."""

import hashlib
import logging
import re
from collections.abc import Callable, Mapping
from typing import Any

from langsmith import get_current_run_tree
from langsmith.schemas import Attachment
from langsmith.utils import tracing_is_enabled

from assistant_agent.tools.delivery import read_tool_delivery


ArtifactResolver = Callable[[str], tuple[str, bytes] | None]
MAX_TRACE_ARTIFACT_BYTES = 25 * 1024 * 1024
_LOGGER = logging.getLogger(__name__)


def attach_tool_artifacts(artifact: Mapping[str, Any], resolver: ArtifactResolver) -> None:
    """Attach bounded outputs without changing business results or creating spans."""
    try:
        if tracing_is_enabled() is not True:
            return
        run = get_current_run_tree()
        if run is None or run.run_type != "tool" or run.extra.get("__disabled"):
            return
        client = run.client
        # LangSmith has no public per-run sampling/redaction predicate. Fail closed
        # if these SDK fields change; never bypass an output masking policy.
        if (
            getattr(client, "_hide_outputs", True) is not False
            or getattr(client, "_anonymizer", True) is not None
            or getattr(client, "_process_buffered_run_ops", True) is not None
            or run.trace_id in getattr(client, "_filtered_post_uuids", {run.trace_id})
            or run.replicas
        ):
            return
        delivery = read_tool_delivery(artifact)
        if delivery is None:
            return
        for ref in dict.fromkeys(delivery.output_refs):
            name = "output-" + hashlib.sha256(ref.encode()).hexdigest()[:20]
            if name in run.attachments:
                continue
            try:
                resolved = resolver(ref)
                if resolved is None:
                    continue
                mime_type, payload = resolved
                if (
                    not isinstance(payload, bytes)
                    or not 0 < len(payload) <= MAX_TRACE_ARTIFACT_BYTES
                    or not re.fullmatch(r"[\w.+-]+/[\w.+-]+", mime_type, flags=re.ASCII)
                ):
                    continue
                run.attachments[name] = Attachment(mime_type=mime_type, data=payload)
            except Exception:
                _LOGGER.warning("Tool artifact attachment skipped: resolution failed")
        # The native LangChain tracer patches this same run at on_tool_end.
    except Exception:
        _LOGGER.warning("Tool artifact attachment skipped: tracing unavailable")

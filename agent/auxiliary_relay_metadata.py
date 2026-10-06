"""Relay shim-facing codec metadata, separate from the actual provider wire route."""
from typing import Any, Dict, Optional

def _record_route_info(
    route_info: Optional[Dict[str, str]], provider: Optional[str], model: Optional[str]
) -> None:
    """Expose the concrete route selected for one auxiliary call."""
    if route_info is not None:
        route_info["provider"] = provider or "auto"
        route_info["model"] = model or "default"


def _relay_completion_metadata(client: Any, metadata: dict[str, Any]) -> dict[str, Any]:
    """Match Relay's codec to the shim-facing request/response, preserving wire identity.

    Codex auxiliary shims expose chat.completions: messages become Responses input
    only inside the provider callback, and Responses output becomes chat choices.
    A Responses codec outside that callback rejects messages before conversion.
    """
    from agent.auxiliary_client import CodexAuxiliaryClient, AsyncCodexAuxiliaryClient

    if (isinstance(client, (CodexAuxiliaryClient, AsyncCodexAuxiliaryClient)) and
            metadata.get("api_mode") == "codex_responses"):
        return {**metadata, "provider_api_mode": "codex_responses", "api_mode": "chat_completions"}
    return metadata


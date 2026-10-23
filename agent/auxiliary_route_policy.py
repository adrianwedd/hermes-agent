"""Provider selection policy for auxiliary discovery."""
import logging
from typing import Optional

logger = logging.getLogger("agent.auxiliary_client")


def _discovery_chain_allowed(main_provider: str, task: Optional[str] = None) -> bool:
    """The built-in discovery chain is a convenience for installs with NO selected main provider.
    Once the user picked one, every auxiliary route must be a provider they configured (main,
    ``auxiliary.<task>``, ``fallback_providers``); guessing "whatever else is logged in" bills an
    account they never pointed this session at (xAI OAuth session with a dead token → every
    compression silently charged to a Nous Portal balance)."""
    if (main_provider or "").strip().lower() in {"", "auto"}:
        return True
    logger.warning(
        "Auxiliary %s: main provider %s is unavailable and no fallback_chain / fallback_providers is "
        "configured — refusing to guess another logged-in provider. Re-authenticate (`hermes model`) "
        "or declare a fallback.", task or "call", main_provider)
    return False


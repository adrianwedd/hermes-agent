"""Same-provider transient retry policy for auxiliary recovery."""
from typing import Optional

def _should_retry_same_provider(task: Optional[str], exc: Exception, tag: str) -> bool:
    """True when ``exc`` is a transient transport blip worth a same-provider retry; critical-path
    tasks skip it on a full-budget timeout (``_should_skip_same_provider_retry``) and go straight
    to fallback."""
    from agent import auxiliary_client as aux
    if not aux._is_transient_transport_error(exc):
        return False
    if aux._should_skip_same_provider_retry(task, exc):
        aux.logger.info("Auxiliary %s%s: timeout on the critical path; "
                    "skipping same-provider retry and falling back: %s", task, tag, exc)
        return False
    return True


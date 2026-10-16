"""Turn-scoped playback ownership for speech-hook consumers."""
from contextlib import contextmanager


@contextmanager
def tts_playback_owner(agent, owner):
    """Keep ownership on this agent only, restoring it even on failed turns."""
    missing = object()
    previous = getattr(agent, "_tts_playback_owner", missing)
    agent._tts_playback_owner = owner
    try:
        yield
    finally:
        if previous is missing:
            del agent._tts_playback_owner
        else:
            agent._tts_playback_owner = previous

"""Durable origin metadata for dispatched worker sessions."""

import json
import os
from typing import Optional


def kanban_worker_origin_json(source: str) -> Optional[str]:
    """Use dispatcher-pinned board identity, never a title or cwd inference."""
    if source != "kanban" or not os.environ.get("HERMES_KANBAN_TASK"):
        return None
    board = (os.environ.get("HERMES_KANBAN_BOARD") or "").strip()
    if not board:
        return None
    return json.dumps({"kanban_board": board,
                       "kanban_task": os.environ["HERMES_KANBAN_TASK"].strip()})

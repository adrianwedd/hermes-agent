"""SQL visibility predicate for a worker's board, including session ancestry."""


def kanban_scope_sql(alias: str) -> str:
    """Return a predicate with board and exact-receipt JSON parameters, in that order.

    A non-Kanban child can inherit a worker transcript. Filtering only its own
    source leaves foreign-board children ahead of legitimate rows under LIMIT.
    UNION bounds corrupt parent cycles; the root check rejects those cycles and
    broken ancestry rather than inferring ownership.
    """
    stamp = "CASE WHEN json_valid(a.origin_json) THEN json_extract(a.origin_json, '$.kanban_board') END"
    return f"""(
        WITH RECURSIVE ancestry(id, parent_id, source, origin_json) AS (
            SELECT id, parent_session_id, source, origin_json
            FROM sessions WHERE id = {alias}.id
            UNION
            SELECT p.id, p.parent_session_id, p.source, p.origin_json
            FROM sessions p JOIN ancestry a ON p.id = a.parent_id
        )
        SELECT CASE WHEN
            SUM(CASE WHEN a.parent_id IS NULL THEN 1 ELSE 0 END) > 0
            AND SUM(CASE WHEN a.source = 'kanban' AND NOT COALESCE((
                ({stamp} = ? OR {stamp} IS NULL)
                    AND a.id IN (SELECT value FROM json_each(?))
            ), 0) THEN 1 ELSE 0 END) = 0
            THEN 1 ELSE 0 END
        FROM ancestry a
    ) = 1"""

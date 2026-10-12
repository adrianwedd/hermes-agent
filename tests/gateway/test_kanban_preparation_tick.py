"""One preparation across all boards in the existing service controller."""
from contextlib import nullcontext
from types import SimpleNamespace

from gateway import kanban_watchers_dispatcher as kwd
from hermes_cli import kanban_preparation as prep


def test_controller_caps_one_preparation_across_boards(monkeypatch):
    seen = []
    settings = kwd._DispatcherSettings(60.0, None, None, 2, 0, True, None, None)
    dispatcher = kwd._KanbanDispatcher(SimpleNamespace(), settings)
    monkeypatch.setattr(dispatcher, '_board_slugs', lambda: ['first', 'second'])
    monkeypatch.setattr(kwd._kbc(), 'connect_closing', lambda board: nullcontext(board))
    def prepare(conn):
        seen.append(conn)
        return {'prepared': 1, 'results': [{'task_id': 't_fixture', 'prepared': True}]}
    monkeypatch.setattr(prep, 'prepare_tick', prepare)
    assert dispatcher.prepare_todo_tick() == 1
    assert seen == ['first']

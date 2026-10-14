import base64
import json
import os
from pathlib import Path

import pytest
from tests.tools.kanban_worker_fixtures import source_layout as source_layout, worker_env as worker_env


# Descriptor-relative, no-symlink local reads are a POSIX capability.
pytestmark = pytest.mark.platforms("macos", "linux")

@pytest.fixture
def local_worker(worker_env, tmp_path):
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
    from hermes_cli.kanban_db_workspace import set_workspace_path
    root=tmp_path/'evidence';root.mkdir()
    with kbc.connect() as c:
        set_workspace_path(c,worker_env,root)
    return worker_env,root


def test_native_local_attachment_roundtrip_and_inline_compatibility(local_worker):
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
    tid,root=local_worker
    path=root/'completion-evidence.json';data=b'{"review":"accepted"}\n';path.write_bytes(data)
    result=json.loads(kt._handle_attach({'file_path':str(path)}))
    assert result.get('attachment_id'),result
    with kbc.connect() as c:
        att=next(a for a in kb.list_attachments(c,tid) if a.id==result['attachment_id'])
        assert Path(att.stored_path).read_bytes()==data
        assert att.filename==path.name
    inline=json.loads(kt._handle_attach({'filename':'inline.txt','content_base64':base64.b64encode(b'legacy').decode()}))
    assert inline.get('attachment_id'),inline


@pytest.mark.parametrize('kind',['outside','file_symlink','directory_symlink','fifo','directory','oversize','both','missing','protected'])
def test_local_payload_rejections_are_atomic(local_worker,tmp_path,monkeypatch,kind):
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
    tid,root=local_worker
    outside=tmp_path/'outside.txt';outside.write_text('not granted')
    path=root/'report';path.write_text('report')
    local={'file_path':str(path)}
    cases={
        'outside': ({'file_path':str(outside)},lambda:None),
        'file_symlink': (local,lambda:(path.unlink(),path.symlink_to(outside))),
        'directory_symlink': ({'file_path':str(root/'link/outside.txt')},lambda:(root/'link').symlink_to(tmp_path,target_is_directory=True)),
        'fifo': (local,lambda:(path.unlink(),os.mkfifo(path))),
        'directory': ({'file_path':str(root)},lambda:None),
        'oversize': (local,lambda:monkeypatch.setattr(kb,'KANBAN_ATTACHMENT_MAX_BYTES',2)),
        'protected': ({'file_path':str(root/'.env')},lambda:path.rename(root/'.env')),
        'both': ({**local,'content_base64':'eA=='},lambda:None),
        'missing': ({},lambda:None),
    }
    args,prepare=cases[kind]
    prepare()
    with kbc.connect() as c:before=len(kb.list_attachments(c,tid))
    result=json.loads(kt._handle_attach(args));assert result.get('error'),result
    with kbc.connect() as c:assert len(kb.list_attachments(c,tid))==before


def test_delegated_terminal_and_wrong_task_cannot_use_local_read(local_worker,monkeypatch):
    from tools import kanban_tools as kt
    from agent.delegation_context import DELEGATED_CHILD_ENV_MARKER
    tid,root=local_worker;path=root/'proof';path.write_text('proof')
    wrong=json.loads(kt._handle_attach({'task_id':'t_unowned','file_path':str(path)}));assert wrong.get('error')
    monkeypatch.setenv(DELEGATED_CHILD_ENV_MARKER,'1')
    denied=json.loads(kt._handle_attach({'file_path':str(path)}));assert 'delegate_task child' in denied.get('error','')


def test_local_read_preserves_existing_internal_denylist(local_worker,monkeypatch):
    from tools import kanban_tools as kt
    import agent.file_safety as safety
    _,root=local_worker;path=root/'protected';path.write_text('never upload')
    monkeypatch.setattr(safety,'get_read_block_error',lambda p:'protected fixture')
    result=json.loads(kt._handle_attach({'file_path':str(path)}));assert 'protected fixture' in result.get('error','')

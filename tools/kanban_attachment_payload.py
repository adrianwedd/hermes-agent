"""Native attachment payloads: bounded local evidence without model byte transport."""
import base64
import binascii
from contextlib import ExitStack
import os
import inspect
from pathlib import Path
import stat


def _read_workspace_file(path, workspace, cap):
    from agent.file_safety import get_read_block_error

    if not workspace:
        raise ValueError("Local attachment requires a resolved native task workspace")
    root = Path(workspace).resolve(strict=True)
    target = Path(path).expanduser()
    if not target.is_absolute() or '..' in target.parts:
        raise ValueError("file_path must be absolute and inside the task workspace")
    try:
        parts = target.relative_to(root).parts
    except ValueError:
        raise ValueError("file_path must stay inside the task workspace") from None
    if not parts:
        raise ValueError("file_path must name a regular file")
    blocked = get_read_block_error(str(target))
    if blocked:
        raise ValueError(blocked)
    if inspect.unwrap(os.open) not in os.supports_dir_fd or not hasattr(os, 'O_NOFOLLOW'):
        raise ValueError("This platform does not support guarded local attachment reads")
    # Descriptor-relative traversal rejects symlinks at every component, including
    # a concurrent directory replacement. Nonblocking open cannot hang on a FIFO.
    with ExitStack() as stack:
        def opened(name, flags, **kwargs):
            fd = os.open(name, flags, **kwargs)
            stack.callback(os.close, fd)
            return fd
        directory = opened(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for part in parts[:-1]:
            directory = opened(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
        fd = opened(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("file_path must name a regular file")
        if info.st_size > cap:
            raise ValueError("Local attachment exceeds the attachment size cap")
        data = bytearray()
        while len(data) <= cap:
            chunk = os.read(fd, min(1024 * 1024, cap + 1 - len(data)))
            if not chunk:
                return bytes(data)
            data.extend(chunk)
        raise ValueError("Local attachment exceeds the attachment size cap")


def attach_payload(args):
    from tools import kanban_tools as kt

    tid = kt._worker_guard("kanban_attach", args)
    local = args.get('file_path')
    inline = args.get('content_base64')
    kt._check(bool(local) != bool(inline), "Provide exactly one of file_path or content_base64")
    if local:
        path = kt._require_text(args, 'file_path')
        with kt._board(args.get('board')) as (kb, conn):
            task = kb.get_task(conn, tid)
            kt._check(task is not None, "Task not found")
            data = _read_workspace_file(path, task.workspace_path, kb.KANBAN_ATTACHMENT_MAX_BYTES)
        filename = args.get('filename') or Path(path).name
    else:
        filename = kt._require_text(args, 'filename')
        try:
            data = base64.b64decode(kt._require_text(args, 'content_base64'), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError(f"content_base64 is not valid base64: {exc}") from None
    return kt._store_attachment(args.get('board'), tid, filename, data, args.get('content_type'))

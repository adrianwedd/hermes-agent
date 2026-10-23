"""Native privileged transition boundary, including worker process ancestry.

This is an application authority guard, not an OS sandbox against arbitrary
same-user database writes. Dispatcher workers cannot gain CLI authority by
removing inherited environment markers.
"""
import os
import subprocess


def reject_worker_ancestry(conn):
    rows = conn.execute("SELECT worker_pid FROM task_runs WHERE ended_at IS NULL AND worker_pid IS NOT NULL").fetchall()
    workers = {int(r[0]) for r in rows}
    if not workers:
        return
    result = subprocess.run(["/bin/ps", "-axo", "pid=,ppid="], capture_output=True, text=True, check=True)
    parents = {int(a): int(b) for line in result.stdout.splitlines() for a, b in [line.split()]}
    pid = os.getpid()
    seen = set()
    while pid > 1 and pid not in seen:
        if pid in workers:
            raise PermissionError("Live worker ancestry cannot attest waits or revoke STOP")
        seen.add(pid)
        pid = parents.get(pid, 0)

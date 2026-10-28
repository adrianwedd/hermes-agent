"""Fresh-process adapter for manual backends paused by historical updaters."""
from __future__ import annotations

import json
from contextlib import suppress
from pathlib import Path
import sys


def main(context: Path, result: Path) -> int:
    handled = False
    try:
        request = json.loads(context.read_text(encoding="utf-8-sig"))
        root = Path(request["root"])
        token = request["stopped_serves"]
        if not token.get("pending"):
            handled = True
            return 0
        sys.path.insert(0, str(root))
        from pm.environments import activate_dependencies
        activate_dependencies(root)
        from hermes_cli.dashboard_procs import _filter_dashboard_respawn_candidates
        from hermes_cli.main_dashboard import _respawn_dashboard_processes
        from hermes_cli.update_serve_readiness import verify_released_and_ready

        candidates = []
        entries = []
        skipped = 0
        for entry in token.get("entries") or []:
            port = entry.get("port")
            if (entry.get("purpose") not in ("serve", "dashboard")
                    or type(port) is not int or not 0 < port <= 65535):
                skipped += 1
                continue
            command = [sys.executable, str(root / "hermes")]
            profile = entry.get("profile")
            if profile and profile != "default":
                command += ["--profile", str(profile)]
            command.append(entry["purpose"])
            if entry.get("host"):
                command += ["--host", str(entry["host"])]
            command += ["--port", str(port)]
            candidates.append((entry.get("pid", 0), command, entry.get("hermes_home") or None))
            entries.append(entry)
        commands = _filter_dashboard_respawn_candidates(candidates, own_home=request.get("home"))
        skipped += len(candidates) - len(commands)
        # Pair each surviving command with its token entry by the port it carries, so a skipped
        # candidate is never spawned and each spawned backend is verified on its own port.
        by_port = {}
        kept = []
        for command in commands or []:
            command = list(command)
            port = None
            for index, part in enumerate(command):
                if part == "--port" and index + 1 < len(command):
                    with suppress(ValueError):
                        port = int(command[index + 1])
            if port is None:
                continue
            by_port[port] = command
            for entry in entries:
                if entry.get("port") == port:
                    kept.append(entry)
                    break

        def spawn(ready_entries):
            selected = [by_port[e.get("port")] for e in ready_entries if e.get("port") in by_port]
            # An acknowledged attempt is terminal even if spawning failed: replaying
            # the whole token at atexit would duplicate the successful backends.
            return _respawn_dashboard_processes(selected) if selected else []

        failures, ready = verify_released_and_ready(kept, spawn)
        handled = True
        if skipped:
            failures.append(f"{skipped} stopped backend(s) were not relaunchable from the recorded token")
        if failures:
            for line in failures:
                print(f"  ✗ {line}", file=sys.stderr)
            if not ready:
                print("Some stopped backends could not be relaunched; restart them manually.",
                      file=sys.stderr)
            else:
                print(f"  {len(ready)} of {len(kept)} stopped backend(s) are serving again.",
                      file=sys.stderr)
            return 1
        return 0
    except Exception as exc:
        print(f"Stopped serve recovery failed: {exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        result.write_text(json.dumps({"serves_handled": handled}), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1]), Path(sys.argv[2])))

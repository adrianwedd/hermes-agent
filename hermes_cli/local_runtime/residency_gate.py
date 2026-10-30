"""One enforceable memory boundary for heavyweight local-model work.

A Kanban worker that loads a 169 GiB model is not an ordinary coding task. On
2026-10-30 a bench worker held a 169.235 GiB residency while an independent
process began loading the same checkpoint; the host went down.

Two properties matter and neither existed before:

* the ledger is **host-wide**, not per-``HERMES_HOME`` — the memory being
  protected belongs to the machine, not to a profile;
* admission is **atomic and recorded**. A read-only check lets two candidates
  pass against the same headroom, which is exactly the failure that happened.
  ``admit`` therefore takes a short hold; the hold expires well before it can
  pin a machine, and live RSS takes over once the load is resident.

Failure is always closed: an unreadable ledger or an unmeasurable machine
refuses admission rather than guessing that the memory is free.
"""
from __future__ import annotations

import errno
import fcntl
import json
import os
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Optional

# Headroom kept back from the physical total: the OS, the desktop, the operator's
# own work. A model load may never plan to take the last of the machine.
DEFAULT_CEILING_FRACTION = 0.85

# Weights residency overhead over the on-disk size (KV cache, mlx buffers).
WEIGHT_OVERHEAD = 1.15

# Long enough to bridge admission -> resident RSS; short enough that a crashed
# holder cannot pin capacity. A live load is visible in available memory anyway.
HOLD_TTL_SECONDS = 10 * 60


class LedgerError(RuntimeError):
    """The ledger exists but cannot be trusted. Callers must fail closed."""


def _root() -> Path:
    override = os.environ.get("HERMES_RESIDENCY_ROOT")
    if override:
        return Path(override)
    # Host-wide by design: every profile, worker and harness on this machine
    # must see the same reservations.
    return Path.home() / ".hermes" / "residency"


def _ledger_path() -> Path:
    return _root() / "leases.json"


def _calibration_path() -> Path:
    return _root() / "calibrated.json"


@contextmanager
def _locked():
    _root().mkdir(parents=True, exist_ok=True)
    with open(_root() / ".lock", "a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _read_leases() -> Dict[str, Any]:
    path = _ledger_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LedgerError(f"residency ledger unreadable: {exc}") from exc
    if not isinstance(data, dict):
        raise LedgerError("residency ledger is not an object")
    return data


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _pid_alive(pid: Optional[int]) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError as exc:
        return exc.errno == errno.EPERM
    return True


def total_gib() -> Optional[float]:
    try:
        out = subprocess.run(
            ["sysctl", "-n", "hw.memsize"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip().isdigit():
            return int(out.stdout.strip()) / 1024 ** 3
    except Exception:
        pass
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) / 1024 ** 2
    except OSError:
        pass
    return None


def available_gib() -> Optional[float]:
    """Reclaimable memory right now: free + inactive + speculative + purgeable."""
    try:
        out = subprocess.run(
            ["vm_stat"], capture_output=True, text=True, encoding="utf-8", timeout=5
        )
        if out.returncode == 0 and out.stdout:
            pages: Dict[str, int] = {}
            page_size = 4096
            for line in out.stdout.splitlines():
                if "page size of" in line:
                    page_size = int(line.split("page size of")[1].split()[0])
                    continue
                if ":" not in line:
                    continue
                key, _, value = line.partition(":")
                try:
                    pages[key.strip()] = int(value.strip().rstrip("."))
                except ValueError:
                    continue
            reclaimable = sum(
                pages.get(key, 0)
                for key in ("Pages free", "Pages inactive", "Pages speculative", "Pages purgeable")
            )
            if reclaimable:
                return reclaimable * page_size / 1024 ** 3
    except Exception:
        pass
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1024 ** 2
    except (OSError, ValueError, IndexError):
        pass
    return None


def _ceiling_gib() -> Optional[float]:
    total = total_gib()
    if total is None:
        return None
    try:
        fraction = float(os.environ.get("HERMES_RESIDENCY_CEILING_FRACTION", DEFAULT_CEILING_FRACTION))
    except ValueError:
        fraction = DEFAULT_CEILING_FRACTION
    return total * max(0.1, min(fraction, 1.0))


def _lease_peak(lease: Any) -> Optional[float]:
    if not isinstance(lease, dict):
        return None
    peak = lease.get("peak_gib")
    if isinstance(peak, bool) or not isinstance(peak, (int, float)) or peak <= 0:
        return None
    return float(peak)


def _live_leases(leases: Dict[str, Any], now: int) -> Dict[str, Any]:
    """Leases still doing work.

    A malformed record raises instead of being skipped: dropping it would turn
    an unknown commitment into apparent free memory.
    """
    live: Dict[str, Any] = {}
    for key, lease in leases.items():
        expires = lease.get("expires_at") if isinstance(lease, dict) else None
        if isinstance(expires, int) and expires <= now:
            continue  # expired: irrelevant whatever shape it is in
        if _lease_peak(lease) is None:
            raise LedgerError(
                f"residency lease {key!r} in {_ledger_path()} has no usable peak_gib"
            )
        pid = lease.get("pid")
        if isinstance(pid, int) and pid > 0 and not _pid_alive(pid):
            continue
        live[key] = lease
    return live


# Processes that hold a model resident without ever taking a Hermes lease. RSS is
# the evidence, not cooperation: the oMLX server and the bench lane both appear
# here, and the 2026-10-30 overlap was exactly this — two of them at once.
_MODEL_PROCESS_PATTERNS = (
    "omlx-server",
    "mlx_lane.worker",
    "llama-server",
    "llama.cpp",
    "mlx_lm",
    "vllm",
)


def observed_model_gib() -> float:
    """Resident memory of running model processes, in GiB. Best effort: 0.0 when
    the process table cannot be read, never an exception."""
    try:
        out = subprocess.run(
            ["ps", "-Ao", "rss=,command="], capture_output=True, text=True, encoding="utf-8", timeout=5
        )
    except Exception:
        return 0.0
    total_kib = 0
    for line in (out.stdout or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        pid_rss, _, command = stripped.partition(" ")
        if not pid_rss.isdigit():
            continue
        if "residency" in command or "ps -Ao" in command:
            continue
        if any(pattern in command for pattern in _MODEL_PROCESS_PATTERNS):
            total_kib += int(pid_rss)
    return total_kib / 1024 ** 2


def _headroom(leases: Dict[str, Any], now: int) -> Dict[str, Any]:
    ceiling = _ceiling_gib()
    available = available_gib()
    if ceiling is None or available is None:
        raise LedgerError("memory measurement unavailable")
    live = _live_leases(leases, now)
    reserved = sum(_lease_peak(lease) or 0.0 for lease in live.values())
    observed = observed_model_gib()
    # `available` already excludes resident RSS: a loaded model has reduced it.
    # Subtracting observed processes again would count the same memory twice, and
    # summing RSS over shared mappings overstates residency anyway. Only the pending
    # holds — work that is admitted but not yet resident — are not in `available`.
    headroom = min(ceiling, available) - reserved
    return {
        "ceiling_gib": ceiling,
        "available_gib": available,
        "reserved_gib": reserved,
        "observed_model_gib": observed,  # evidence for pricing and conflict, not arithmetic
        "headroom_gib": headroom,
        "leases": live,
    }


def _refusal(peak: float, facts: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "admitted": False,
        "reason": (
            f"needs {peak:.1f} GiB, available {facts['available_gib']:.1f} GiB, "
            f"reserved {facts['reserved_gib']:.1f} GiB, ceiling {facts['ceiling_gib']:.1f} GiB"
        ),
        "available_gib": facts["available_gib"],
        "reserved_gib": facts["reserved_gib"],
        "ceiling_gib": facts["ceiling_gib"],
    }


def admit(
    holder: str,
    peak_gib: float,
    *,
    ttl_seconds: int = HOLD_TTL_SECONDS,
    note: Optional[str] = None,
) -> Dict[str, Any]:
    """Take a hold for ``peak_gib``, or refuse with the arithmetic.

    Atomic: the check and the hold happen under one lock, so two candidates
    cannot both pass against the same headroom.
    """
    if not isinstance(peak_gib, (int, float)) or isinstance(peak_gib, bool) or peak_gib <= 0:
        return {"admitted": False, "reason": "peak_gib must be a positive number"}
    now = int(time.time())
    try:
        with _locked():
            try:
                facts = _headroom(_read_leases(), now)
            except LedgerError as exc:
                return {"admitted": False, "reason": f"{exc}; refusing to guess"}
            if float(peak_gib) > facts["headroom_gib"]:
                return _refusal(float(peak_gib), facts)
            facts["leases"][holder] = {
                "peak_gib": float(peak_gib),
                "pid": os.getpid(),
                "note": note,
                "granted_at": now,
                "expires_at": now + int(ttl_seconds),
            }
            _write_json(_ledger_path(), facts["leases"])
            return {
                "admitted": True,
                "holder": holder,
                "available_gib": facts["available_gib"],
                "reserved_gib": facts["reserved_gib"],
                "ceiling_gib": facts["ceiling_gib"],
            }
    except (OSError, TypeError, ValueError, LedgerError) as exc:
        return {"admitted": False, "reason": f"residency ledger unavailable: {exc}"}


def release(holder: str, *, pid: Optional[int] = None) -> bool:
    """Drop a hold. With ``pid``, only that process's hold is released — two
    harnesses naming the same model must not cancel each other's reservation."""
    try:
        with _locked():
            leases = _read_leases()
            lease = leases.get(holder)
            if not isinstance(lease, dict):
                return False
            if pid is not None and lease.get("pid") not in (None, pid):
                return False
            leases.pop(holder, None)
            _write_json(_ledger_path(), leases)
            return True
    except (OSError, LedgerError):
        return False


def snapshot() -> Dict[str, Any]:
    now = int(time.time())
    try:
        with _locked():
            facts = _headroom(_read_leases(), now)
    except (OSError, LedgerError) as exc:
        return {"error": str(exc), "total_gib": total_gib(), "available_gib": available_gib()}
    return {
        "total_gib": total_gib(),
        "available_gib": facts["available_gib"],
        "ceiling_gib": facts["ceiling_gib"],
        "reserved_gib": round(facts["reserved_gib"], 3),
        "headroom_gib": round(facts["headroom_gib"], 3),
        "leases": facts["leases"],
    }


def calibrate(model_id: str, peak_gib: float, *, source: str = "bench") -> None:
    try:
        with _locked():
            try:
                data = json.loads(_calibration_path().read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            if not isinstance(data, dict):
                data = {}
            data[str(model_id)] = {
                "peak_gib": round(float(peak_gib), 3),
                "source": source,
                "at": int(time.time()),
            }
            _write_json(_calibration_path(), data)
    except OSError:
        pass


def conservative_unknown_peak() -> float:
    """Price for a heavyweight job with no evidence at all.

    Not a floor of convenience: with no model identity and no history, the safest
    honest assumption is that it may take most of the machine, not a quarter of it.
    """
    total = total_gib()
    return max(96.0, (total or 0.0) * 0.6)


def heaviest_calibrated_gib() -> Optional[float]:
    """The largest residency this machine has actually been observed to hold."""
    try:
        data = json.loads(_calibration_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    peaks = [
        float(record["peak_gib"])
        for record in data.values()
        if isinstance(record, dict)
        and isinstance(record.get("peak_gib"), (int, float))
        and not isinstance(record.get("peak_gib"), bool)
        and record["peak_gib"] > 0
    ]
    return max(peaks) if peaks else None


def estimate_peak_gib(weights_bytes: Optional[int], model_id: Optional[str] = None) -> Optional[float]:
    """Calibrated observation if we have one, else weights × overhead."""
    if model_id:
        try:
            data = json.loads(_calibration_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        record = data.get(model_id) if isinstance(data, dict) else None
        if isinstance(record, dict):
            peak = record.get("peak_gib")
            if isinstance(peak, (int, float)) and not isinstance(peak, bool) and peak > 0:
                return float(peak)
    if isinstance(weights_bytes, int) and not isinstance(weights_bytes, bool) and weights_bytes > 0:
        return weights_bytes / 1024 ** 3 * WEIGHT_OVERHEAD
    return None

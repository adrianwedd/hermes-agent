"""Doctor output primitives shared by every ``hermes_cli.doctor_*`` module."""

from __future__ import annotations

import functools
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from hermes_cli.colors import Colors, color


_current_finding: ContextVar = ContextVar("doctor_finding", default=None)


@contextmanager
def finding_scope(finding):
    token = _current_finding.set(finding)
    try:
        yield
    finally:
        _current_finding.reset(token)


def _record(category: str, text: str, detail: str = "") -> None:
    finding = _current_finding.get()
    if finding is not None:
        getattr(finding, category).append(text + (f" {detail}" if detail else ""))


def _mark(glyph: str, col: str):
    def report(text, detail=""):
        if glyph == "⚠":
            _record("warnings", text, detail)
        print(f"  {color(glyph, col)} {text}" + (f" {color(detail, Colors.DIM)}" if detail else ""))
    return report


check_ok, check_warn, check_fail = _mark("✓", Colors.GREEN), _mark("⚠", Colors.YELLOW), _mark("✗", Colors.RED)


def check_unavailable(text: str, detail: str = "") -> None:
    _record("unavailable", text, detail)
    check_info(f"Unavailable: {text} {detail}".rstrip())


def check_unverified(text: str, detail: str = "") -> None:
    _record("unverified", text, detail)
    check_info(f"Unverified: {text} {detail}".rstrip())


def check_info(text: str):
    print(f"    {color('→', Colors.CYAN)} {text}")


def check_bool(cond, ok, bad, *, fail: bool = False):
    """``check_ok(*ok)`` when *cond* else ``check_warn(*bad)`` (``check_fail`` with fail=True); returns bool(cond).
    *ok* / *bad* are a text string or a ``(text, detail)`` tuple."""
    args = ok if cond else bad
    (check_ok if cond else (check_fail if fail else check_warn))(*((args,) if isinstance(args, str) else args))
    return bool(cond)


def _section(title: str) -> None:
    """Print a doctor section banner: blank line + bold cyan ◆ title."""
    print()
    print(color(f"◆ {title}", Colors.CYAN, Colors.BOLD))


def _fail_and_issue(text: str, detail: str, fix: str, issues: list[str]) -> None:
    """Emit a check_fail and append the corresponding fix instruction."""
    check_fail(text, detail)
    issues.append(fix)


@contextmanager
def warn_on_error(text: str, detail: str = "({e})", report=check_warn):
    """Best-effort block: an exception prints ``report(text.format(e=e), detail.format(e=e))`` (nothing when
    *text* is ``""``) instead of propagating. ``{e}`` in either string is the exception."""
    try:
        yield
    except Exception as e:
        check_unverified(text.format(e=e) if text else "Check could not complete", str(e))
        if text and report is not check_warn:
            report(text.format(e=e), detail.format(e=e))


@dataclass
class Finding:
    """What one doctor check contributed: auto-fixable issues, manual-only issues, fixes applied."""

    issues: list = field(default_factory=list)
    manual_issues: list = field(default_factory=list)
    fixed: int = 0
    security: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    unavailable: list = field(default_factory=list)
    unverified: list = field(default_factory=list)

    def merge(self, other: Finding) -> None:
        self.issues.extend(other.issues)
        self.manual_issues.extend(other.manual_issues)
        self.fixed += other.fixed
        for category in ("security", "warnings", "unavailable", "unverified"):
            getattr(self, category).extend(getattr(other, category))


def doctor_check(on_error: str | None = None, detail: str = ""):
    """Turn ``fn(should_fix, f: Finding)`` into a ``(should_fix) -> Finding`` doctor check.

    Exceptions are recorded as unverified and the partial Finding is returned, so established
    errors or vulnerabilities survive a later failure. *on_error* supplies a user-facing label."""
    def deco(fn):
        @functools.wraps(fn)
        def check(should_fix: bool) -> Finding:
            f = Finding()
            token = _current_finding.set(f)
            try:
                if on_error is None:
                    fn(should_fix, f)
                else:
                    with warn_on_error(on_error, detail):
                        fn(should_fix, f)
            except Exception as exc:
                logging.getLogger(__name__).exception("Doctor check %s failed", fn.__name__)
                check_unverified(f"{fn.__name__} could not complete", str(exc))
            finally:
                _current_finding.reset(token)
            return f
        return check
    return deco


def ensure_dir(f: Finding, should_fix: bool, path, exists_msg: str, created_msg: str, missing_msg: str) -> None:
    """ok when *path* exists; with --fix create it (counts as fixed); else warn "(will be created on first use)"."""
    if path.exists():
        check_ok(exists_msg)
    elif should_fix:
        path.mkdir(parents=True, exist_ok=True)
        check_ok(created_msg)
        f.fixed += 1
    else:
        check_warn(missing_msg, "(will be created on first use)")

"""A commit message is data, not a command: the label-independent launchctl pre-scan must agree.

Measured defect. ``gateway_lifecycle_block`` front-runs the full guard with a label-independent
pre-scan (``contains_launchctl_submit_command``) purely to choose a more specific diagnostic. That
pre-scan tokenized the raw command text, so a heredoc body attached to ``git commit -F -`` — a
commit message that documents a command — was read as the command itself. The enforcement pass
(``_contains_unsafe_gateway_action``) masked inert heredoc bodies first, so the diagnostic was
strictly more restrictive than the guard it describes: the documented action was refused while the
guard would have permitted it.

The fix is intent discrimination, not relaxation: ``git commit|tag`` consume a quoted heredoc body
as opaque text, so their bodies join the inert set, and the pre-scan masks before tokenizing. Every
executable shape must still block — an unquoted body, a shell consumer, or a bare invocation —
because the stripper fails open on any ambiguity.

Controls that must stay TRUE below are the point of this file: a matcher that stops blocking
altogether would pass the prose cases and fail the protection cases.
"""

import pytest

from cron.lifecycle_guard import contains_launchctl_submit_command
from tools.shell_heredoc import strip_inert_heredoc_bodies


class TestProseIsNotACommand:
    @pytest.mark.parametrize("owner", ["git commit -q -F - -- a.txt", "git tag -a v1 -F -"])
    def test_documented_command_in_commit_message_is_inert(self, owner):
        command = (
            f"cd /repo && {owner} <<'MSG'\n"
            f"fix: reload the unit\n"
            f"  launchctl bootstrap gui/501/org.example.job\n"
            f"MSG"
        )
        assert contains_launchctl_submit_command(command) is False

    def test_stripper_recognises_commit_message_consumer(self):
        """The allowlist is what makes the body inert; assert it directly, not via the result."""
        command = "git commit -F - <<'MSG'\nlaunchctl bootstrap gui/501/x\nMSG"
        assert strip_inert_heredoc_bodies(command) != command

    def test_python_heredoc_prose_still_inert(self):
        command = "python3 - <<'PY'\nprint('launchctl bootstrap gui/501/x')\nPY"
        assert contains_launchctl_submit_command(command) is False

    def test_quoted_mention_only_is_not_a_command(self):
        assert contains_launchctl_submit_command('echo "launchctl bootstrap"') is False


class TestProtectionIsIntact:
    def test_shell_heredoc_still_blocks(self):
        """The realistic bypass: a body a shell will execute."""
        command = "bash <<'EOF'\nlaunchctl bootstrap gui/501/x\nEOF"
        assert contains_launchctl_submit_command(command) is True

    def test_unquoted_heredoc_still_blocks(self):
        """Unquoted delimiters expand, so the stripper must leave them visible."""
        command = "sh <<EOF\nlaunchctl submit -l y -- /bin/true\nEOF"
        assert contains_launchctl_submit_command(command) is True

    def test_unterminated_heredoc_still_blocks(self):
        command = "bash <<'EOF'\nlaunchctl bootstrap gui/501/x"
        assert contains_launchctl_submit_command(command) is True

    @pytest.mark.parametrize(
        "command",
        ["launchctl bootstrap gui/501/x", "launchctl submit -l y -- /bin/true"],
    )
    def test_bare_invocation_still_blocks(self, command):
        assert contains_launchctl_submit_command(command) is True

    def test_non_subcommand_git_invocation_stays_visible(self):
        """Only commit/tag are allowlisted; a general git invocation may run aliases or hooks."""
        command = "git -c x=1 <<'EOF'\nlaunchctl bootstrap gui/501/x\nEOF"
        assert contains_launchctl_submit_command(command) is True

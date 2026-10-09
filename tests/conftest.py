from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from systemdpilot.core.runner import CommandResult, CommandRunner  # noqa: E402


class FakeRunner(CommandRunner):
    """Records calls and replies with canned results keyed by argv prefix."""

    label = "fake"

    def __init__(self):
        self.calls: list[dict] = []
        self.replies: list[tuple[tuple[str, ...], CommandResult]] = []

    def reply(self, *prefix: str, stdout: str = "", stderr: str = "", returncode: int = 0):
        self.replies.append((prefix, CommandResult(prefix, returncode, stdout, stderr)))

    def run(self, argv: Sequence[str], *, input=None, privileged=False, timeout=60):
        self.calls.append({"argv": list(argv), "input": input, "privileged": privileged})
        for prefix, result in self.replies:
            if tuple(argv[: len(prefix)]) == prefix:
                return CommandResult(tuple(argv), result.returncode, result.stdout, result.stderr)
        return CommandResult(tuple(argv), 0, "", "")


@pytest.fixture
def runner():
    return FakeRunner()

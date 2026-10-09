"""Starting points for new unit files."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class UnitTemplate:
    id: str
    title: str
    content: str


TEMPLATES = (
    UnitTemplate(
        "simple",
        "Long-running service",
        """[Unit]
Description=My service
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/sleep infinity
Restart=on-failure

[Install]
WantedBy={target}
""",
    ),
    UnitTemplate(
        "oneshot",
        "One-shot task",
        """[Unit]
Description=My task

[Service]
Type=oneshot
ExecStart=/usr/bin/true
RemainAfterExit=yes

[Install]
WantedBy={target}
""",
    ),
)


def render(template: UnitTemplate, user_scope: bool) -> str:
    return template.content.format(target="default.target" if user_scope else "multi-user.target")

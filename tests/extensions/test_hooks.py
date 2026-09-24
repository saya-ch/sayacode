"""项目 Hook 的信任边界与前置阻止行为。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from sayacode.extensions.hooks import HookRuntime


@pytest.mark.asyncio
async def test_project_hook_requires_trust_and_can_block_tool(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "project"
    config = workspace / ".sayacode" / "hooks.json"
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "command": [sys.executable, "-c", "import sys; sys.exit(7)"],
                            "blocking": True,
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    hooks = HookRuntime(workspace, state_home=tmp_path / "state")
    assert hooks.status()["project_hooks"] == 0
    assert await hooks.trigger("PreToolUse", {"tool": "write_file"}) is None
    hooks.trust()
    assert hooks.status()["project_hooks"] == 1
    blocked = await hooks.trigger("PreToolUse", {"tool": "write_file"})
    assert blocked and "blocked PreToolUse" in blocked
    hooks.untrust()
    assert hooks.status()["project_hooks"] == 0

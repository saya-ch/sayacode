"""项目 Hook 的信任边界与前置阻止行为。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from sayacode.extensions.hooks import HookRuntime
from tests.support import contract_app


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


async def test_read_only_and_workspace_auto_do_not_run_user_prompt_hooks(tmp_path: Path) -> None:
    app = await contract_app(tmp_path)
    marker = tmp_path / "hook-ran.txt"
    config = app.workspace / ".sayacode" / "hooks.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        json.dumps(
            {
                "hooks": {
                    "UserPromptSubmit": [
                        {
                            "command": [
                                sys.executable,
                                "-c",
                                f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')",
                            ]
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    app.hooks.trust()
    app.hooks.reload()
    try:
        await app._save_thread_policy(app.session_id, trust_level="read_only")
        assert (await app.run("只读查询"))["ok"] is True
        assert not marker.exists()
        await app._save_thread_policy(app.session_id, trust_level="workspace_auto")
        assert (await app.run("工作区任务"))["ok"] is True
        assert not marker.exists()
        await app._save_thread_policy(app.session_id, trust_level="ask")
        assert (await app.run("询问档任务"))["ok"] is True
        assert marker.exists()
    finally:
        await app.aclose()

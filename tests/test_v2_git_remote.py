"""Remote management uses real local Git configuration without contacting servers."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest
from langchain.tools import ToolRuntime

from sayacode.policy import Policy
from sayacode.tools import git


def run_git(root, *args, check=True):
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    if check:
        assert result.returncode == 0, result.stderr
    return result


def runtime(root):
    return ToolRuntime(
        state={},
        config={},
        store=None,
        tool_call_id="remote-test",
        stream_writer=lambda _: None,
        context=SimpleNamespace(
            workspace=root, mode="build", output_dir=root / "outputs", policy=Policy()
        ),
    )


async def test_remote_add_set_url_query_and_remove(tmp_path):
    run_git(tmp_path, "init")
    execution = runtime(tmp_path)
    added = await git.coroutine(
        action="remote_add",
        remote="backup",
        url="https://example.invalid/team/repository.git",
        runtime=execution,
    )
    assert added["exit_code"] == 0, added["stderr"]
    assert (
        run_git(tmp_path, "config", "--get", "remote.backup.url").stdout.strip()
        == "https://example.invalid/team/repository.git"
    )
    updated = await git.coroutine(
        action="remote_set_url",
        remote="backup",
        url="git@example.invalid:team/repository.git",
        runtime=execution,
    )
    assert updated["exit_code"] == 0, updated["stderr"]
    assert (
        run_git(tmp_path, "config", "--get", "remote.backup.url").stdout.strip()
        == "git@example.invalid:team/repository.git"
    )
    listing = await git.coroutine(action="remote", runtime=execution)
    assert (
        "backup" in listing["stdout"]
        and "git@example.invalid:team/repository.git" in listing["stdout"]
    )
    local_path = str(tmp_path / "local repository")
    updated = await git.coroutine(
        action="remote_set_url", remote="backup", url=local_path, runtime=execution
    )
    assert updated["exit_code"] == 0, updated["stderr"]
    assert run_git(tmp_path, "config", "--get", "remote.backup.url").stdout.strip() == local_path
    removed = await git.coroutine(action="remote_remove", remote="backup", runtime=execution)
    assert removed["exit_code"] == 0, removed["stderr"]
    assert run_git(tmp_path, "remote").stdout.strip() == ""


@pytest.mark.parametrize(
    "name,url",
    [
        ("-bad", "https://example.invalid/repo"),
        ("bad name", "https://example.invalid/repo"),
        ("../escape", "https://example.invalid/repo"),
        ("origin", ""),
        ("origin", "--upload-pack=bad"),
        ("origin", "https:///missing-host"),
        ("origin", "https://example.invalid:wrong/repo"),
        ("origin", "https://example.invalid/repo\nnext-command"),
    ],
)
async def test_invalid_remote_arguments_leave_configuration_unchanged(tmp_path, name, url):
    run_git(tmp_path, "init")
    before = (tmp_path / ".git" / "config").read_bytes()
    with pytest.raises(ValueError):
        await git.coroutine(action="remote_add", remote=name, url=url, runtime=runtime(tmp_path))
    assert (tmp_path / ".git" / "config").read_bytes() == before
    assert run_git(tmp_path, "remote").stdout.strip() == ""


@pytest.mark.parametrize("action", ["remote_add", "remote_remove", "remote_set_url"])
def test_remote_mutations_require_approval_and_are_denied_in_read_only_modes(tmp_path, action):
    policy = Policy()
    arguments = {"action": action, "remote": "origin", "url": "https://example.invalid/repo"}
    ctx = runtime(tmp_path).context
    assert policy.decide("git", arguments, ctx).action == "ask"
    for mode in ("plan", "review"):
        ctx.mode = mode
        assert policy.decide("git", arguments, ctx).action == "deny"
        assert policy.decide("git", {"action": "remote"}, ctx).action == "allow"

# git_tools 全覆盖测试：真仓库实测 + 异常分支定点触发。

from pathlib import Path
import subprocess

import pytest

import lib.tools.git_tools as gt
from lib.tools.git_tools import (
    get_default_workspace,
    git_add,
    git_branch,
    git_checkout,
    git_commit,
    git_diff,
    git_log,
    git_pull,
    git_push,
    git_remote,
    git_stash,
    git_status,
    reset_workspace,
    use_workspace,
)


def _run(*args, cwd):
    # 同步跑 git，失败直接抛，让测试 fail-fast。
    subprocess.run(["git", *args], cwd=str(cwd), check=True,
                   capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    # 建一个带一次提交的真仓库，并把 git 工具工作区绑过去。
    _run("init", cwd=tmp_path)
    _run("config", "user.email", "t@t.t", cwd=tmp_path)
    _run("config", "user.name", "t", cwd=tmp_path)
    (tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")
    _run("add", ".", cwd=tmp_path)
    _run("commit", "-m", "init", cwd=tmp_path)
    token = use_workspace(tmp_path)
    try:
        yield tmp_path
    finally:
        reset_workspace(token)


@pytest.fixture
def no_perm(monkeypatch):
    # 默认放行变更类工具的权限检查。
    monkeypatch.setattr(gt, "enforce_tool_permission", lambda *a, **k: None)


# ── 工作区与纯函数 ───────────────────────────────────────────────────────

class TestHelpers:
    def test_workspace_roundtrip(self, tmp_path):
        from lib.tools.git_tools import set_default_workspace

        token = use_workspace(tmp_path)
        assert get_default_workspace() == tmp_path.resolve()
        reset_workspace(token)
        try:
            set_default_workspace(tmp_path)
            assert get_default_workspace() == tmp_path.resolve()
        finally:
            set_default_workspace(Path.cwd())

    def test_resolve_cwd(self, repo):
        from lib.tools.git_tools import _resolve_git_workspace

        assert _resolve_git_workspace() == repo.resolve()
        sub = repo / "sub"
        sub.mkdir()
        assert _resolve_git_workspace("sub") == sub.resolve()

    def test_format_output(self):
        from lib.tools.git_tools import _format_git_output

        assert "body" in _format_git_output("body")
        out = _format_git_output("body", title="T")
        assert "T" in out and "body" in out

    def test_is_git_repo(self, repo, tmp_path):
        from lib.tools.git_tools import _is_git_repo

        assert _is_git_repo(repo) is True
        assert _is_git_repo(tmp_path / "nope") is False
        (tmp_path / "fake").mkdir()
        (tmp_path / "fake" / ".git").write_text("gitdir: elsewhere", encoding="utf-8")
        assert _is_git_repo(tmp_path / "fake") is False

    def test_has_changes(self, repo, no_perm):
        from lib.tools.git_tools import _has_worktree_changes

        assert _has_worktree_changes(repo) is False
        (repo / "a.txt").write_text("dirty\n", encoding="utf-8")
        assert _has_worktree_changes(repo) is True

    def test_has_changes_bad_rc(self, repo, monkeypatch):
        from lib.tools.git_tools import _has_worktree_changes

        monkeypatch.setattr(gt, "_run_git_command", lambda *a, **k: ("", "err", 1))
        assert _has_worktree_changes(repo) is False

    def test_validate_ref(self):
        from lib.tools.git_tools import _validate_git_ref_name

        assert _validate_git_ref_name("") is not None
        assert _validate_git_ref_name("  ") is not None
        assert _validate_git_ref_name("-b") is not None
        assert _validate_git_ref_name("a\nb") is not None
        assert _validate_git_ref_name("main") is None


class TestRunGitCommand:
    def test_ok(self, repo):
        out, _, rc = gt._run_git_command(["status"], cwd=repo)
        assert rc == 0

    def test_default_cwd(self, repo):
        token = use_workspace(repo)
        try:
            _, _, rc = gt._run_git_command(["status"])
        finally:
            reset_workspace(token)
        assert rc == 0

    def test_missing_binary(self, repo, monkeypatch):
        monkeypatch.setattr(gt.subprocess, "Popen",
                            lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))
        _, err, rc = gt._run_git_command(["status"], cwd=repo)
        assert rc == 127
        assert "Git" in err

    def test_generic_error(self, repo, monkeypatch):
        monkeypatch.setattr(gt.subprocess, "Popen",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        _, err, rc = gt._run_git_command(["status"], cwd=repo)
        assert rc == 1
        assert "boom" in err

    def test_timeout(self, repo, monkeypatch):
        import subprocess as _sp

        class FakeProc:
            returncode = 0
            calls = 0

            def communicate(self, timeout=None):
                type(self).calls += 1
                if type(self).calls >= 3:
                    return ("", "")
                raise _sp.TimeoutExpired(["git"], timeout)

            def kill(self):
                raise RuntimeError("already gone")

        FakeProc.calls = 0
        monkeypatch.setattr(gt.subprocess, "Popen", lambda *a, **k: FakeProc())
        monkeypatch.setattr(gt, "terminate_process_tree", lambda p: None)
        out, err, rc = gt._run_git_command(["status"], cwd=repo, timeout=1)
        assert rc == 124
        assert "超时" in err

    def test_timeout_with_stderr(self, repo, monkeypatch):
        import subprocess as _sp

        class FakeProc:
            returncode = 0
            calls = 0

            def communicate(self, timeout=None):
                type(self).calls += 1
                if type(self).calls == 2:
                    return ("partial", "some-err")
                raise _sp.TimeoutExpired(["git"], timeout)

            def kill(self):
                pass

        FakeProc.calls = 0
        monkeypatch.setattr(gt.subprocess, "Popen", lambda *a, **k: FakeProc())
        monkeypatch.setattr(gt, "terminate_process_tree", lambda p: None)
        _, err, rc = gt._run_git_command(["status"], cwd=repo, timeout=1)
        assert rc == 124
        assert "some-err" in err and "超时" in err


# ── 只读工具 ─────────────────────────────────────────────────────────────

class TestReadOnly:
    def test_status_ok(self, repo):
        assert "Git" in git_status.invoke({})

    def test_status_not_repo(self, tmp_path):
        token = use_workspace(tmp_path)
        try:
            out = git_status.invoke({})
        finally:
            reset_workspace(token)
        assert "不是 Git 仓库" in out

    def test_status_unsafe_cwd(self, repo):
        assert "不安全" in git_status.invoke({"cwd": "../evil"})

    def test_status_failure(self, repo, monkeypatch):
        monkeypatch.setattr(gt, "_run_git_command", lambda *a, **k: ("", "bad", 1))
        assert "失败" in git_status.invoke({})

    def test_diff_clean(self, repo):
        assert "没有未提交" in git_diff.invoke({})

    def test_diff_dirty(self, repo):
        (repo / "a.txt").write_text("changed\n", encoding="utf-8")
        out = git_diff.invoke({})
        assert "changed" in out

    def test_diff_single_file(self, repo):
        (repo / "a.txt").write_text("changed\n", encoding="utf-8")
        (repo / "b.txt").write_text("new\n", encoding="utf-8")
        out = git_diff.invoke({"file_path": "a.txt"})
        assert "a.txt" in out

    def test_diff_control_chars(self, repo):
        assert "控制字符" in git_diff.invoke({"file_path": "a\n.txt"})

    def test_diff_failure(self, repo, monkeypatch):
        monkeypatch.setattr(gt, "_run_git_command", lambda *a, **k: ("", "bad", 1))
        assert "失败" in git_diff.invoke({})

    def test_diff_not_repo(self, tmp_path):
        token = use_workspace(tmp_path)
        try:
            out = git_diff.invoke({})
        finally:
            reset_workspace(token)
        assert "不是 Git 仓库" in out

    def test_diff_unsafe(self, repo):
        assert "不安全" in git_diff.invoke({"cwd": "../evil"})

    def test_log_ok(self, repo):
        out = git_log.invoke({})
        assert "init" in out

    def test_log_bad_n_rejected_by_schema(self, repo):
        import pytest as _pytest
        from pydantic import ValidationError as _ValidationError

        with _pytest.raises(_ValidationError):
            git_log.invoke({"n": "oops"})

    def test_log_bad_n_falls_back(self, repo):
        out = git_log.func("oops")
        assert "init" in out

    def test_log_clamp(self, repo, monkeypatch):
        seen = {}

        def fake(args, cwd=None, timeout=30):
            seen["args"] = args
            return ("x", "", 0)

        monkeypatch.setattr(gt, "_run_git_command", fake)
        git_log.invoke({"n": 500})
        assert seen["args"][1] == "-100"
        git_log.invoke({"n": -5})
        assert seen["args"][1] == "-1"

    def test_log_empty(self, repo, monkeypatch):
        monkeypatch.setattr(gt, "_run_git_command", lambda *a, **k: ("", "", 0))
        assert "为空" in git_log.invoke({})

    def test_log_failure(self, repo, monkeypatch):
        monkeypatch.setattr(gt, "_run_git_command", lambda *a, **k: ("", "bad", 1))
        assert "失败" in git_log.invoke({})

    def test_log_not_repo(self, tmp_path):
        token = use_workspace(tmp_path)
        try:
            out = git_log.invoke({})
        finally:
            reset_workspace(token)
        assert "不是 Git 仓库" in out

    def test_branch_ok(self, repo):
        out = git_branch.invoke({})
        assert "当前分支" in out

    def test_branch_multiple(self, repo):
        _run("branch", "other", cwd=repo)
        out = git_branch.invoke({})
        assert "other" in out
        assert "当前分支" in out

    def test_branch_failure(self, repo, monkeypatch):
        monkeypatch.setattr(gt, "_run_git_command", lambda *a, **k: ("", "bad", 1))
        assert "失败" in git_branch.invoke({})

    def test_branch_not_repo(self, tmp_path):
        token = use_workspace(tmp_path)
        try:
            out = git_branch.invoke({})
        finally:
            reset_workspace(token)
        assert "不是 Git 仓库" in out

    def test_remote_none(self, repo):
        assert "没有配置远程" in git_remote.invoke({})

    def test_remote_failure(self, repo, monkeypatch):
        monkeypatch.setattr(gt, "_run_git_command", lambda *a, **k: ("", "bad", 1))
        assert "失败" in git_remote.invoke({})

    def test_remote_ok(self, repo, monkeypatch):
        monkeypatch.setattr(gt, "_run_git_command", lambda *a, **k: ("origin\turl (fetch)\n", "", 0))
        assert "origin" in git_remote.invoke({})

    def test_remote_not_repo(self, tmp_path):
        token = use_workspace(tmp_path)
        try:
            out = git_remote.invoke({})
        finally:
            reset_workspace(token)
        assert "不是 Git 仓库" in out


# ── 变更工具 ─────────────────────────────────────────────────────────────

class TestMutating:
    def test_checkout_denied(self, repo, monkeypatch):
        monkeypatch.setattr(gt, "enforce_tool_permission", lambda *a, **k: "denied")
        assert git_checkout.invoke({"branch": "x"}) == "denied"

    def test_checkout_bad_ref(self, repo, no_perm):
        assert "分支名" in git_checkout.invoke({"branch": "-b"})

    def test_checkout_dirty(self, repo, no_perm):
        (repo / "a.txt").write_text("dirty\n", encoding="utf-8")
        out = git_checkout.invoke({"branch": "other"})
        assert "未提交" in out

    def test_checkout_new_branch(self, repo, no_perm):
        out = git_checkout.invoke({"branch": "feature", "create_new": True})
        assert "创建并切换" in out

    def test_checkout_existing(self, repo, no_perm):
        _run("branch", "other", cwd=repo)
        out = git_checkout.invoke({"branch": "other"})
        assert "切换到" in out

    def test_checkout_failure(self, repo, no_perm, monkeypatch):
        monkeypatch.setattr(gt, "_run_git_command", lambda *a, **k: ("", "bad", 1))
        monkeypatch.setattr(gt, "_has_worktree_changes", lambda cwd: False)
        assert "失败" in git_checkout.invoke({"branch": "ghost"})

    def test_log_unsafe(self, repo):
        assert "不安全" in git_log.invoke({"cwd": "../evil"})

    def test_branch_unsafe(self, repo):
        assert "不安全" in git_branch.invoke({"cwd": "../evil"})

    def test_remote_unsafe(self, repo):
        assert "不安全" in git_remote.invoke({"cwd": "../evil"})

    def test_checkout_unsafe(self, repo, no_perm):
        assert "不安全" in git_checkout.invoke({"branch": "x", "cwd": "../evil"})

    def test_add_unsafe(self, repo, no_perm):
        assert "不安全" in git_add.invoke({"add_all": True, "cwd": "../evil"})

    def test_commit_unsafe(self, repo, no_perm):
        assert "不安全" in git_commit.invoke({"message": "x", "cwd": "../evil"})

    def test_pull_unsafe(self, repo, no_perm):
        assert "不安全" in git_pull.invoke({"cwd": "../evil"})

    def test_push_unsafe(self, repo, no_perm):
        assert "不安全" in git_push.invoke({"cwd": "../evil"})

    def test_add_not_repo(self, tmp_path, no_perm):
        token = use_workspace(tmp_path)
        try:
            out = git_add.invoke({"add_all": True})
        finally:
            reset_workspace(token)
        assert "不是 Git 仓库" in out

    def test_commit_not_repo(self, tmp_path, no_perm):
        token = use_workspace(tmp_path)
        try:
            out = git_commit.invoke({"message": "x"})
        finally:
            reset_workspace(token)
        assert "不是 Git 仓库" in out

    def test_stash_not_repo(self, tmp_path, no_perm):
        token = use_workspace(tmp_path)
        try:
            out = git_stash.invoke({})
        finally:
            reset_workspace(token)
        assert "不是 Git 仓库" in out

    def test_pull_not_repo(self, tmp_path, no_perm):
        token = use_workspace(tmp_path)
        try:
            out = git_pull.invoke({})
        finally:
            reset_workspace(token)
        assert "不是 Git 仓库" in out

    def test_push_not_repo(self, tmp_path, no_perm):
        token = use_workspace(tmp_path)
        try:
            out = git_push.invoke({})
        finally:
            reset_workspace(token)
        assert "不是 Git 仓库" in out

    def test_checkout_not_repo(self, tmp_path, no_perm):
        token = use_workspace(tmp_path)
        try:
            out = git_checkout.invoke({"branch": "x"})
        finally:
            reset_workspace(token)
        assert "不是 Git 仓库" in out

    def test_add_all(self, repo, no_perm):
        (repo / "a.txt").write_text("v2\n", encoding="utf-8")
        assert "暂存所有" in git_add.invoke({"add_all": True})

    def test_add_files(self, repo, no_perm):
        (repo / "a.txt").write_text("v2\n", encoding="utf-8")
        out = git_add.invoke({"files": ["a.txt"]})
        assert "a.txt" in out

    def test_add_neither(self, repo, no_perm):
        assert "请指定" in git_add.invoke({})

    def test_add_control_chars(self, repo, no_perm):
        assert "控制字符" in git_add.invoke({"files": ["a\nb"]})

    def test_add_failure(self, repo, no_perm, monkeypatch):
        monkeypatch.setattr(gt, "_run_git_command", lambda *a, **k: ("", "bad", 1))
        assert "失败" in git_add.invoke({"add_all": True})

    def test_add_denied(self, repo, monkeypatch):
        monkeypatch.setattr(gt, "enforce_tool_permission", lambda *a, **k: "denied")
        assert git_add.invoke({"add_all": True}) == "denied"

    def test_commit_ok(self, repo, no_perm):
        (repo / "a.txt").write_text("v2\n", encoding="utf-8")
        _run("add", ".", cwd=repo)
        out = git_commit.invoke({"message": "second"})
        assert "提交成功" in out
        assert "提交哈希" in out

    def test_commit_no_staged(self, repo, no_perm):
        assert "暂存" in git_commit.invoke({"message": "x"})

    def test_commit_empty_message(self, repo, no_perm):
        (repo / "a.txt").write_text("v2\n", encoding="utf-8")
        _run("add", ".", cwd=repo)
        assert "不能为空" in git_commit.invoke({"message": "  "})

    def test_commit_amend(self, repo, no_perm):
        (repo / "a.txt").write_text("v2\n", encoding="utf-8")
        _run("add", ".", cwd=repo)
        out = git_commit.invoke({"message": "amended", "amend": True})
        assert "提交成功" in out

    def test_commit_no_hash_match(self, repo, no_perm, monkeypatch):
        (repo / "a.txt").write_text("v2\n", encoding="utf-8")
        _run("add", ".", cwd=repo)
        def fake_status(args, cwd=None, timeout=30):
            if args and args[0] == "status":
                return ("Changes to be committed", "", 0)
            if args and list(args[:2]) == ["diff", "--cached"]:
                return ("", "", 1)
            return ("committed quietly", "", 0)

        monkeypatch.setattr(gt, "_run_git_command", fake_status)
        out = git_commit.invoke({"message": "x"})
        assert "提交成功" in out
        assert "提交哈希" not in out

    def test_commit_failure(self, repo, no_perm, monkeypatch):
        (repo / "a.txt").write_text("v2\n", encoding="utf-8")
        _run("add", ".", cwd=repo)
        monkeypatch.setattr(gt, "_run_git_command", lambda *a, **k: ("", "bad", 1))
        assert "失败" in git_commit.invoke({"message": "x"})

    def test_commit_denied(self, repo, monkeypatch):
        monkeypatch.setattr(gt, "enforce_tool_permission", lambda *a, **k: "denied")
        assert git_commit.invoke({"message": "x"}) == "denied"

    def test_stash_push(self, repo, no_perm):
        (repo / "a.txt").write_text("dirty\n", encoding="utf-8")
        out = git_stash.invoke({"message": "wip"})
        assert "已暂存" in out

    def test_stash_plain(self, repo, no_perm):
        (repo / "a.txt").write_text("dirty\n", encoding="utf-8")
        out = git_stash.invoke({})
        assert "未提供说明" in out

    def test_stash_pop(self, repo, no_perm):
        (repo / "a.txt").write_text("dirty\n", encoding="utf-8")
        git_stash.invoke({})
        out = git_stash.invoke({"pop": True})
        assert "已恢复" in out
        assert (repo / "a.txt").read_text(encoding="utf-8") == "dirty\n"

    def test_stash_failure(self, repo, no_perm, monkeypatch):
        monkeypatch.setattr(gt, "_run_git_command", lambda *a, **k: ("", "bad", 1))
        assert "失败" in git_stash.invoke({})

    def test_stash_denied(self, repo, monkeypatch):
        monkeypatch.setattr(gt, "enforce_tool_permission", lambda *a, **k: "denied")
        assert git_stash.invoke({}) == "denied"

    def test_pull_dirty(self, repo, no_perm):
        (repo / "a.txt").write_text("dirty\n", encoding="utf-8")
        assert "未提交" in git_pull.invoke({})

    def test_pull_no_remote(self, repo, no_perm):
        assert "失败" in git_pull.invoke({})

    def test_pull_ok(self, repo, no_perm, monkeypatch):
        monkeypatch.setattr(gt, "_run_git_command", lambda *a, **k: ("up to date", "", 0))
        monkeypatch.setattr(gt, "_has_worktree_changes", lambda cwd: False)
        out = git_pull.invoke({"rebase": True})
        assert "已拉取" in out

    def test_pull_empty_stdout(self, repo, no_perm, monkeypatch):
        monkeypatch.setattr(gt, "_run_git_command", lambda *a, **k: ("", "", 0))
        monkeypatch.setattr(gt, "_has_worktree_changes", lambda cwd: False)
        assert "已是最新" in git_pull.invoke({})

    def test_pull_denied(self, repo, monkeypatch):
        monkeypatch.setattr(gt, "enforce_tool_permission", lambda *a, **k: "denied")
        assert git_pull.invoke({}) == "denied"

    def test_push_no_remote(self, repo, no_perm):
        assert "失败" in git_push.invoke({})

    def test_push_ok(self, repo, no_perm, monkeypatch):
        monkeypatch.setattr(gt, "_run_git_command", lambda *a, **k: ("", "", 0))
        out = git_push.invoke({"set_upstream": True})
        assert "已推送" in out

    def test_push_denied(self, repo, monkeypatch):
        monkeypatch.setattr(gt, "enforce_tool_permission", lambda *a, **k: "denied")
        assert git_push.invoke({}) == "denied"

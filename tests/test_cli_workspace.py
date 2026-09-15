"""``lib/cli/workspace.py`` 的行为回归。

这里的用例都来自**真实链路**：

* ``echo "我的问题" | sayacode`` 会在退出路径上被一个 git 提交确认截走 stdin；
* 交互启动时总会重问一遍工作区路径，即使配置里已经记住了它。
"""

from types import SimpleNamespace

from lib.cli import workspace as workspace_module


class _Args:
    """最小的 args 替身（只需要 ``workspace``）。"""

    def __init__(self, workspace=None):
        self.workspace = workspace


def _patch(monkeypatch, *, interactive: bool, has_changes: bool) -> list:
    """装好探针：返回被记录下来的提问次数。"""
    monkeypatch.setattr(
        workspace_module, "_supports_interactive_input", lambda: interactive
    )
    monkeypatch.setattr(
        workspace_module, "check_git_changes", lambda workspace: has_changes
    )
    prompted: list = []
    monkeypatch.setattr(
        workspace_module,
        "confirm_action",
        lambda *args, **kwargs: prompted.append(args) or False,
    )
    return prompted


def test_suggest_git_commit_does_not_prompt_when_stdin_is_not_interactive(
    monkeypatch, tmp_path
):
    """非交互 stdin 下不得提问。

    回归保护：本函数位于退出路径的最后，且会 ``input()``。实测
    ``echo "我的问题" | sayacode`` 时，管道里剩下的内容被这一问当成「y/n」吃掉
    并误判（打印 "Please enter Y or N"），用户真正的 prompt 根本没机会执行。
    """
    prompted = _patch(monkeypatch, interactive=False, has_changes=True)

    workspace_module.suggest_git_commit(tmp_path)

    assert prompted == []


def test_suggest_git_commit_still_prompts_when_interactive(monkeypatch, tmp_path):
    """交互模式下行为不变 —— 避免为了修 bug 把功能一起关掉。"""
    prompted = _patch(monkeypatch, interactive=True, has_changes=True)

    workspace_module.suggest_git_commit(tmp_path)

    assert len(prompted) == 1


def test_suggest_git_commit_does_nothing_without_changes(monkeypatch, tmp_path):
    """没有改动时不该提问（原有行为，一并守住）。"""
    prompted = _patch(monkeypatch, interactive=True, has_changes=False)

    workspace_module.suggest_git_commit(tmp_path)

    assert prompted == []


# ── 记住的工作区必须被读取 ────────────────────────────────────────────────────
#
# 回归保护：persist_local_state 一直在写 user_config.workspace，但此前**没有任何
# 地方读它**，于是每次交互启动都要重问一次工作区路径（真实 PTY 驱动时复现：
# 第一次问答把会话整个带偏）。


def _spy_on_prompt(monkeypatch) -> list:
    """把工作区提问替换成探针，返回被调用时收到的默认值列表。"""
    called: list = []

    def fake_get_workspace_path(default_path=None):
        called.append(default_path)
        return default_path

    monkeypatch.setattr(workspace_module, "get_workspace_path", fake_get_workspace_path)
    return called


def test_remembered_workspace_equal_to_cwd_skips_the_prompt(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    called = _spy_on_prompt(monkeypatch)

    result = workspace_module.resolve_launch_workspace(
        _Args(), SimpleNamespace(workspace=str(tmp_path))
    )

    assert result == tmp_path.resolve()
    assert called == [], "记住的工作区就是当前目录时不该再问一次"


def test_remembered_workspace_equal_to_cwd_works_for_a_dict_config(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    called = _spy_on_prompt(monkeypatch)

    result = workspace_module.resolve_launch_workspace(_Args(), {"workspace": str(tmp_path)})

    assert result == tmp_path.resolve()
    assert called == []


def test_remembered_workspace_that_differs_still_prompts(monkeypatch, tmp_path):
    """记住的目录 ≠ 当前目录时仍然询问 —— 不静默把用户换到别的目录。"""
    monkeypatch.chdir(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    called = _spy_on_prompt(monkeypatch)

    result = workspace_module.resolve_launch_workspace(
        _Args(), SimpleNamespace(workspace=str(other))
    )

    assert called == [tmp_path.resolve()], "应当以当前目录为默认值询问"
    assert result == tmp_path.resolve()


def test_missing_remembered_workspace_still_prompts(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    called = _spy_on_prompt(monkeypatch)

    result = workspace_module.resolve_launch_workspace(
        _Args(), SimpleNamespace(workspace=None)
    )

    assert called == [tmp_path.resolve()]
    assert result == tmp_path.resolve()


def test_invalid_remembered_workspace_still_prompts(monkeypatch, tmp_path):
    """配置里的值是垃圾时当作没有，不得让它传下去。"""
    monkeypatch.chdir(tmp_path)
    called = _spy_on_prompt(monkeypatch)

    workspace_module.resolve_launch_workspace(_Args(), SimpleNamespace(workspace=""))

    assert called == [tmp_path.resolve()]


def test_explicit_workspace_wins_over_the_remembered_one(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    called = _spy_on_prompt(monkeypatch)
    target = tmp_path / "explicit"

    result = workspace_module.resolve_launch_workspace(
        _Args(str(target)), SimpleNamespace(workspace=str(tmp_path))
    )

    assert result == target.resolve()
    assert target.is_dir(), "显式指定的目录不存在时应被创建"
    assert called == [], "显式指定时不必询问"

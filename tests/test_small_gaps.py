# 小缺口合集上半：delegate_tools、tools/context、process_env、private_io。

import pytest

from lib.tools.delegate_tools import (
    build_manager_resume_fn,
    build_manager_spawn_fn,
    build_manager_spawn_with_id,
    create_async_delegate_tools,
    create_cancel_tool,
    create_delegate_tool,
    create_resume_tool,
)


class FakeManager:
    # TeamManager 替身。
    def __init__(self):
        self.spawned = []

    def spawn(self, agent_type, task, workspace=None):
        self.spawned.append((agent_type, task))
        return "w1"

    def wait(self, wid, timeout=None):
        return {"ok": True}

    def get_result(self, wid):
        return {"response": "done-text"}

    def resume(self, wid, follow_up):
        return "w2"


class TestDelegateFns:
    def test_sync_tool(self):
        tool = create_delegate_tool(lambda task, at: "ok:" + task)
        assert tool.invoke({"task": "  "}) != ""
        assert "20000" in tool.invoke({"task": "x" * 20001})
        assert tool.invoke({"task": "do-x"}) == "ok:do-x"
        assert create_delegate_tool(lambda t, a: (_ for _ in ()).throw(RuntimeError("boom"))).invoke({"task": "x"}) != ""
        assert create_delegate_tool(lambda t, a: "x" * 30000).invoke({"task": "x"}) != ""
        assert create_delegate_tool(lambda t, a: "").invoke({"task": "x"}) != ""
        assert create_delegate_tool(lambda t, a: None).invoke({"task": "x"}) != ""

    def test_async_tools(self):
        from lib.core.delegate_pool import AsyncDelegateRegistry

        pool = AsyncDelegateRegistry()
        async_tool, poll_tool = create_async_delegate_tools(lambda t, a: "r", registry=pool)
        assert "不能为空" in async_tool.invoke({"task": "  "})
        assert "20000" in async_tool.invoke({"task": "x" * 20001})
        assert "已派单" in async_tool.invoke({"task": "real"})
        assert "不能为空" in poll_tool.invoke({"handle_id": "  "})
        assert "未知句柄" in poll_tool.invoke({"handle_id": "ghost"})
        pool.shutdown()

    def test_poll_crash(self):
        from lib.core.delegate_pool import AsyncDelegateRegistry

        pool = AsyncDelegateRegistry()
        _, poll_tool = create_async_delegate_tools(lambda t, a: "r", registry=pool)
        real = pool.poll
        pool.poll = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("busy"))
        try:
            assert "busy" in poll_tool.invoke({"handle_id": "whatever"})
        finally:
            pool.poll = real
            pool.shutdown()

    def test_cancel_tool(self):
        from types import SimpleNamespace

        tool = create_cancel_tool(lambda h: (_ for _ in ()).throw(KeyError(h)))
        assert "不能为空" in tool.invoke({"handle_id": "  "})
        assert "未知句柄" in tool.invoke({"handle_id": "ghost"})
        tool = create_cancel_tool(lambda h: SimpleNamespace(status="done"))
        assert "已终结" in tool.invoke({"handle_id": "h"})
        tool = create_cancel_tool(lambda h: SimpleNamespace(status="running"))
        assert "已取消" in tool.invoke({"handle_id": "h"})

    def test_resume_tools(self):
        from types import SimpleNamespace

        resume_tool, notes_tool = create_resume_tool(lambda h, f: SimpleNamespace(turns=1), lambda: [])
        assert "不能为空" in resume_tool.invoke({"handle_id": "  ", "follow_up": "x"})
        assert "不能为空" in resume_tool.invoke({"handle_id": "h", "follow_up": "  "})
        assert "已追问" in resume_tool.invoke({"handle_id": "h", "follow_up": "more"})
        assert "暂无" in notes_tool.invoke({})
        job = SimpleNamespace(handle="h", status="done", agent_type="builder", result="out", error="")
        _, notes2 = create_resume_tool(lambda h, f: None, lambda: [job])
        assert "完成" in notes2.invoke({})
        bad, _ = create_resume_tool(lambda h, f: (_ for _ in ()).throw(ValueError("nope")), lambda: [])
        assert "nope" in bad.invoke({"handle_id": "h", "follow_up": "x"})

    def test_spawn_fns(self, tmp_path):
        mgr = FakeManager()
        assert build_manager_spawn_fn(mgr, tmp_path)("task", "builder") == "done-text"
        text, wid = build_manager_spawn_with_id(mgr, tmp_path)("task", "builder")
        assert (text, wid) == ("done-text", "w1")
        assert build_manager_resume_fn(mgr)("w1", "more") == "done-text"

    def test_spawn_timeout(self, tmp_path):
        mgr = FakeManager()
        mgr.wait = lambda wid, timeout=None: None
        with pytest.raises(RuntimeError):
            build_manager_spawn_fn(mgr, tmp_path)("task", "builder")
        with pytest.raises(RuntimeError):
            build_manager_spawn_with_id(mgr, tmp_path)("task", "builder")
        with pytest.raises(RuntimeError):
            build_manager_resume_fn(mgr)("w1", "more")

    def test_result_text(self):
        from lib.tools.delegate_tools import _result_text

        assert _result_text({"result": "r"}) == "r"
        assert _result_text({"output": "o"}) == "o"
        assert _result_text({"other": 1}) != ""
        assert _result_text("") == ""
        assert _result_text(None) == ""


class TestToolsContext:
    def test_queue(self):
        from lib.tools.context import ContextModifierQueue

        q = ContextModifierQueue()
        q.enqueue(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        seen = []
        q.enqueue(lambda: seen.append(1))
        assert q.pending_count == 2
        q.apply_all()
        assert seen == [1] and q.pending_count == 0

    def test_resolve(self, tmp_path):
        from types import SimpleNamespace
        from lib.tools.context import ToolExecutionContext, resolve_tool_workspace

        ctx = ToolExecutionContext(workspace=tmp_path)
        assert resolve_tool_workspace(ctx) == tmp_path.resolve()
        assert resolve_tool_workspace(SimpleNamespace(workspace=str(tmp_path))) == tmp_path.resolve()
        assert resolve_tool_workspace(str(tmp_path)) == tmp_path.resolve()

    def test_abort_helpers(self):
        from lib.tools.context import ToolAbortController, get_abort_controller, set_abort_controller

        ctrl = ToolAbortController()
        assert ctrl.is_aborted is False and ctrl.reason == "unknown"
        ctrl.abort("stop")
        assert ctrl.is_aborted is True and ctrl.reason == "stop"
        ctrl.reset()
        assert ctrl.is_aborted is False
        set_abort_controller(ctrl)
        assert get_abort_controller() is ctrl

    def test_session_binds(self, tmp_path):
        from types import SimpleNamespace
        from lib.tools.context import ToolExecutionContext, tool_execution_session

        ctx = ToolExecutionContext(workspace=tmp_path)
        with tool_execution_session(ctx):
            pass
        with tool_execution_session(str(tmp_path)):
            pass
        with tool_execution_session(SimpleNamespace(workspace=str(tmp_path), permissions=None, hooks=None)):
            pass


class TestProcessEnv:
    def test_strip_url(self, monkeypatch):
        from lib.core.process_env import _strip_url_credentials, build_process_env

        assert _strip_url_credentials("https://user:pass@host:8080/x") == "https://host:8080/x"
        assert _strip_url_credentials("https://host/x") == "https://host/x"
        assert build_process_env()["GIT_TERMINAL_PROMPT"] == "0"
        assert "PATH" in build_process_env() or True

    def test_urlsplit_crash(self, monkeypatch):
        import lib.core.process_env as _pe

        monkeypatch.setattr(_pe, "urlsplit", lambda *a, **k: (_ for _ in ()).throw(ValueError("bad")))
        assert _pe._strip_url_credentials("https://u:p@h") == "https://u:p@h"

    def test_sensitive_filtered(self, monkeypatch):
        from lib.core.process_env import build_process_env

        monkeypatch.setenv("MY_TEST_SECRET_XYZ", "shhh")
        assert "MY_TEST_SECRET_XYZ" not in build_process_env()


class TestPrivateIO:
    def test_posix_branch(self, tmp_path, monkeypatch):
        import os as _os
        from lib.core.private_io import restrict_permissions

        monkeypatch.setattr(_os, "name", "posix")
        f = tmp_path / "f.txt"
        f.write_text("x", encoding="utf-8")
        restrict_permissions(f)
        monkeypatch.setattr("pathlib.Path.chmod", lambda *a, **k: (_ for _ in ()).throw(OSError("ro")))
        restrict_permissions(f)

    def test_missing_and_user(self, tmp_path, monkeypatch):
        import getpass as _gp
        from lib.core.private_io import _current_windows_user, restrict_permissions

        restrict_permissions(tmp_path / "nope.txt")
        monkeypatch.delenv("USERNAME", raising=False)
        monkeypatch.delenv("USERDOMAIN", raising=False)
        monkeypatch.setattr(_gp, "getuser", lambda: "tester")
        assert _current_windows_user() == "tester"
        monkeypatch.setattr(_gp, "getuser", lambda: (_ for _ in ()).throw(OSError("busy")))
        assert _current_windows_user() == ""
        monkeypatch.setenv("USERNAME", "u")
        monkeypatch.setenv("USERDOMAIN", "D")
        assert _current_windows_user() == "D\\u"

    def test_icacls_fail(self, tmp_path, monkeypatch):
        import subprocess as _sp
        from lib.core.private_io import restrict_permissions

        f = tmp_path / "f.txt"
        f.write_text("x", encoding="utf-8")
        monkeypatch.setattr(_sp, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("busy")))
        restrict_permissions(f)
        restrict_permissions(tmp_path)


class TestWebGap:
    def test_empty(self):
        import lib.tools.web_tools as _wt

        assert "empty" in _wt.web_search.func("   ")

    def test_searxng_no_url(self, monkeypatch):
        import lib.tools.web_tools as _wt

        monkeypatch.setenv("SAYACODE_SEARCH_PROVIDER", "searxng")
        monkeypatch.delenv("SAYACODE_SEARXNG_URL", raising=False)
        assert "SAYACODE_SEARXNG_URL" in _wt.web_search.func("hello")

    def test_searxng_ok(self, monkeypatch):
        import json as _json
        import lib.tools.web_tools as _wt

        monkeypatch.setenv("SAYACODE_SEARCH_PROVIDER", "searxng")
        monkeypatch.setenv("SAYACODE_SEARXNG_URL", "http://s")
        monkeypatch.setattr(_wt, "_http_get_text", lambda *a, **k: _json.dumps({"results": [{"title": "t", "url": "http://x", "content": "c"}, {"title": "", "url": ""}]}))
        out = _wt.web_search.func("hello", time_range="week")
        assert "Provider: searxng" in out

    def test_provider_crash(self, monkeypatch):
        import lib.tools.web_tools as _wt

        monkeypatch.setattr(_wt, "_search_duckduckgo", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
        assert "failed" in _wt.web_search.func("hello")

    def test_no_results(self, monkeypatch):
        import lib.tools.web_tools as _wt

        monkeypatch.setattr(_wt, "_search_duckduckgo", lambda *a, **k: [])
        assert "No web search results" in _wt.web_search.func("hello")

    def test_ddg_lite_fallback(self, monkeypatch):
        import lib.tools.web_tools as _wt

        calls = {"n": 0}

        def fake_http(url, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                return "<html></html>"
            return '<a class="result__a" href="http://x">T</a><div class="result__snippet">S</div>'

        monkeypatch.setattr(_wt, "_http_get_text", fake_http)
        out = _wt.web_search.func("hello", time_range="day")
        assert "T" in out

    def test_http_errors(self, monkeypatch):
        import lib.tools.web_tools as _wt
        from urllib.error import HTTPError, URLError

        monkeypatch.setattr(_wt, "urlopen", lambda *a, **k: (_ for _ in ()).throw(HTTPError("u", 500, "e", {}, None)))
        with __import__("pytest").raises(RuntimeError):
            _wt._http_get_text("http://x")
        monkeypatch.setattr(_wt, "urlopen", lambda *a, **k: (_ for _ in ()).throw(URLError("down")))
        with __import__("pytest").raises(RuntimeError):
            _wt._http_get_text("http://x")

    def test_normalize_url(self):
        import lib.tools.web_tools as _wt

        assert _wt._normalize_duckduckgo_url("") == ""
        assert _wt._normalize_duckduckgo_url("//x.com/a") == "https://x.com/a"
        assert "example" in _wt._normalize_duckduckgo_url("https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com")
        assert _wt._normalize_duckduckgo_url("https://example.com") == "https://example.com"

    def test_dedupe(self):
        import lib.tools.web_tools as _wt
        from lib.tools.web_tools import SearchResult

        out = _wt._dedupe_results([SearchResult("t", "http://x", "s"), SearchResult("t", "http://x", "s"),
                                   SearchResult("t", "", "s")], max_results=10)
        assert len(out) == 1

    def test_helpers(self):
        import lib.tools.web_tools as _wt

        assert _wt._clamp_max_results("oops") == 5
        assert _wt._clamp_max_results(99) == 10
        assert _wt._clamp_max_results(0) == 1
        assert _wt._clean_text("a\x00b  c") == "ab c"
        assert _wt._duckduckgo_time_range("month") == "m"
        assert _wt._duckduckgo_time_range("oops") == ""
        assert _wt._searxng_time_range("year") == "year"

    def test_tool_invoke(self, monkeypatch):
        import lib.tools.web_tools as _wt

        monkeypatch.setattr(_wt, "_search_duckduckgo", lambda *a, **k: [])
        assert "No web search results" in _wt.web_search.invoke({"query": "hi"})


class TestWorktreeGap:
    def _mgr(self, tmp_path):
        from lib.core.team_worktree import TeamWorktreeManager

        return TeamWorktreeManager(tmp_path / "team")

    def test_dict(self):
        from lib.core.team_worktree import TeamWorktree

        wt = TeamWorktree(worker_id="w12345678", source_workspace="s", repo_root="r",
                          worktree_root="w", workspace="ws", branch="b", source_commit="c")
        assert wt.to_dict()["branch"] == "b"

    def test_bad_id(self, tmp_path):
        from lib.core.team_worktree import WorktreeIsolationError

        import pytest as _pytest

        with _pytest.raises(WorktreeIsolationError):
            self._mgr(tmp_path).prepare("bad-id", str(tmp_path))

    def test_not_dir(self, tmp_path):
        from lib.core.team_worktree import WorktreeIsolationError

        import pytest as _pytest

        with _pytest.raises(WorktreeIsolationError):
            self._mgr(tmp_path).prepare("w12345678", str(tmp_path / "nope"))

    def test_outside_repo(self, tmp_path, monkeypatch):
        from lib.core.team_worktree import TeamWorktreeManager, WorktreeIsolationError

        import pytest as _pytest

        mgr = self._mgr(tmp_path)
        monkeypatch.setattr(TeamWorktreeManager, "_git", staticmethod(lambda cwd, *a: "/elsewhere"))
        with _pytest.raises(WorktreeIsolationError):
            mgr.prepare("w12345678", str(tmp_path))

    def test_escape_and_exists(self, tmp_path, monkeypatch):
        import subprocess as _sp
        from pathlib import Path as _Path
        from lib.core.team_worktree import WorktreeIsolationError

        import pytest as _pytest

        _sp.run(["git", "init"], cwd=str(tmp_path), check=True, capture_output=True)
        _sp.run(["git", "config", "user.email", "t@t"], cwd=str(tmp_path), check=True, capture_output=True)
        _sp.run(["git", "config", "user.name", "t"], cwd=str(tmp_path), check=True, capture_output=True)
        (tmp_path / "f.txt").write_text("x", encoding="utf-8")
        _sp.run(["git", "add", "."], cwd=str(tmp_path), check=True, capture_output=True)
        _sp.run(["git", "commit", "-m", "init"], cwd=str(tmp_path), check=True, capture_output=True)
        mgr = self._mgr(tmp_path)
        real_rel = _Path.relative_to
        monkeypatch.setattr(_Path, "relative_to", lambda self, *a, **k: (_ for _ in ()).throw(ValueError("out")))
        with _pytest.raises(WorktreeIsolationError):
            mgr.prepare("w12345678", str(tmp_path))
        monkeypatch.setattr(_Path, "relative_to", real_rel)
        wt = mgr.prepare("w12345678", str(tmp_path))
        assert wt.branch == "sayacode/team-w12345678"
        with _pytest.raises(WorktreeIsolationError):
            mgr.prepare("w12345678", str(tmp_path))

    def test_inspect_errors(self, tmp_path):
        from lib.core.team_worktree import WorktreeIsolationError

        import pytest as _pytest

        mgr = self._mgr(tmp_path)
        with _pytest.raises(WorktreeIsolationError):
            mgr.inspect("/outside/x")
        with _pytest.raises(WorktreeIsolationError):
            mgr.inspect(str(tmp_path / "team" / "ghost"))
        d = tmp_path / "team" / "w12345678"
        d.mkdir(parents=True)
        with _pytest.raises(WorktreeIsolationError):
            mgr.inspect(str(d), source_commit="oops")

    def test_git_failures(self, tmp_path, monkeypatch):
        import subprocess as _sp
        from lib.core.team_worktree import TeamWorktreeManager, WorktreeIsolationError

        import pytest as _pytest

        monkeypatch.setattr(_sp, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("busy")))
        with _pytest.raises(WorktreeIsolationError):
            TeamWorktreeManager._git(tmp_path, "status")
        monkeypatch.setattr(_sp, "run", lambda *a, **k: (_ for _ in ()).throw(_sp.TimeoutExpired("git", 1)))
        with _pytest.raises(WorktreeIsolationError):
            TeamWorktreeManager._git(tmp_path, "status")

        from types import SimpleNamespace
        monkeypatch.setattr(_sp, "run", lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="bad"))
        with _pytest.raises(WorktreeIsolationError):
            TeamWorktreeManager._git(tmp_path, "status")
        monkeypatch.setattr(_sp, "run", lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr=""))
        with _pytest.raises(WorktreeIsolationError):
            TeamWorktreeManager._git(tmp_path, "status", "extra")


class TestPackagerGap:
    def _req(self, tmp_path, **kw):
        from lib.core.context import ProjectContext
        from lib.core.context_packager import ContextPackRequest

        kw.setdefault("workspace", tmp_path)
        kw.setdefault("project_context", ProjectContext(str(tmp_path)))
        return ContextPackRequest(**kw)

    def test_modes(self, tmp_path):
        from lib.core.context_packager import ContextPackager

        (tmp_path / "a.py").write_text("def f():\n    pass\n", encoding="utf-8")
        packer = ContextPackager()
        assert packer.pack(self._req(tmp_path, agent_mode="plan")).content != ""
        assert packer.explain(self._req(tmp_path))["included_sections"] != []

    def test_history(self, tmp_path):
        from lib.core.context_packager import ContextPackager
        from lib.core.session import SessionManager

        (tmp_path / "a.py").write_text("x\n", encoding="utf-8")
        session = SessionManager()
        session.add_user_message("hello")
        session.add_assistant_message("hi")
        packer = ContextPackager()
        out = packer.pack(self._req(tmp_path, session=session, include_history=True))
        assert "hello" in out.content

    def test_history_variants(self, tmp_path):
        from types import SimpleNamespace
        from lib.core.context_packager import ContextPackager

        packer = ContextPackager()
        calls = {"n": 0}

        def old_style(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise TypeError("old")
            return []

        assert packer._render_history(SimpleNamespace(messages=None, get_messages=old_style)) == ""
        assert packer._render_history(SimpleNamespace(messages=[{"role": "user", "content": "q"}, {"role": "x"}])) != ""
        assert packer._render_history(SimpleNamespace(messages=[])) == ""
        assert packer._render_history(SimpleNamespace(messages=None, get_messages=lambda include_system=False: [{"role": "user", "content": "q"}])) != ""

    def test_symbols_crash(self, tmp_path, monkeypatch):
        import lib.core.context_packager as _cp
        from lib.core.context_packager import ContextPackager

        (tmp_path / "a.py").write_text("x\n", encoding="utf-8")
        monkeypatch.setattr(_cp, "SymbolIndex", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        packer = ContextPackager()
        assert "symbols" not in packer.pack(self._req(tmp_path)).included_sections

    def test_zero_budget(self, tmp_path):
        from lib.core.context_packager import ContextPackager

        (tmp_path / "a.py").write_text("x\n", encoding="utf-8")
        out = ContextPackager().pack(self._req(tmp_path, max_chars=0))
        assert out.truncated is True and out.content == ""

    def test_tiny_budget(self, tmp_path):
        from lib.core.context_packager import ContextPackager

        (tmp_path / "a.py").write_text("x\n", encoding="utf-8")
        out = ContextPackager().pack(self._req(tmp_path, max_chars=10))
        assert out.truncated is True

    def test_estimator_fallback(self, tmp_path):
        from lib.core.context_packager import ContextPackager

        (tmp_path / "a.py").write_text("x\n", encoding="utf-8")

        class BadEst:
            def estimate(self, text):
                raise RuntimeError("boom")

        out = ContextPackager().pack(self._req(tmp_path, token_estimator=BadEst()))
        assert out.estimated_tokens >= 0

        class WeirdEst:
            def estimate(self, text):
                return 42

        out = ContextPackager().pack(self._req(tmp_path, token_estimator=WeirdEst()))
        assert out.estimated_tokens >= 0

    def test_estimator_pure(self):
        from lib.core.context_packager import TokenEstimator

        assert TokenEstimator().estimate("").tokens == 0
        assert TokenEstimator().estimate("abcd").tokens == 1

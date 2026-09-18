# _process 全覆盖：fake 进程 + 平台补丁走完终止树所有分支。

import sys

import pytest

import lib.tools._process as pr
from lib.tools._process import popen_platform_kwargs, terminate_process_tree


class FakeProc:
    # 可编排 poll 序列的假进程。
    def __init__(self, polls):
        self._polls = list(polls)
        self.pid = 424242
        self.killed = 0
        self.terminated = 0

    def poll(self):
        if self._polls:
            return self._polls.pop(0)
        return 0

    def kill(self):
        self.killed += 1

    def terminate(self):
        self.terminated += 1


@pytest.fixture
def posix_env(monkeypatch):
    # 把平台切到 POSIX 并补齐 Windows 缺失的 SIGKILL。
    import signal as _sig

    monkeypatch.setattr(sys, "platform", "linux")
    if not hasattr(_sig, "SIGKILL"):
        monkeypatch.setattr(_sig, "SIGKILL", 9, raising=False)
    return _sig


class TestPopenKwargs:
    def test_win_branch(self):
        if not sys.platform.startswith("win"):
            pytest.skip("windows only")
        assert "creationflags" in popen_platform_kwargs()

    def test_posix_branch(self, monkeypatch, posix_env):
        monkeypatch.setattr(sys, "platform", "linux")
        assert popen_platform_kwargs() == {"start_new_session": True}


class TestTerminate:
    def test_already_dead(self):
        terminate_process_tree(FakeProc([0]))

    def test_win_taskkill_ok(self, monkeypatch):
        if not sys.platform.startswith("win"):
            pytest.skip("windows only")
        seen = {}

        def fake_run(*args, **kwargs):
            seen["args"] = args[0]
            return None

        monkeypatch.setattr(pr.subprocess, "run", fake_run)
        proc = FakeProc([None])
        terminate_process_tree(proc)
        assert seen["args"][:2] == ["taskkill", "/PID"]
        assert proc.killed == 0

    def test_win_taskkill_fails_falls_back_to_kill(self, monkeypatch):
        if not sys.platform.startswith("win"):
            pytest.skip("windows only")

        def boom(*args, **kwargs):
            raise RuntimeError("no taskkill")

        monkeypatch.setattr(pr.subprocess, "run", boom)
        proc = FakeProc([None])
        terminate_process_tree(proc)
        assert proc.killed == 1

    def test_win_kill_also_fails_swallowed(self, monkeypatch):
        if not sys.platform.startswith("win"):
            pytest.skip("windows only")

        def boom(*args, **kwargs):
            raise RuntimeError("no taskkill")

        class Stubborn(FakeProc):
            def kill(self):
                raise RuntimeError("gone")

        monkeypatch.setattr(pr.subprocess, "run", boom)
        terminate_process_tree(Stubborn([None]))

    def test_posix_term_then_dies(self, monkeypatch, posix_env):
        monkeypatch.setattr(sys, "platform", "linux")
        signals = []

        def fake_killpg(pid, sig):
            signals.append(sig)

        monkeypatch.setattr(pr.os, "killpg", fake_killpg, raising=False)
        import signal as _sig

        proc = FakeProc([None, 0])
        terminate_process_tree(proc, grace_seconds=5)
        assert signals == [_sig.SIGTERM]
        assert proc.killed == 0

    def test_posix_grace_loop_sleeps(self, monkeypatch, posix_env):
        monkeypatch.setattr(pr.os, "killpg", lambda pid, sig: None, raising=False)
        proc = FakeProc([None, None, 0])
        terminate_process_tree(proc, grace_seconds=5)
        assert proc.killed == 0

    def test_posix_term_lookup_error(self, monkeypatch, posix_env):
        monkeypatch.setattr(sys, "platform", "linux")

        def gone(pid, sig):
            raise ProcessLookupError()

        monkeypatch.setattr(pr.os, "killpg", gone, raising=False)
        terminate_process_tree(FakeProc([None]))

    def test_posix_term_fails_uses_terminate(self, monkeypatch, posix_env):
        monkeypatch.setattr(sys, "platform", "linux")

        def boom(pid, sig):
            raise RuntimeError("no pg")

        monkeypatch.setattr(pr.os, "killpg", boom, raising=False)
        proc = FakeProc([None])
        terminate_process_tree(proc, grace_seconds=0)
        assert proc.terminated == 1

    def test_posix_terminate_also_fails(self, monkeypatch, posix_env):
        monkeypatch.setattr(sys, "platform", "linux")

        def boom(pid, sig):
            raise RuntimeError("no pg")

        class Stubborn(FakeProc):
            def terminate(self):
                raise RuntimeError("gone")

        monkeypatch.setattr(pr.os, "killpg", boom, raising=False)
        terminate_process_tree(Stubborn([None]), grace_seconds=0)

    def test_posix_escalates_to_sigkill(self, monkeypatch, posix_env):
        monkeypatch.setattr(sys, "platform", "linux")
        import signal as _sig

        signals = []

        def fake_killpg(pid, sig):
            signals.append(sig)

        monkeypatch.setattr(pr.os, "killpg", fake_killpg, raising=False)
        terminate_process_tree(FakeProc([None]), grace_seconds=0)
        assert signals == [_sig.SIGTERM, _sig.SIGKILL]

    def test_posix_sigkill_lookup_error(self, monkeypatch, posix_env):
        monkeypatch.setattr(sys, "platform", "linux")
        import signal as _sig

        def flaky(pid, sig):
            if sig == _sig.SIGKILL:
                raise ProcessLookupError()

        monkeypatch.setattr(pr.os, "killpg", flaky, raising=False)
        terminate_process_tree(FakeProc([None]), grace_seconds=0)

    def test_posix_sigkill_fails_uses_kill(self, monkeypatch, posix_env):
        monkeypatch.setattr(sys, "platform", "linux")
        import signal as _sig

        def flaky(pid, sig):
            if sig == _sig.SIGKILL:
                raise RuntimeError("no pg")

        monkeypatch.setattr(pr.os, "killpg", flaky, raising=False)
        proc = FakeProc([None])
        terminate_process_tree(proc, grace_seconds=0)
        assert proc.killed == 1

    def test_posix_kill_also_fails_swallowed(self, monkeypatch, posix_env):
        monkeypatch.setattr(sys, "platform", "linux")
        import signal as _sig

        def flaky(pid, sig):
            if sig == _sig.SIGKILL:
                raise RuntimeError("no pg")

        class Stubborn(FakeProc):
            def kill(self):
                raise RuntimeError("gone")

        monkeypatch.setattr(pr.os, "killpg", flaky, raising=False)
        terminate_process_tree(Stubborn([None]), grace_seconds=0)

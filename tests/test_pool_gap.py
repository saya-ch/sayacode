"""委托池边角：背压、修剪、校验、快照面。"""

import threading

import pytest

from lib.core.delegate_pool import AsyncDelegateRegistry, get_delegate_registry


def test_backpressure_rejects_when_full(monkeypatch):
    import lib.core.delegate_pool as pool_mod

    registry = AsyncDelegateRegistry(max_workers=1)
    monkeypatch.setattr(pool_mod, "MAX_PENDING_JOBS", 1)
    gate = threading.Event()

    def slow(task, kind):
        gate.wait(timeout=10)
        return "ok"

    registry.submit(slow, "t1", "builder")
    with pytest.raises(RuntimeError):
        registry.submit(slow, "t2", "builder")
    gate.set()


def test_prune_keeps_bounded_history(monkeypatch):
    import lib.core.delegate_pool as pool_mod

    registry = AsyncDelegateRegistry(max_workers=1)
    monkeypatch.setattr(pool_mod, "KEEP_FINISHED", 2)
    first = None
    for i in range(5):
        handle = registry.submit(lambda task, kind: "ok", f"t{i}", "builder")
        if first is None:
            first = handle
        assert registry.poll(handle, wait_seconds=5).status == "done"
    remaining = [j.handle for j in registry.list_jobs()]
    assert len(remaining) == 3
    assert first not in remaining


def test_poll_unknown_and_resume_validations():
    registry = AsyncDelegateRegistry(max_workers=1)
    with pytest.raises(KeyError):
        registry.poll("d_nope")
    with pytest.raises(KeyError):
        registry.resume("d_nope", "x")
    with pytest.raises(ValueError):
        registry.resume("d_nope2", "")
    handle = registry.submit(lambda task, kind: ("text", "tok"), "t", "builder")
    assert registry.poll(handle, wait_seconds=5).status == "done"
    with pytest.raises(RuntimeError):
        registry.resume(handle, "追问无支持")


def test_resume_unfinished_rejected():
    registry = AsyncDelegateRegistry(max_workers=1)
    gate = threading.Event()

    def slow(task, kind):
        gate.wait(timeout=10)
        return "ok"

    handle = registry.submit(slow, "t", "builder")
    assert registry.poll(handle, wait_seconds=2).status == "running"
    with pytest.raises(RuntimeError):
        registry.resume(handle, "等等")
    gate.set()
    assert registry.poll(handle, wait_seconds=5).status == "done"


def test_direct_run_pre_cancelled_is_clean():
    registry = AsyncDelegateRegistry(max_workers=1)
    handle = registry.submit(lambda task, kind: "ok", "t", "builder")
    job = registry.poll(handle, wait_seconds=5)
    assert job.status == "done"
    assert job.to_dict()["handle"] == handle
    assert job.to_dict()["turns"] == 1
    assert set(job.to_dict()) == {"handle", "task", "agent_type", "status", "result", "error", "turns"}
    registry.shutdown()


def test_direct_run_respects_pre_cancel():
    import contextvars

    registry = AsyncDelegateRegistry(max_workers=1)
    handle = registry.submit(lambda task, kind: "ok", "t", "builder")
    assert registry.poll(handle, wait_seconds=5).status == "done"
    called = []

    def never(task, kind):
        called.append(True)
        return "x"

    handle2 = registry.submit(never, "t2", "builder")
    registry.cancel(handle2)
    out = registry._run(contextvars.copy_context(), never, handle2)
    assert out == ""
    assert called == []
    assert registry.poll(handle2).status == "cancelled"


def test_manager_spawn_timeout_and_raw_result():
    from lib.tools.delegate_tools import build_manager_spawn_fn

    class SlowManager:
        def spawn(self, agent_type, task, workspace="."):
            return "w1"

        def wait(self, worker_id, timeout=60.0):
            return None

        def get_result(self, worker_id, mark_read=True):
            return None

    import pytest as _pytest

    with _pytest.raises(RuntimeError):
        build_manager_spawn_fn(SlowManager(), "/ws")( "t", "builder")

    class RawManager(SlowManager):
        def wait(self, worker_id, timeout=60.0):
            return {"ok": True}

        def get_result(self, worker_id, mark_read=True):
            return 42

    assert build_manager_spawn_fn(RawManager(), "/ws")("t", "builder") == "42"


def test_singleton_and_shutdown():
    assert get_delegate_registry() is get_delegate_registry()
    get_delegate_registry().shutdown()

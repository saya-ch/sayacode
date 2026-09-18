"""完成推送测试：轮后 drain 打印一次，二次静默，外加计划表渲染。"""

from lib.core.plans import Plan, PlanTask
from lib.theme import _build_plan_table

from lib.core.delegate_pool import get_delegate_registry
from lib.runtime.interactive import InteractiveLoop


def test_drain_prints_finished_once(monkeypatch):
    registry = get_delegate_registry()
    handle = registry.submit(lambda task, kind: "推送内容", "后台活", "builder")
    registry.poll(handle, wait_seconds=5)
    printed = []
    import lib.theme as theme_mod

    real_line = theme_mod._line
    monkeypatch.setattr(theme_mod, "_line", lambda *a, **k: printed.append(a) or real_line(*a, **k))
    loop = object.__new__(InteractiveLoop)
    loop.printed_notifications = set()
    loop._drain_delegate_notifications()
    flat = str(printed)
    assert handle in flat and "推送内容" in flat
    printed.clear()
    loop._drain_delegate_notifications()
    assert printed == []


def test_plan_table_renders_tasks_and_status():
    from rich.console import Console

    plan = Plan(goal="上线", tasks=[
        PlanTask(id="t1", title="写码", status="done", result="好"),
        PlanTask(id="t2", title="测试", status="doing"),
        PlanTask(id="t3", title="回滚预案", status="failed", result="挂"),
    ], rounds=2)
    console = Console(record=True, width=100)
    console.print(_build_plan_table(plan))
    text = console.export_text()
    assert "上线" in text
    assert "写码" in text and "测试" in text
    assert "2" in text


def test_drain_never_breaks_loop(monkeypatch):
    import lib.runtime.interactive as mod

    monkeypatch.setattr(mod, "get_delegate_registry", lambda: 1 / 0)
    loop = object.__new__(InteractiveLoop)
    loop._drain_delegate_notifications()

"""回归：检索工具的参数 schema 必须可序列化。

真实事故（2026-11-07）：为「检索输出加限流」给两个检索工具加了
``runtime: ToolRuntime | None = None`` 参数，本意是从运行时上下文读取
``output_limit_bytes``；但该参数被当成**工具入参**，pydantic 无法为
``ToolRuntime`` 生成 JSON schema：

    Failed to generate JSON schema for 'glob_search':
    Cannot generate a JsonSchema for core_schema.CallableSchema

后果：**整个产品不可用**——worker 在装配工具时就 ``run.failed``，
12 个 trial 全部 2 秒内失败（`SAYACODE worker exit=1`）。

修法：用 ``Annotated[..., InjectedToolArg]`` 声明，框架注入、不进 schema。
本文件锁住「注入参数不得出现在 args 里」这一点。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from sayacode.tools.search import WorkspaceFileSearchMiddleware

#: 这些参数由框架注入，绝不能出现在工具对模型暴露的参数表里
INTERNAL_ARGS = {"runtime", "state", "store", "config", "tool_call_id"}


@pytest.fixture()
def middleware(tmp_path: Path) -> WorkspaceFileSearchMiddleware:
    (tmp_path / "a.py").write_text("VALUE = 1\n", encoding="utf-8")
    return WorkspaceFileSearchMiddleware(root_path=str(tmp_path))


@pytest.mark.parametrize("tool_name", ["glob_search", "grep_search"])
def test_schema_generation_does_not_raise(
    middleware: WorkspaceFileSearchMiddleware, tool_name: str
) -> None:
    """能取到 schema 就是不抛异常的证明——事故里这一步直接炸。"""
    tool: Any = getattr(middleware, tool_name)
    schema = tool.args_schema
    assert schema is not None, f"{tool_name} 必须有可用的 args_schema"


@pytest.mark.parametrize("tool_name", ["glob_search", "grep_search"])
def test_internal_args_are_not_exposed(
    middleware: WorkspaceFileSearchMiddleware, tool_name: str
) -> None:
    """框架注入参数不得出现在模型可见的参数表里。"""
    tool: Any = getattr(middleware, tool_name)
    exposed = set((tool.args or {}).keys())
    leaked = exposed & INTERNAL_ARGS
    assert not leaked, f"{tool_name} 把注入参数暴露给了模型: {sorted(leaked)}"


@pytest.mark.parametrize("tool_name", ["glob_search", "grep_search"])
def test_model_visible_args_are_primitive_only(
    middleware: WorkspaceFileSearchMiddleware, tool_name: str
) -> None:
    """模型可见的参数必须是 JSON 可表达的类型（str/int/bool/array/object）。"""
    tool: Any = getattr(middleware, tool_name)
    schema = tool.tool_call_schema.model_json_schema() if tool.tool_call_schema else {}
    props = (schema.get("properties") or {})
    for name, spec in props.items():
        kind = spec.get("type")
        # anyOf 形式（Optional）也允许，但子类型同样要是原始类型
        if kind is None and "anyOf" in spec:
            kinds = {s.get("type") for s in spec["anyOf"]}
            assert kinds <= {"string", "integer", "number", "boolean", "array", "object", "null"}, (
                f"{tool_name}.{name} 的类型不可序列化: {kinds}"
            )
        elif kind is not None:
            assert kind in {"string", "integer", "number", "boolean", "array", "object", "null"}, (
                f"{tool_name}.{name} 的类型不可序列化: {kind}"
            )


def test_tools_still_work_after_injection(
    middleware: WorkspaceFileSearchMiddleware,
) -> None:
    """注入改动不能让工具本身失效——两个工具都要能正常返回结果。"""
    glob_result = middleware.glob_search.invoke({"pattern": "**/*.py"})
    assert "a.py" in glob_result

    grep_result = middleware.grep_search.invoke({"pattern": "VALUE"})
    assert "a.py" in grep_result

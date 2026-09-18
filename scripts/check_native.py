"""核对原生依赖与关键符号是否可用，快速定位环境缺件。
用法：
    python scripts/check_native.py
退出码：恒为 0，只打印探针结果，不做门禁判定。
"""
import importlib
import inspect


def probe(mod, attr=None):
    """导入模块并检查符号存在性，输出单行结果。"""
    try:
        m = importlib.import_module(mod)
        v = getattr(m, "__version__", "?")
        if attr:
            ok = hasattr(m, attr)
            print(mod, v, "has " + attr if ok else "MISSING " + attr)
        else:
            print(mod, v, "ok")
    except Exception as exc:
        print(mod, "ABSENT", str(exc)[:120])


def main():
    """探测各依赖模块与关键符号。"""
    # 探测模块是否可导入。
    for mod in (
        "langchain",
        "langgraph",
        "langchain_core",
        "langgraph_supervisor",
        "langgraph_checkpoint_sqlite",
        "langgraph.graph",
        "langgraph.types",
        "langgraph.store.memory",
        "langgraph.checkpoint.sqlite",
        "langchain.agents",
    ):
        probe(mod)
    # 探测关键符号是否存在（收敛依赖的官方能力缺一即告警）。
    for mod, attr in (
        ("langgraph.graph", "StateGraph"),
        ("langgraph.types", "Send"),
        ("langgraph.types", "Command"),
        ("langgraph.types", "interrupt"),
        ("langgraph.store.memory", "InMemoryStore"),
        ("langgraph.checkpoint.sqlite", "SqliteSaver"),
        ("langchain.agents", "create_agent"),
        ("langchain.agents.middleware", "ModelRetryMiddleware"),
        ("langchain.agents.middleware", "ToolRetryMiddleware"),
        ("langchain.agents.middleware", "ModelCallLimitMiddleware"),
        ("langchain.agents.middleware", "ToolCallLimitMiddleware"),
        ("langchain.agents.middleware", "ContextEditingMiddleware"),
        ("langchain.agents.middleware", "SummarizationMiddleware"),
        ("langchain.agents.middleware", "HumanInTheLoopMiddleware"),
        ("langchain.agents.middleware", "dynamic_prompt"),
        ("langchain_core.callbacks", "BaseCallbackHandler"),
    ):
        probe(mod, attr)
    from langchain.agents import create_agent

    # 打印签名，供核对上游参数漂移。
    print("create_agent params:", list(inspect.signature(create_agent).parameters)[:12])


if __name__ == "__main__":
    main()

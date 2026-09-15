"""模型基类 —— 自有传输实现的抽象契约。

本模块现在的职责收窄为两件事：

1. 重新导出共享词汇表（``TokenUsage`` / ``ModelInfo`` / ``parse_context_window``），
   保持 ``from lib.models.base import ...`` 这一既有导入路径有效；
2. 提供 :class:`BaseModel`：给**自有传输实现**（不走 LangChain 集成的模型，
   以及测试替身）使用的抽象基类。

绝大多数模型不再继承本类。它们由 :mod:`lib.models.providers` 里的薄类提供，
直接多重继承 LangChain 的具体 chat model 与 :class:`~lib.models.extras.ModelExtras`。

共享行为（上下文窗口、用量、消息转换、连通性检查）全部在 ``ModelExtras`` 里实现，
``BaseModel`` 只是把它与 LangChain 无关的抽象契约组合起来 —— 因此不存在两份实现。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Iterator, List

from .extras import ModelExtras
from .vocabulary import ModelInfo, TokenUsage, parse_context_window


class BaseModel(ModelExtras, ABC):
    """自有传输实现的抽象基类。

    子类必须实现 ``_initialize_model`` / ``chat`` / ``chat_stream`` /
    ``get_model_info``。所有其他行为继承自 :class:`ModelExtras`。
    """

    def __init__(self, model_name: str, temperature: float = 0.2, **kwargs: Any) -> None:
        """
        初始化模型

        Args:
            model_name: 模型名称
            temperature: 温度参数（0-1）
            **kwargs: 其他模型参数；``context_window`` 会被本层消费
        """
        explicit_context_window = parse_context_window(kwargs.pop("context_window", None))

        self.model_name = model_name
        self.temperature = temperature
        self._model: Any = None
        self.__dict__["_extra_params"] = dict(kwargs)
        # 上下文窗口必须来自用户显式输入或 API；未知时保持 0。
        self.__dict__["_context_window"] = explicit_context_window or self.DEFAULT_CONTEXT_WINDOW
        self.__dict__["_context_window_source"] = "manual" if explicit_context_window else ""

    @abstractmethod
    def _initialize_model(self) -> None:
        """创建底层模型客户端。"""

    @abstractmethod
    def chat(self, messages: List[dict], **kwargs: Any) -> str:
        """发送对话请求（非流式），返回回复文本。"""

    @abstractmethod
    def chat_stream(self, messages: List[dict], **kwargs: Any) -> Iterator[str]:
        """发送对话请求（流式），逐块产出回复文本。"""

    @abstractmethod
    def get_model_info(self) -> ModelInfo:
        """返回模型元信息。"""

    def validate_temperature(self, temperature: float) -> float:
        """:meth:`ModelExtras.clamp_temperature` 的兼容别名。

        只在本类提供。LangChain 支撑的模型**无法**暴露这个名字：``temperature``
        是它们的字段，而 pydantic 会把 ``validate_<字段名>`` 形式的成员当成该字段的
        校验器，导致构造失败。需要该能力时请调用 ``clamp_temperature``。
        """
        return self.clamp_temperature(temperature)

    def bind_tools(self, tools: List[Any]) -> Any:
        """把工具绑定到底层 LangChain 模型。

        Agent 创建阶段会优先调用该方法获取可执行工具调用的模型。
        """
        self._initialize_model()

        if self._model is None or not hasattr(self._model, "bind_tools"):
            raise NotImplementedError(f"{self.__class__.__name__} 不支持工具绑定")

        return self._model.bind_tools(tools)


__all__ = [
    "BaseModel",
    "ModelExtras",
    "ModelInfo",
    "TokenUsage",
    "parse_context_window",
]

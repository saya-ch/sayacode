"""
模型基类

定义所有模型实现的抽象基类，确保统一的接口和行为。
"""

import re
import unicodedata
from abc import ABC, abstractmethod
from typing import List, Dict, Any, Iterator, Optional
from dataclasses import dataclass
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage


_CONTEXT_WINDOW_SUFFIXES = {
    "": 1,
    "k": 1024,
    "m": 1024 * 1024,
    "g": 1024 * 1024 * 1024,
}
_MAX_CONTEXT_WINDOW = 100_000_000


def parse_context_window(value: Any) -> Optional[int]:
    """
    Parse a model context window value.

    Accepted examples:
    - 128000
    - "128000"
    - "128,000"
    - "256k" / "256K"
    - "1M" / "1.5m"

    Suffixes use 1024-based context notation because most model windows are
    advertised as 32K/128K/1M style powers of two. Users who need exact
    decimal values can enter a plain number.
    """
    if value is None or isinstance(value, bool):
        return None

    if isinstance(value, int):
        return value if value > 0 else None

    if isinstance(value, float):
        return int(value) if value > 0 and value.is_integer() else None

    text = unicodedata.normalize("NFKC", str(value)).strip()
    if not text:
        return None

    text = text.replace(",", "").replace("_", "").strip()
    match = re.fullmatch(
        r"([0-9]+(?:\.[0-9]+)?)\s*([kKmMgG]?)\s*(?:tokens?|token|t)?",
        text,
    )
    if not match:
        return None

    number_text, suffix = match.groups()
    multiplier = _CONTEXT_WINDOW_SUFFIXES.get(suffix.lower())
    if multiplier is None:
        return None

    try:
        number = float(number_text)
    except ValueError:
        return None

    if number <= 0:
        return None

    if suffix == "" and not number.is_integer():
        return None

    parsed = int(number * multiplier)
    if parsed > _MAX_CONTEXT_WINDOW:
        return None
    return parsed if parsed > 0 else None


@dataclass
class TokenUsage:
    """单次调用的 Token 用量统计"""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
        )


@dataclass
class ModelInfo:
    """模型信息数据结构"""
    # 模型名称
    name: str

    # 模型类型（ollama/openai 等）
    model_type: str

    # 模型供应商
    provider: str

    # 模型支持的参数
    supported_params: List[str]

    # 是否支持流式输出
    supports_streaming: bool = True

    # 其他元数据
    metadata: Dict[str, Any] = None

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}




class BaseModel(ABC):
    """
    模型基类

    定义所有语言模型的统一接口，包括聊天、补全和工具调用功能。
    新增 Token 用量追踪和成本估算支持。
    """

    # 上下文窗口必须来自 API 明确返回或用户显式输入；未知时保持 0。
    _DEFAULT_CONTEXT_WINDOW = 0

    def __init__(
        self,
        model_name: str,
        temperature: float = 0.2,
        **kwargs
    ):
        """
        初始化模型

        Args:
            model_name: 模型名称
            temperature: 温度参数（0-1）
            **kwargs: 其他模型参数
        """
        explicit_context_window = parse_context_window(kwargs.pop("context_window", None))

        self.model_name = model_name
        self.temperature = temperature
        self.extra_params = kwargs
        self._model = None  # 延迟初始化
        self._last_usage: Optional[TokenUsage] = None
        self._session_usage = TokenUsage()
        self._context_window: int = explicit_context_window or self._DEFAULT_CONTEXT_WINDOW
        self._context_window_source: str = "manual" if explicit_context_window else ""

    @property
    def context_window(self) -> int:
        """模型的最大上下文窗口大小（token）。"""
        return self._context_window

    @context_window.setter
    def context_window(self, value: int):
        parsed = parse_context_window(value)
        if parsed:
            self._context_window = parsed
            self._context_window_source = "manual"

    @property
    def context_window_source(self) -> str:
        """上下文窗口来源：api / manual / 空字符串（未知）。"""
        return self._context_window_source

    def detect_context_window(self) -> Optional[int]:
        """
        探测模型实际的上下文窗口大小（token）。

        探测优先级：
        1. 用户显式配置的 context_window
        2. API 探测（子类可覆盖实现特定 API 的查询）

        无法准确获取时返回 None，调用方应要求用户输入，不能使用默认值。
        """
        if self._context_window > 0 and self._context_window_source == "manual":
            return self._context_window

        detected = self._probe_api_for_context_window()
        if detected and detected > 0:
            self._context_window = detected
            self._context_window_source = "api"
            return detected

        return None

    def _probe_api_for_context_window(self) -> Optional[int]:
        """
        通过 API 查询模型上下文窗口大小。

        基类返回 None；各子类应覆盖此方法以使用各自的 API 端点查询。
        """
        return None

    @property
    def last_usage(self) -> Optional[TokenUsage]:
        """获取最近一次调用的 Token 用量"""
        return self._last_usage

    @property
    def session_usage(self) -> TokenUsage:
        """获取当前会话累计的 Token 用量"""
        return self._session_usage

    def reset_session_usage(self) -> None:
        """重置会话级 Token 统计"""
        self._session_usage = TokenUsage()

    def _record_usage(self, usage: TokenUsage) -> None:
        """记录一次调用的 Token 用量"""
        self._last_usage = usage
        self._session_usage = self._session_usage + usage

    @staticmethod
    def _extract_usage_from_response(response: Any) -> TokenUsage:
        """
        从 LangChain 响应对象中提取 token 用量。
        子类可覆盖以适配不同供应商的响应格式。
        """
        usage = TokenUsage()
        if response is None:
            return usage

        # LangChain 标准 usage_metadata
        if hasattr(response, "usage_metadata") and response.usage_metadata:
            meta = response.usage_metadata
            usage.prompt_tokens = int(meta.get("input_tokens", 0) or meta.get("prompt_tokens", 0))
            usage.completion_tokens = int(meta.get("output_tokens", 0) or meta.get("completion_tokens", 0))
            usage.total_tokens = int(meta.get("total_tokens", 0) or (usage.prompt_tokens + usage.completion_tokens))
            return usage

        # OpenAI 风格 response_metadata
        if hasattr(response, "response_metadata") and response.response_metadata:
            meta = response.response_metadata
            token_usage = meta.get("token_usage") or meta.get("usage")
            if token_usage:
                usage.prompt_tokens = int(token_usage.get("prompt_tokens", 0))
                usage.completion_tokens = int(token_usage.get("completion_tokens", 0))
                usage.total_tokens = int(token_usage.get("total_tokens", 0) or (usage.prompt_tokens + usage.completion_tokens))
                return usage

        # 直接属性
        for attr in ("usage", "token_usage"):
            if hasattr(response, attr):
                val = getattr(response, attr)
                if val:
                    usage.prompt_tokens = int(getattr(val, "prompt_tokens", val.get("prompt_tokens", 0)) if isinstance(val, dict) else getattr(val, "prompt_tokens", 0))
                    usage.completion_tokens = int(getattr(val, "completion_tokens", val.get("completion_tokens", 0)) if isinstance(val, dict) else getattr(val, "completion_tokens", 0))
                    usage.total_tokens = int(getattr(val, "total_tokens", val.get("total_tokens", 0)) if isinstance(val, dict) else getattr(val, "total_tokens", 0))
                    return usage

        return usage

    @abstractmethod
    def _initialize_model(self):
        """
        初始化底层模型实例

        子类需要实现此方法来创建具体的模型客户端。
        """
        pass

    @abstractmethod
    def chat(
        self,
        messages: List[Dict[str, str]],
        **kwargs
    ) -> str:
        """
        发送对话请求（非流式）

        Args:
            messages: 消息列表，格式为 [{"role": "user", "content": "..."}]
            **kwargs: 其他参数

        Returns:
            模型回复文本
        """
        pass

    @abstractmethod
    def chat_stream(
        self,
        messages: List[Dict[str, str]],
        **kwargs
    ) -> Iterator[str]:
        """
        发送对话请求（流式）

        Args:
            messages: 消息列表，格式为 [{"role": "user", "content": "..."}]
            **kwargs: 其他参数

        Yields:
            逐步返回模型回复的文本块
        """
        pass

    @abstractmethod
    def get_model_info(self) -> ModelInfo:
        """
        获取模型信息

        Returns:
            ModelInfo 对象，包含模型的元数据
        """
        pass

    def convert_messages(
        self,
        messages: List[Dict[str, str]]
    ) -> List[BaseMessage]:
        """
        将字典格式的消息转换为 LangChain 消息对象

        Args:
            messages: 字典格式的消息列表

        Returns:
            LangChain 消息对象列表
        """
        converted = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")

            if role == "user":
                converted.append(HumanMessage(content=content))
            elif role == "assistant":
                converted.append(AIMessage(content=content))
            elif role == "system":
                # LangChain 使用 SystemMessage
                from langchain_core.messages import SystemMessage
                converted.append(SystemMessage(content=content))
            else:
                # 默认作为用户消息
                converted.append(HumanMessage(content=content))

        return converted

    def prepare_messages(
        self,
        user_input: str,
        system_prompt: Optional[str] = None,
        history: Optional[List[Dict[str, str]]] = None
    ) -> List[BaseMessage]:
        """
        准备发送给模型的消息列表

        Args:
            user_input: 用户输入
            system_prompt: 系统提示词（可选）
            history: 对话历史（可选）

        Returns:
            准备好的消息列表
        """
        messages = []

        # 添加系统提示词
        if system_prompt:
            from langchain_core.messages import SystemMessage
            messages.append(SystemMessage(content=system_prompt))

        # 添加历史消息
        if history:
            messages.extend(self.convert_messages(history))

        # 添加当前用户输入
        messages.append(HumanMessage(content=user_input))

        return messages

    def validate_temperature(self, temperature: float) -> float:
        """
        验证并规范化温度参数

        Args:
            temperature: 温度值

        Returns:
            规范化后的温度值（0-1之间）
        """
        return max(0.0, min(1.0, temperature))

    def get_config(self) -> Dict[str, Any]:
        """
        获取模型配置

        Returns:
            包含当前配置的字典
        """
        return {
            "model_name": self.model_name,
            "temperature": self.temperature,
            "model_type": self.__class__.__name__,
            **self.extra_params
        }

    def bind_tools(self, tools: List[Any]):
        """
        将工具绑定到底层 LangChain 模型。

        大多数本地包装类都把真实模型实例保存在 ``self._model`` 中。
        Agent 创建阶段会优先调用该方法获取可执行工具调用的模型。
        """
        self._initialize_model()

        if self._model is None or not hasattr(self._model, "bind_tools"):
            raise NotImplementedError(
                f"{self.__class__.__name__} 不支持工具绑定"
            )

        return self._model.bind_tools(tools)

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(model={self.model_name}, temperature={self.temperature})"


class StreamingMixin:
    """流式输出混入类，提供通用的流式处理功能"""

    def stream_wrapper(
        self,
        stream_iterator: Iterator
    ) -> Iterator[str]:
        """
        包装流式迭代器，添加错误处理

        Args:
            stream_iterator: 底层流迭代器

        Yields:
            文本块
        """
        try:
            for chunk in stream_iterator:
                if chunk:
                    yield str(chunk)
        except Exception as e:
            yield f"\n[流式输出错误: {str(e)}]"

    def format_stream_output(
        self,
        content: str,
        buffer: str = ""
    ) -> str:
        """
        格式化流式输出内容

        Args:
            content: 当前内容块
            buffer: 已有缓冲区

        Returns:
            格式化后的内容
        """
        return buffer + content

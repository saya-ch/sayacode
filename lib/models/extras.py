"""所有模型共享的行为 —— 上下文窗口、用量统计、消息转换、连通性检查。

**为什么不用 pydantic 私有属性**

本类要与 LangChain 的 chat model 类（pydantic ``BaseModel`` 子类）多重继承：

    class OpenAIModel(NonstandardPassthroughMixin, ModelExtras, ChatOpenAI): ...

pydantic 只在**它处理的基类**上收集 ``PrivateAttr`` / 字段，普通 mixin 不在其列。
实测两种写法都不可用：

* 把 ``PrivateAttr`` 声明在普通 mixin 上 → 初始读取返回描述符对象本身（``default=0``）；
* 把 ``PrivateAttr`` 声明在具体类上 → setter 赋值后读回仍是默认值。

因此状态一律通过 ``self.__dict__`` 存取：写入用 ``__dict__[...] = ...``（绕过 pydantic
的 ``__setattr__``），读取用 ``__dict__.get(...)`` 并给默认值。实测该方式在
初始值、赋值、多实例隔离、``model_dump()`` 兼容性上全部正确。
"""

from __future__ import annotations

from typing import Any, ClassVar, Dict, Iterator, List, Optional

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from ..i18n import tr
from .vocabulary import ModelInfo, TokenUsage, parse_context_window


class ModelExtras:
    """所有模型共享的实现。

    子类需提供 ``model_name``（LangChain 的 chat model 自带该字段）与
    ``invoke`` / ``stream``（LangChain 提供）。自有传输实现请继承
    :class:`lib.models.base.BaseModel`，它会把 ``chat`` / ``chat_stream`` /
    ``get_model_info`` 重新声明为抽象方法。
    """

    # 上下文窗口必须来自 API 明确返回或用户显式输入；未知时保持 0。
    #
    # 下面这些类级声明必须是 ClassVar **且不带下划线前缀**：本类会与 pydantic 模型
    # 多重继承，而 pydantic 要求非下划线类属性注解为 ClassVar，否则报错；下划线属性
    # 则会被当成私有属性接管，导致子类覆盖失效（实测子类值读出来仍是基类默认）。
    DEFAULT_CONTEXT_WINDOW: ClassVar[int] = 0

    # 供 get_model_info() 使用；具体协议类覆盖。
    MODEL_TYPE_NAME: ClassVar[str] = ""
    PROVIDER_NAME: ClassVar[str] = ""
    SUPPORTS_STREAMING: ClassVar[bool] = True

    # ── 上下文窗口 ──────────────────────────────────────────────────────────

    @property
    def context_window(self) -> int:
        """模型的最大上下文窗口大小（token）；未知时为 0。"""
        return int(self.__dict__.get("_context_window", self.DEFAULT_CONTEXT_WINDOW))

    @context_window.setter
    def context_window(self, value: Any) -> None:
        """设置上下文窗口。

        三种结果，语义互不重叠：

        * 合法正数（含 ``"128k"`` 这类写法）→ 记录并标记来源为 ``manual``；
        * ``None`` → **显式清空**（回到 0 / 未知）。允许清空是必要的：否则一旦设过值
          就再也无法回到「未知」，而「未知」正是要求用户输入的正确前置状态；
        * 其它非法值（``"abc"``、``0``、``-1``）→ 忽略，不污染已有取值。
        """
        if value is None:
            self.__dict__.pop("_context_window", None)
            self.__dict__["_context_window_source"] = ""
            return

        parsed = parse_context_window(value)
        if parsed:
            self.__dict__["_context_window"] = parsed
            self.__dict__["_context_window_source"] = "manual"

    @property
    def context_window_source(self) -> str:
        """上下文窗口来源：``api`` / ``manual`` / 空字符串（未知）。"""
        return str(self.__dict__.get("_context_window_source", ""))

    @context_window_source.setter
    def context_window_source(self, value: str) -> None:
        self.__dict__["_context_window_source"] = str(value or "")

    def detect_context_window(self) -> Optional[int]:
        """探测模型真实的上下文窗口。

        优先使用用户显式配置；否则走协议自己的探测。**无法确定时返回 None** ——
        调用方应要求用户输入，不能编造一个默认值。
        """
        if self.context_window > 0 and self.context_window_source == "manual":
            return self.context_window

        detected = self._probe_api_for_context_window()
        if detected and detected > 0:
            self.__dict__["_context_window"] = detected
            self.__dict__["_context_window_source"] = "api"
            return detected
        return None

    def _probe_api_for_context_window(self) -> Optional[int]:
        """通过 API 查询上下文窗口。基类返回 None；各协议子类覆盖。"""
        return None

    # ── 用量统计 ────────────────────────────────────────────────────────────

    @property
    def last_usage(self) -> Optional[TokenUsage]:
        """最近一次调用的 Token 用量。"""
        value = self.__dict__.get("_last_usage")
        return value if isinstance(value, TokenUsage) else None

    @property
    def session_usage(self) -> TokenUsage:
        """当前会话累计的 Token 用量。"""
        return self.__dict__.setdefault("_session_usage", TokenUsage())

    def reset_session_usage(self) -> None:
        """重置会话级 Token 统计。"""
        self.__dict__["_session_usage"] = TokenUsage()

    def _record_usage(self, usage: TokenUsage) -> None:
        self.__dict__["_last_usage"] = usage
        self.__dict__["_session_usage"] = self.session_usage + usage

    @staticmethod
    def _extract_usage_from_response(response: Any) -> TokenUsage:
        """从 LangChain 响应对象中提取 token 用量。

        覆盖三种来源：标准 ``usage_metadata``、OpenAI 风格 ``response_metadata``、
        以及直接挂在对象上的 ``usage`` / ``token_usage`` 属性。
        """
        usage = TokenUsage()
        if response is None:
            return usage

        metadata = getattr(response, "usage_metadata", None)
        if metadata:
            usage.prompt_tokens = int(metadata.get("input_tokens", 0) or metadata.get("prompt_tokens", 0))
            usage.completion_tokens = int(metadata.get("output_tokens", 0) or metadata.get("completion_tokens", 0))
            usage.total_tokens = int(
                metadata.get("total_tokens", 0) or (usage.prompt_tokens + usage.completion_tokens)
            )
            return usage

        response_metadata = getattr(response, "response_metadata", None)
        if response_metadata:
            token_usage = response_metadata.get("token_usage") or response_metadata.get("usage")
            if token_usage:
                usage.prompt_tokens = int(token_usage.get("prompt_tokens", 0))
                usage.completion_tokens = int(token_usage.get("completion_tokens", 0))
                usage.total_tokens = int(
                    token_usage.get("total_tokens", 0) or (usage.prompt_tokens + usage.completion_tokens)
                )
                return usage

        for attr in ("usage", "token_usage"):
            value = getattr(response, attr, None)
            if not value:
                continue
            usage.prompt_tokens = _read_int(value, "prompt_tokens")
            usage.completion_tokens = _read_int(value, "completion_tokens")
            usage.total_tokens = _read_int(value, "total_tokens") or (
                usage.prompt_tokens + usage.completion_tokens
            )
            return usage

        return usage

    @staticmethod
    def _estimate_usage_from_text(
        messages: List[Dict[str, str]],
        response_text: str,
    ) -> TokenUsage:
        """流式响应通常不带用量，按字符数粗略估算（1 token ≈ 3 字符）。"""
        prompt_chars = sum(len(message.get("content", "")) for message in messages)
        prompt_tokens = prompt_chars // 3
        completion_tokens = len(response_text) // 3
        return TokenUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        )

    # ── 采样参数 ────────────────────────────────────────────────────────────

    def clamp_temperature(self, temperature: float) -> float:
        """把温度钳制到 [0, 1]。

        为什么叫 ``clamp_temperature`` 而不是 ``validate_temperature``：
        实测 ``langchain_openai`` 自己在 ``BaseChatOpenAI`` 上就定义了
        ``validate_temperature``，并由 pydantic 的 ``validate_<字段名>`` 约定注册为
        ``temperature`` 字段的校验器。本 mixin 在 MRO 中位于厂商类之前，若用同名方法
        就会**覆盖**那个校验器，导致签名不匹配（校验器收到的是 ``ValidationInfo``
        而不是 float），模型构造直接失败。改名即可避免覆盖。
        自有传输基类 :class:`~lib.models.base.BaseModel` 另保留了旧名别名。
        """
        return max(0.0, min(1.0, temperature))

    # ── 消息转换 ────────────────────────────────────────────────────────────

    def convert_messages(self, messages: List[Dict[str, str]]) -> List[BaseMessage]:
        """把 ``[{"role", "content"}]`` 字典转换成 LangChain 消息对象。"""
        converted: List[BaseMessage] = []
        for message in messages:
            role = message.get("role", "user")
            content = message.get("content", "")

            if role == "user":
                converted.append(HumanMessage(content=content))
            elif role == "assistant":
                converted.append(AIMessage(content=content))
            elif role == "system":
                converted.append(SystemMessage(content=content))
            else:
                # 未知 role 按用户消息处理（与历史行为一致）
                converted.append(HumanMessage(content=content))
        return converted

    def prepare_messages(
        self,
        user_input: str,
        system_prompt: Optional[str] = None,
        history: Optional[List[Dict[str, str]]] = None,
    ) -> List[BaseMessage]:
        """组装 system → history → 当前输入 的消息序列。"""
        messages: List[BaseMessage] = []
        if system_prompt:
            messages.append(SystemMessage(content=system_prompt))
        if history:
            messages.extend(self.convert_messages(history))
        messages.append(HumanMessage(content=user_input))
        return messages

    # ── 调用 ────────────────────────────────────────────────────────────────

    def _initialize_model(self) -> None:
        """兼容钩子。

        LangChain 模型在构造时即完成初始化，无需额外动作；保留本方法是为了让
        ``check_connection`` 与历史调用点（含测试的 monkeypatch）继续成立。
        """
        return None

    def chat(self, messages: List[Dict[str, str]], **kwargs: Any) -> str:
        """发送对话请求（非流式），返回回复文本。"""
        response = self.invoke(self.convert_messages(messages), **kwargs)  # type: ignore[attr-defined]
        self._record_usage(self._extract_usage_from_response(response))
        return response.content if hasattr(response, "content") else str(response)

    def chat_stream(self, messages: List[Dict[str, str]], **kwargs: Any) -> Iterator[str]:
        """发送对话请求（流式），逐块产出回复文本。"""
        full_response = ""
        usage_chunk: Any = None

        for chunk in self.stream(self.convert_messages(messages), **kwargs):  # type: ignore[attr-defined]
            # 判据必须是「有没有 content 属性」，而不是「content 是否非空」：
            # 结束块与 usage 块的 content 是空字符串，若这样区分就会落进下面的分支、
            # 把整个 chunk 对象的 repr 当文本产出（实测真实 SSE 流会命中）。
            if hasattr(chunk, "content"):
                content = chunk.content
                if content:
                    full_response += content
                    yield content
            elif chunk:
                full_response += str(chunk)
                yield str(chunk)

            # 用量块**不是**最后一个块（其后通常还有一个只带 chunk_position='last'
            # 的空收尾块），只看最后一块会拿不到 usage、静默退化成字符数估算
            # （实测：真实用量 7/3/10 被估成 3/2/5）。因此记住最后一个带用量的块。
            if self._extract_usage_from_response(chunk).total_tokens > 0:
                usage_chunk = chunk

        if usage_chunk is not None:
            self._record_usage(self._extract_usage_from_response(usage_chunk))
            return
        # 上游确实没给用量时才退化到估算
        self._record_usage(self._estimate_usage_from_text(messages, full_response))

    def check_connection(self) -> bool:
        """发一次最小请求验证连通性；成功时顺带探测上下文窗口。"""
        try:
            self._initialize_model()
            self.chat([{"role": "user", "content": "ping"}])
        except Exception as exc:
            print(tr("connection.failed", error=str(exc)))
            return False

        print(tr("connection.connected"))
        detected = self.detect_context_window()
        if detected:
            print(tr("connection.context_detected", context_window=f"{detected:,}"))
        return True

    # ── 元信息 ──────────────────────────────────────────────────────────────

    def get_model_info(self) -> ModelInfo:
        """返回模型元信息。"""
        return ModelInfo(
            name=str(getattr(self, "model_name", "")),
            model_type=self.MODEL_TYPE_NAME,
            provider=self.PROVIDER_NAME,
            supported_params=[],
            supports_streaming=self.SUPPORTS_STREAMING,
        )

    @property
    def extra_params(self) -> Dict[str, Any]:
        """构造时未被模型类消费的额外参数（供 get_config 展示）。"""
        value = self.__dict__.get("_extra_params")
        return dict(value) if isinstance(value, dict) else {}

    def get_config(self) -> Dict[str, Any]:
        """返回当前配置的扁平视图。"""
        return {
            "model_name": str(getattr(self, "model_name", "")),
            "temperature": getattr(self, "temperature", None),
            "model_type": type(self).__name__,
            **self.extra_params,
        }

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(model={getattr(self, 'model_name', '?')}, "
            f"temperature={getattr(self, 'temperature', None)})"
        )


def _read_int(source: Any, key: str) -> int:
    """从 dict 或对象上读一个整数字段，缺失或非法时返回 0。"""
    if isinstance(source, dict):
        raw = source.get(key, 0)
    else:
        raw = getattr(source, key, 0)
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0


__all__ = ["ModelExtras"]

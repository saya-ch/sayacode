"""所有模型共享的行为，上下文窗口，用量统计，消息转换，连通性检查。

状态不用私有属性写法，本类要与模型基类多重继承，
基类只在它处理的父类上收集私有属性，普通混入不在其列。
两种私有写法都不可用，声明在混入上读回描述符，声明在具体类上赋值后读回仍是默认。
因此状态一律通过实例字典存取，写入绕过校验，读取给默认值。
该方式在初始值，赋值，多实例隔离，序列化兼容上全部正确。
"""

from __future__ import annotations

from typing import Any, ClassVar, Iterator, Optional

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from ..i18n import tr
from .vocabulary import (
    ModelInfo,
    TokenUsage,
    parse_context_window,
    token_usage_from_mapping,
    token_usage_from_message,
)


class ModelExtras:
    """所有模型共享的实现。

    子类需提供模型名与调用方法，自有传输实现请继承自有基类，
    它会把对话等契约成员重新声明为抽象方法。
    """

    # 上下文窗口必须来自接口明确返回或用户显式输入，未知时保持零。
    #
    # 下面类级声明必须是类变量且不带下划线前缀，本类会与模型基类多重继承，
    # 基类要求非下划线类属性注解为类变量，否则报错，下划线属性会被接管，导致子类覆盖失效。
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
    def context_window(self, value: object) -> None:
        # 参数用 object，合法写法是正数或特定字符串，函数内已做解析分支
        """设置上下文窗口。

        三种结果语义互不重叠，合法正数记录并标记来源为手动。
        空值显式清空回到未知，允许清空是必要的，否则一旦设值就无法回到未知，
        而未知正是要求用户输入的正确前置状态。
        其它非法值忽略，不污染已有取值。
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
        """上下文窗口来源：api / manual / 空字符串（未知）。"""
        return str(self.__dict__.get("_context_window_source", ""))

    @context_window_source.setter
    def context_window_source(self, value: str) -> None:
        """设置上下文窗口来源标记。"""
        self.__dict__["_context_window_source"] = str(value or "")

    def detect_context_window(self) -> Optional[int]:
        """探测模型真实的上下文窗口。

        优先使用用户显式配置；否则走协议自己的探测。无法确定时返回 None ——
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
    def _extract_usage_from_response(response: object) -> TokenUsage:
        # 参数用 object，输入是各家响应的未知形态，函数内已做类型分支
        """从响应对象中提取用量。

        委托词汇表唯一解析入口，覆盖标准用量，开放风格元数据，
        直接挂载的用量属性。
        """
        if response is None:
            return TokenUsage()
        usage = token_usage_from_message(response)
        if usage:
            return usage
        for attr in ("usage", "token_usage"):
            value = getattr(response, attr, None)
            if not value:
                continue
            usage = token_usage_from_mapping(value)
            if usage:
                return usage
        return TokenUsage()

    @staticmethod
    def _estimate_usage_from_text(
        messages: list[dict[str, str]],
        response_text: str,
    ) -> TokenUsage:
        """流式响应通常不带用量，按字符数粗略估算。"""
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
        """把温度钳制到零到一。

        方法名不能与上游校验器同名，上游已按字段名约定注册了同名校验器。
        本混入在继承顺序中靠前，同名会覆盖那个校验器，导致签名不匹配，模型构造失败。
        改名即可避免覆盖，自有传输基类另保留了同义别名。
        """
        return max(0.0, min(1.0, temperature))

    # ── 消息转换 ────────────────────────────────────────────────────────────

    def convert_messages(self, messages: list[dict[str, str]]) -> list[BaseMessage]:
        """把字典转换成消息对象。"""
        converted: list[BaseMessage] = []
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
                # 未知类型按用户消息处理
                converted.append(HumanMessage(content=content))
        return converted

    def prepare_messages(
        self,
        user_input: str,
        system_prompt: Optional[str] = None,
        history: Optional[list[dict[str, str]]] = None,
    ) -> list[BaseMessage]:
        """组装系统提示加历史加当前输入的消息序列。"""
        messages: list[BaseMessage] = []
        if system_prompt:
            messages.append(SystemMessage(content=system_prompt))
        if history:
            messages.extend(self.convert_messages(history))
        messages.append(HumanMessage(content=user_input))
        return messages

    # ── 调用 ────────────────────────────────────────────────────────────────

    def _initialize_model(self) -> None:
        """兼容钩子。

        模型在构造时即完成初始化，无需额外动作，保留本方法让
        连通性检查与调用点继续成立。
        """
        return None

    def chat(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        # 额外参数透传给上游调用，键形态不一，保持 Any
        """发送对话请求（非流式），返回回复文本。"""
        response = self.invoke(self.convert_messages(messages), **kwargs)  # type: ignore[attr-defined]
        self._record_usage(self._extract_usage_from_response(response))
        return response.content if hasattr(response, "content") else str(response)

    def chat_stream(self, messages: list[dict[str, str]], **kwargs: Any) -> Iterator[str]:
        # 额外参数透传给上游调用，键形态不一，保持 Any
        """发送对话请求（流式），逐块产出回复文本。"""
        full_response = ""
        usage_chunk: object = None

        for chunk in self.stream(self.convert_messages(messages), **kwargs):  # type: ignore[attr-defined]
            # 判据必须是有没有内容属性，而不是内容是否非空。
            # 结束块与用量块的内容是空字符串，若按非空区分就会落进下面分支，
            # 把整个块对象的文本表示当正文产出。
            if hasattr(chunk, "content"):
                content = chunk.content
                if content:
                    full_response += content
                    yield content
            elif chunk:
                full_response += str(chunk)
                yield str(chunk)

            # 用量块不是最后一个块，其后通常还有一个空收尾块。
            # 只看最后一块会拿不到用量，静默退化成字符数估算。
            # 因此记住最后一个带用量的块。
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
    def extra_params(self) -> dict[str, Any]:
        # 构造时没被消费的额外参数，键形态不一，保持 Any
        """构造时未被模型类消费的额外参数（供 get_config 展示）。"""
        value = self.__dict__.get("_extra_params")
        return dict(value) if isinstance(value, dict) else {}

    def get_config(self) -> dict[str, Any]:
        # 扁平配置视图，值形态不一，保持 Any
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


def _read_int(source: object, key: str) -> int:
    # 参数用 object，兼容词汇表入口，输入可能是字典或属性对象
    """兼容别名，委托词汇表唯一入口。"""
    from .vocabulary import _read_int_value

    return _read_int_value(source, key)


__all__ = ["ModelExtras"]

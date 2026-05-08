"""
Anthropic Claude 模型集成

提供与 Anthropic Claude API 的集成。
"""

from importlib.util import find_spec
from typing import List, Dict, Any, Iterator, Optional
from langchain_core.messages import AIMessage

from ..i18n import tr
from .base import BaseModel, ModelInfo, TokenUsage, parse_context_window


def _check_anthropic_available():
    """检查 langchain_anthropic 是否可用"""
    return find_spec("langchain_anthropic") is not None


class AnthropicModel(BaseModel):
    """
    Anthropic Claude 模型封装

    使用 langchain_anthropic 库与 Claude API 通信。
    支持流式输出、自定义模型和参数配置。
    """

    def __init__(
        self,
        model_name: str = "claude-sonnet-4-20250514",
        api_key: Optional[str] = None,
        base_url: str = "https://api.anthropic.com/v1",
        temperature: float = 0.2,
        max_tokens: int = 4096,
        **kwargs
    ):
        """
        初始化 Anthropic Claude 模型

        Args:
            model_name: 模型名称（如 claude-sonnet-4-20250514）
            api_key: API 密钥
            base_url: API 基础 URL
            temperature: 温度参数（0-1）
            max_tokens: 最大生成 token 数
            **kwargs: 其他参数
        """
        super().__init__(
            model_name=model_name,
            temperature=temperature,
            base_url=base_url,
            api_key=api_key,
            max_tokens=max_tokens,
            **kwargs
        )
        self.api_key = api_key or self._get_api_key_from_env()
        self.base_url = base_url
        self.max_tokens = max_tokens
        self._model = None

    def _get_api_key_from_env(self) -> Optional[str]:
        """
        从环境变量获取 API 密钥

        Returns:
            API 密钥，如果未找到则返回 None
        """
        import os
        return os.getenv("ANTHROPIC_API_KEY")

    def _initialize_model(self):
        """初始化底层 Anthropic 模型客户端"""
        if self._model is not None:
            return
            
        # 检查依赖是否可用
        if not _check_anthropic_available():
            raise ImportError(
                "Anthropic 模型需要 langchain_anthropic 模块。\n"
                "请安装: pip install langchain-anthropic\n"
                "或者使用其他模型类型。"
            )
        
        from langchain_anthropic import ChatAnthropic
        
        # 构建初始化参数
        init_params = {
            "model": self.model_name,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        
        # 添加 API 密钥
        if self.api_key:
            init_params["anthropic_api_key"] = self.api_key
        else:
            raise ValueError(
                "Anthropic API 需要 API 密钥。\n"
                "请设置 ANTHROPIC_API_KEY 环境变量或在配置中提供。"
            )
        
        # 只在非默认 URL 时添加兼容端点
        if self.base_url and self.base_url != "https://api.anthropic.com/v1":
            init_params["anthropic_api_url"] = self.base_url
        
        # 添加其他额外参数，但排除构造过程中已处理的字段，
        # 避免把 api_key=None、base_url 等旧参数再次传给底层 SDK。
        excluded_keys = {
            "model",
            "model_name",
            "temperature",
            "max_tokens",
            "api_key",
            "base_url",
            "anthropic_api_key",
            "anthropic_api_url",
        }
        for key, value in self.extra_params.items():
            if key not in excluded_keys and value is not None:
                init_params[key] = value

        self._model = ChatAnthropic(**init_params)

    def _probe_api_for_context_window(self) -> Optional[int]:
        """通过 Anthropic Models API 查询模型上下文窗口。"""
        try:
            import requests

            headers = {
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "Accept": "application/json",
            }

            # Anthropic API: GET /v1/models/{model_name}
            # 返回包含 max_input_tokens 的模型信息
            url = f"{self.base_url}/models/{self.model_name}"
            resp = requests.get(url, headers=headers, timeout=15)
            if resp.status_code != 200:
                return None

            data = resp.json()
            # Anthropic 响应示例: {"type": "model", "id": "claude-sonnet-4-20250514", ...,
            #                       "max_input_tokens": 200000}
            max_tokens = parse_context_window(data.get("max_input_tokens"))
            if max_tokens:
                return max_tokens

            # 部分旧版本可能在嵌套结构中
            model_data = data.get("model", data)
            if isinstance(model_data, dict):
                max_tokens = parse_context_window(model_data.get("max_input_tokens"))
                if max_tokens:
                    return max_tokens

            return None

        except Exception as e:
            print(tr("model.context_probe_failed", error=str(e)))
            return None

    @staticmethod
    def _extract_text_content(content: Any) -> str:
        """将 Anthropic 的结构化 content 归一化为纯文本。"""
        if isinstance(content, str):
            return content

        if isinstance(content, list):
            text_parts = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text" and block.get("text"):
                        text_parts.append(str(block["text"]))
                elif hasattr(block, "type") and getattr(block, "type", None) == "text":
                    text_value = getattr(block, "text", None)
                    if text_value:
                        text_parts.append(str(text_value))

            if text_parts:
                return "\n".join(text_parts)

        return str(content)

    def chat(
        self,
        messages: List[Dict[str, str]],
        **kwargs
    ) -> str:
        """
        发送对话请求（非流式）

        Args:
            messages: 消息列表
            **kwargs: 其他参数

        Returns:
            模型回复文本
        """
        self._initialize_model()

        # 转换消息格式
        langchain_messages = self.convert_messages(messages)

        # 调用模型
        response = self._model.invoke(langchain_messages, **kwargs)

        # 记录 token 用量
        self._record_usage(self._extract_usage_from_response(response))

        if hasattr(response, 'content'):
            return self._extract_text_content(response.content)
        return str(response)

    def chat_stream(
        self,
        messages: List[Dict[str, str]],
        **kwargs
    ) -> Iterator[str]:
        """
        发送对话请求（流式）

        Args:
            messages: 消息列表
            **kwargs: 其他参数

        Yields:
            逐步返回模型回复的文本块
        """
        self._initialize_model()

        # 转换消息格式
        langchain_messages = self.convert_messages(messages)

        # 流式调用
        full_response = ""
        last_chunk = None
        for chunk in self._model.stream(langchain_messages, **kwargs):
            last_chunk = chunk
            if hasattr(chunk, 'content'):
                content = self._extract_text_content(chunk.content)
                if content:
                    full_response += content
                    yield content
            elif chunk:
                full_response += str(chunk)
                yield str(chunk)

        # 尝试提取用量
        if last_chunk is not None:
            usage = self._extract_usage_from_response(last_chunk)
            if usage.total_tokens > 0:
                self._record_usage(usage)
            else:
                estimated = self._estimate_usage_from_text(messages, full_response)
                self._record_usage(estimated)
        else:
            estimated = self._estimate_usage_from_text(messages, full_response)
            self._record_usage(estimated)

    def _estimate_usage_from_text(
        self,
        messages: List[Dict[str, str]],
        response_text: str,
    ) -> TokenUsage:
        """基于文本长度粗略估算 token 数（1 token ≈ 3 字符）"""
        prompt_chars = sum(len(m.get("content", "")) for m in messages)
        completion_chars = len(response_text)
        prompt_tokens = prompt_chars // 3
        completion_tokens = completion_chars // 3
        return TokenUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        )

    def chat_with_tools(
        self,
        messages: List[Dict[str, str]],
        tools: List[Any],
        **kwargs
    ) -> AIMessage:
        """
        使用工具调用进行对话

        Args:
            messages: 消息列表
            tools: 可用工具列表
            **kwargs: 其他参数

        Returns:
            AI 消息响应
        """
        self._initialize_model()

        # 绑定工具
        model_with_tools = self._model.bind_tools(tools)

        # 转换消息
        langchain_messages = self.convert_messages(messages)

        # 调用
        response = model_with_tools.invoke(langchain_messages, **kwargs)

        # 记录 token 用量
        self._record_usage(self._extract_usage_from_response(response))

        return response

    def get_model_info(self) -> ModelInfo:
        """
        获取模型信息

        Returns:
            ModelInfo 对象
        """
        return ModelInfo(
            name=self.model_name,
            model_type="anthropic",
            provider="Anthropic",
            supported_params=[
                "temperature",
                "max_tokens",
                "top_p",
                "top_k",
                "stop_sequences"
            ],
            supports_streaming=True,
            metadata={
                "base_url": self.base_url,
                "temperature": self.temperature,
                "max_tokens": self.max_tokens
            }
        )

    def check_connection(self) -> bool:
        """
        检查 API 连接状态

        Returns:
            连接是否可用
        """
        # 先检查依赖
        if not _check_anthropic_available():
            print(tr("model.missing_dependency", name="langchain-anthropic"))
            print(tr("model.install_hint", package="langchain-anthropic"))
            return False

        try:
            self._initialize_model()
            # 尝试调用模型
            test_messages = [{"role": "user", "content": "ping"}]
            self.chat(test_messages)
            # 连接成功后自动探测上下文窗口
            detected = self.detect_context_window()
            if detected:
                print(tr("connection.context_detected", context_window=f"{detected:,}"))
            return True
        except Exception as e:
            print(tr("connection.failed", error=str(e)))
            return False

    def __repr__(self) -> str:
        return f"AnthropicModel(model={self.model_name}, base_url={self.base_url})"


# 便捷函数：检查 Anthropic 支持
def is_anthropic_available() -> bool:
    """检查 Anthropic 模型是否可用（依赖已安装）"""
    return _check_anthropic_available()

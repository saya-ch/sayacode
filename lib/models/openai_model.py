"""
OpenAI 兼容模型集成

提供与 OpenAI API 及兼容服务（如 vLLM、LocalAI、Azure OpenAI 等）的集成。
"""

from typing import List, Dict, Any, Iterator, Optional
from langchain_core.messages import AIMessage
from .universal_chat_openai import UniversalChatOpenAI

from ..i18n import tr
from .base import BaseModel, ModelInfo, TokenUsage, parse_context_window


class OpenAIModel(BaseModel):
    """
    OpenAI 兼容模型封装

    使用 langchain_openai 库与 OpenAI API 或兼容服务通信。
    支持自定义 base_url（用于兼容 vLLM、LocalAI 等）。
    """

    def __init__(
        self,
        model_name: str = "gpt-4",
        base_url: str = "https://api.openai.com/v1",
        api_key: Optional[str] = None,
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
        **kwargs
    ):
        """
        初始化 OpenAI 兼容模型

        Args:
            model_name: 模型名称（如 gpt-4、gpt-3.5-turbo）
            base_url: API 基础 URL
            api_key: API 密钥（如果为 None，将尝试从环境变量获取）
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
        self.base_url = base_url
        self._explicit_api_key = api_key
        self.api_key = api_key or self._get_api_key_from_env()
        self.max_tokens = max_tokens
        self._model = None

    def _get_api_key_from_env(self) -> Optional[str]:
        """
        从环境变量获取 API 密钥

        Returns:
            API 密钥，如果未找到则返回 None
        """
        import os
        return os.getenv("OPENAI_API_KEY")

    def _is_deepseek(self) -> bool:
        """检测 base_url 是否指向 DeepSeek API。"""
        if not self.base_url:
            return False
        return "deepseek" in self.base_url.lower()

    def _initialize_model(self):
        """初始化底层模型客户端。

        自动检测 DeepSeek API 并切换到 ChatDeepSeek，
        确保 reasoning_content（思维链）在工具调用循环中正确透传。
        """
        if self._model is None:
            if self._is_deepseek():
                self._model = self._init_deepseek()
            else:
                self._model = self._init_openai()

    def _init_deepseek(self):
        """使用 UniversalChatDeepSeek（ChatDeepSeek + reasoning_content 注入修复）。"""
        try:
            from .universal_chat_openai import UniversalChatDeepSeek
        except ImportError:
            raise ImportError(
                "使用 DeepSeek API 需要安装 langchain-deepseek。\n"
                "运行: pip install langchain-deepseek"
            )

        # DeepSeek API Key: 优先显式传入，其次 DEEPSEEK_API_KEY，最后 OPENAI_API_KEY
        import os
        api_key = self._explicit_api_key or os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY")

        init_params = {
            "model": self.model_name,
            "temperature": self.temperature,
            "api_base": self.base_url,
        }
        if api_key:
            init_params["api_key"] = api_key
        if self.max_tokens:
            init_params["max_tokens"] = self.max_tokens
        # 过滤不应传给 ChatDeepSeek 的参数
        _deepseek_skip = frozenset((
            "model", "temperature", "api_base", "max_tokens",
            "openai_api_base", "openai_api_key", "api_key",
            "model_name", "base_url",
        ))
        extra = {k: v for k, v in self.extra_params.items() if k not in _deepseek_skip and v is not None}
        init_params.update(extra)
        return UniversalChatDeepSeek(**init_params)

    def _init_openai(self):
        """使用 UniversalChatOpenAI（自动透传非标准 additional_kwargs 字段）。

        替代原生 ChatOpenAI，确保 reasoning_content 等厂商特有字段
        在工具调用循环中不会丢失。
        """
        # 构建初始化参数
        init_params = {
            "model": self.model_name,
            "temperature": self.temperature,
        }

        # 如果 base_url 不是默认的 OpenAI 地址，使用它
        if self.base_url and self.base_url != "https://api.openai.com/v1":
            init_params["openai_api_base"] = self.base_url

        # 添加 API 密钥（如果有）
        if self.api_key:
            init_params["openai_api_key"] = self.api_key

        # 添加最大 token 数（如果有）
        if self.max_tokens:
            init_params["max_tokens"] = self.max_tokens

        # 添加其他参数
        init_params.update(self.extra_params)

        return UniversalChatOpenAI(**init_params)

    def _probe_api_for_context_window(self) -> Optional[int]:
        """通过 OpenAI 兼容 API 的 /v1/models/{model} 端点探测上下文窗口。"""
        try:
            import requests

            headers = {}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            headers.setdefault("Accept", "application/json")

            url = f"{self.base_url}/models/{self.model_name}"
            resp = requests.get(url, headers=headers, timeout=15)
            if resp.status_code != 200:
                return None

            data = resp.json()

            # 各 OpenAI 兼容服务在不同字段名中返回上下文长度
            # 按优先级尝试常见字段名
            field_candidates = [
                # vLLM / 大多数开源推理引擎
                "max_model_len",
                "max_context_length",
                # TGI / Text Generation Inference
                "max_sequence_length",
                "max_total_tokens",
                # 通用命名
                "context_length",
                "context_window",
                "max_position_embeddings",
                "n_positions",
                "model_max_length",
                # 模型规格嵌套场景
                "max_length",
            ]

            def _search(data: Any, fields: List[str], depth: int = 0) -> Optional[int]:
                """递归搜索嵌套结构中的上下文窗口字段。"""
                if depth > 3:
                    return None
                if isinstance(data, dict):
                    for field in fields:
                        parsed = parse_context_window(data.get(field))
                        if parsed:
                            return parsed
                    for val in data.values():
                        result = _search(val, fields, depth + 1)
                        if result:
                            return result
                elif isinstance(data, list):
                    for item in data:
                        result = _search(item, fields, depth + 1)
                        if result:
                            return result
                return None

            return _search(data, field_candidates)

        except Exception as e:
            print(tr("model.context_probe_failed", error=str(e)))
            return None

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

        return response.content if hasattr(response, 'content') else str(response)

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

        # 流式调用：流式结束后通过额外请求获取用量，或尝试从最后一个 chunk 提取
        full_response = ""
        last_chunk = None
        for chunk in self._model.stream(langchain_messages, **kwargs):
            last_chunk = chunk
            if hasattr(chunk, 'content'):
                content = chunk.content
                if content:
                    full_response += content
                    yield content
            elif chunk:
                full_response += str(chunk)
                yield str(chunk)

        # 尝试从最后一个 chunk 提取用量（部分 provider 会在最后一个 chunk 附带 usage）
        if last_chunk is not None:
            usage = self._extract_usage_from_response(last_chunk)
            if usage.total_tokens > 0:
                self._record_usage(usage)
            else:
                # 流式通常不返回用量，记录一个基于文本长度的估算值作为 fallback
                estimated = self._estimate_usage_from_text(messages, full_response)
                self._record_usage(estimated)
        else:
            estimated = self._estimate_usage_from_text(messages, full_response)
            self._record_usage(estimated)

    def _estimate_usage_from_text(
        self,
        messages: List[Dict[str, str]],
        response_text: str,
    ) -> "TokenUsage":
        """基于文本长度粗略估算 token 数（1 token ≈ 4 字符，中文约 2 字符/token）"""
        prompt_chars = sum(len(m.get("content", "")) for m in messages)
        completion_chars = len(response_text)
        # 混合语言保守估算：1 token ≈ 3 字符
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
            model_type="openai",
            provider=self._get_provider_name(),
            supported_params=[
                "temperature",
                "max_tokens",
                "top_p",
                "frequency_penalty",
                "presence_penalty",
                "stop"
            ],
            supports_streaming=True,
            metadata={
                "base_url": self.base_url,
                "temperature": self.temperature,
                "max_tokens": self.max_tokens
            }
        )

    def _get_provider_name(self) -> str:
        """
        根据 base_url 推断服务提供商

        Returns:
            提供商名称
        """
        if not self.base_url:
            return "OpenAI"

        url_lower = self.base_url.lower()

        if "deepseek" in url_lower:
            return "DeepSeek"
        elif "azure" in url_lower:
            return "Azure OpenAI"
        elif "claude" in url_lower:
            return "Anthropic"
        elif "vllm" in url_lower:
            return "vLLM"
        elif "localai" in url_lower:
            return "LocalAI"
        elif "groq" in url_lower:
            return "Groq"
        elif "ollama" in url_lower:
            return "Ollama"
        else:
            return "OpenAI Compatible"

    def check_connection(self) -> bool:
        """
        检查 API 连接状态

        Returns:
            连接是否可用
        """
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

    def list_models(self) -> List[str]:
        """
        获取可用模型列表（如果 API 支持）

        Returns:
            模型名称列表
        """
        try:
            import requests

            headers = {}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"

            response = requests.get(
                f"{self.base_url}/models",
                headers=headers,
                timeout=10,
            )

            if response.status_code == 200:
                data = response.json()
                if 'data' in data:
                    return [model['id'] for model in data['data']]
        except Exception as e:
            print(tr("model.list_failed", error=str(e)))

        return []

    def __repr__(self) -> str:
        return f"OpenAIModel(model={self.model_name}, base_url={self.base_url})"


class AzureOpenAIModel(OpenAIModel):
    """Azure OpenAI 专用模型封装"""

    def __init__(
        self,
        model_name: str,
        api_key: str,
        azure_endpoint: str,
        api_version: str = "2024-02-01",
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
        **kwargs
    ):
        """
        初始化 Azure OpenAI 模型

        Args:
            model_name: 部署名称
            api_key: Azure API 密钥
            azure_endpoint: Azure 端点
            api_version: API 版本
            temperature: 温度参数
            max_tokens: 最大 token 数
            **kwargs: 其他参数
        """
        super().__init__(
            model_name=model_name,
            base_url=azure_endpoint,
            api_key=api_key,
            temperature=temperature,
            max_tokens=max_tokens,
            api_version=api_version,
            **kwargs
        )

    def _initialize_model(self):
        """初始化 Azure OpenAI 模型"""
        from langchain_openai import AzureChatOpenAI

        if self._model is None:
            self._model = AzureChatOpenAI(
                azure_deployment=self.model_name,
                openai_api_key=self.api_key,
                azure_endpoint=self.base_url,
                api_version=self.extra_params.get("api_version", "2024-02-01"),
                temperature=self.temperature,
                max_tokens=self.max_tokens
            )

    def _get_provider_name(self) -> str:
        return "Azure OpenAI"

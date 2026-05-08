"""
Ollama 模型集成

提供与本地 Ollama 服务的集成，支持本地大语言模型调用。
"""

from typing import List, Dict, Any, Iterator, Optional
from langchain_ollama import ChatOllama
from langchain_core.messages import AIMessage

from ..i18n import tr
from .base import BaseModel, ModelInfo, TokenUsage, parse_context_window


def _normalize_ollama_base_url(base_url: str) -> str:
    """兼容旧配置中保存的 OpenAI-compatible /v1 Ollama 地址。"""
    normalized = (base_url or "http://localhost:11434").rstrip("/")
    if normalized.endswith("/v1"):
        return normalized[:-3]
    return normalized


class OllamaModel(BaseModel):
    """
    Ollama 模型封装

    使用 langchain_ollama 库与本地 Ollama 服务通信。
    支持流式输出、自定义模型和温度参数配置。
    """

    def __init__(
        self,
        model_name: str = "qwen3.5:9b",
        base_url: str = "http://localhost:11434",
        temperature: float = 0.2,
        **kwargs
    ):
        """
        初始化 Ollama 模型

        Args:
            model_name: Ollama 模型名称
            base_url: Ollama 服务器地址
            temperature: 温度参数（0-1）
            **kwargs: 其他参数，如 timeout、stream 等
        """
        normalized_base_url = _normalize_ollama_base_url(base_url)
        super().__init__(
            model_name=model_name,
            temperature=temperature,
            base_url=normalized_base_url,
            **kwargs
        )
        self.base_url = normalized_base_url
        self.timeout = float(kwargs.get("timeout", 10) or 10)
        self._model = None
        # Ollama 本地模型窗口差异大；仅信任用户显式传入的 num_ctx/context_window 或 Show API。
        if self._context_window <= 0:
            configured_num_ctx = parse_context_window(kwargs.get("num_ctx"))
            if configured_num_ctx:
                self._context_window = configured_num_ctx
                self._context_window_source = "manual"

    def _initialize_model(self):
        """初始化底层 Ollama 模型客户端"""
        if self._model is None:
            # 构建初始化参数 - 只包含必要的参数
            init_params = {
                "model": self.model_name,
                "temperature": self.temperature,
            }
            
            # 只在非默认URL时添加 base_url
            if self.base_url and self.base_url != "http://localhost:11434":
                init_params["base_url"] = self.base_url
            
            # 添加其他额外参数（但排除 base_url 避免重复）
            for key, value in self.extra_params.items():
                if key != "base_url" and key != "url":
                    init_params[key] = value

            self._model = ChatOllama(**init_params)

    def _probe_api_for_context_window(self) -> Optional[int]:
        """通过 Ollama Show API 查询模型实际的上下文窗口配置。

        探测逻辑：
        1. 从 model_info 获取模型原生的 context_length（如 llama.context_length）
        2. 从 modelfile 获取运行时 num_ctx 配置
        3. 有效上下文 = min(native_ctx, runtime_num_ctx)
        """
        try:
            import requests

            url = f"{self.base_url}/api/show"
            resp = requests.post(
                url,
                json={"name": self.model_name},
                timeout=15,
            )
            if resp.status_code != 200:
                return None

            data = resp.json()

            # 方式1: 从 model_info 中提取原生上下文长度（Ollama 0.3+）
            # model_info 的 key 为架构相关格式，如 "llama.context_length" / "qwen2.context_length"
            native_ctx = None
            model_info = data.get("model_info", {})
            if model_info:
                for key, val in model_info.items():
                    parsed = parse_context_window(val)
                    if parsed:
                        key_lower = key.lower()
                        if "context_length" in key_lower or "context_len" in key_lower:
                            native_ctx = parsed
                            break

            # 方式2: 从 modelfile 中解析运行时 num_ctx（Ollama 默认 2048）
            runtime_ctx = None
            modelfile = data.get("modelfile", "")
            if modelfile:
                for line in modelfile.split("\n"):
                    line = line.strip()
                    if line.lower().startswith("num_ctx"):
                        parts = line.split()
                        if len(parts) >= 2:
                            try:
                                parsed = parse_context_window(parts[1])
                                if parsed:
                                    runtime_ctx = parsed
                            except (TypeError, ValueError):
                                pass

            # 方式3: 取较小值作为有效上下文
            if native_ctx is not None and runtime_ctx is not None:
                return min(native_ctx, runtime_ctx)
            elif native_ctx is not None:
                return native_ctx
            elif runtime_ctx is not None:
                return runtime_ctx

            return None

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

        # 记录 token 用量（Ollama 通常不返回用量，提取可能为空）
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

        # 流式调用
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
            model_type="ollama",
            provider="Ollama",
            supported_params=[
                "temperature",
                "top_p",
                "top_k",
                "repeat_penalty",
                "num_ctx",
                "seed",
                "stop"
            ],
            supports_streaming=True,
            metadata={
                "base_url": self.base_url,
                "temperature": self.temperature
            }
        )

    def check_connection(self) -> bool:
        """
        检查 Ollama 服务连接状态

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

    def list_available_models(self) -> List[str]:
        """
        获取 Ollama 服务器上可用的模型列表

        Returns:
            模型名称列表
        """
        try:
            import requests
            response = requests.get(f"{self.base_url}/api/tags", timeout=self.timeout)
            if response.status_code == 200:
                data = response.json()
                return [model['name'] for model in data.get('models', [])]
        except Exception as e:
            print(tr("model.list_failed", error=str(e)))

        return []

    def pull_model(self, model_name: Optional[str] = None) -> bool:
        """
        从 Ollama 拉取模型

        Args:
            model_name: 要拉取的模型名称，默认为当前模型

        Returns:
            是否成功
        """
        try:
            import requests
            import threading

            target_model = model_name or self.model_name

            def pull():
                requests.post(
                    f"{self.base_url}/api/pull",
                    json={"name": target_model},
                    timeout=self.timeout,
                )

            # 后台线程拉取
            threading.Thread(target=pull, daemon=True).start()
            print(tr("model.pulling", model=target_model))
            return True

        except Exception as e:
            print(tr("model.pull_failed", error=str(e)))
            return False

    def __repr__(self) -> str:
        return f"OllamaModel(model={self.model_name}, base_url={self.base_url})"

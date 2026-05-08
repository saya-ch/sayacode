"""
Google Gemini 模型集成

使用官方 REST API 与 Gemini 模型通信，不依赖额外 SDK。
"""

from __future__ import annotations

import json
import os
from typing import List, Dict, Any, Iterator, Optional

import requests

from ..i18n import tr
from .base import BaseModel, ModelInfo, TokenUsage, parse_context_window


class GeminiModel(BaseModel):
    """Google Gemini REST API 封装。"""

    def __init__(
        self,
        model_name: str = "gemini-2.5-flash",
        base_url: str = "https://generativelanguage.googleapis.com/v1beta",
        api_key: Optional[str] = None,
        temperature: float = 0.2,
        timeout: float = 60.0,
        **kwargs
    ):
        super().__init__(
            model_name=model_name,
            temperature=temperature,
            base_url=base_url,
            api_key=api_key,
            timeout=timeout,
            **kwargs
        )
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or os.getenv("GEMINI_API_KEY")
        self.timeout = timeout

    def _initialize_model(self):
        """Gemini 使用 REST API，请求时无需额外初始化。"""
        return None

    def _probe_api_for_context_window(self) -> Optional[int]:
        """通过 Gemini Models API 查询模型上下文窗口。"""
        try:
            import requests

            if not self.api_key:
                return None

            # Gemini Model API: GET /v1beta/models/{model}
            # 返回包含 inputTokenLimit 的模型信息
            url = f"{self.base_url}/models/{self.model_name}"
            params = {"key": self.api_key}
            resp = requests.get(url, params=params, timeout=15)
            if resp.status_code != 200:
                return None

            data = resp.json()
            # Gemini 响应示例: {"name": "models/gemini-2.5-flash",
            #                    "inputTokenLimit": 1048576, ...}
            limit = parse_context_window(data.get("inputTokenLimit"))
            if limit:
                return limit

            # 部分端点可能在嵌套结构中
            for field in ("input_token_limit", "max_input_tokens", "context_window"):
                val = parse_context_window(data.get(field))
                if val:
                    return val

            return None

        except Exception as e:
            print(tr("model.context_probe_failed", error=str(e)))
            return None

    def _build_url(self, streaming: bool = False) -> str:
        action = "streamGenerateContent?alt=sse" if streaming else "generateContent"
        return f"{self.base_url}/models/{self.model_name}:{action}"

    def _build_headers(self) -> Dict[str, str]:
        if not self.api_key:
            raise ValueError("Gemini API 需要 API Key，请设置 GEMINI_API_KEY 或在配置中提供。")

        return {
            "Content-Type": "application/json",
            "x-goog-api-key": self.api_key,
        }

    def _build_payload(self, messages: List[Dict[str, str]], tools: Optional[List[Any]] = None) -> Dict[str, Any]:
        contents = []
        system_parts = []

        for message in messages:
            role = message.get("role", "user")
            content = message.get("content", "")

            if not content:
                continue

            if role == "system":
                system_parts.append({"text": content})
                continue

            gemini_role = "model" if role == "assistant" else "user"
            contents.append({
                "role": gemini_role,
                "parts": [{"text": content}],
            })

        payload: Dict[str, Any] = {
            "contents": contents or [{"role": "user", "parts": [{"text": ""}]}],
            "generationConfig": {
                "temperature": self.temperature,
            },
        }

        max_tokens = self.extra_params.get("max_tokens")
        if max_tokens:
            payload["generationConfig"]["maxOutputTokens"] = max_tokens

        if system_parts:
            payload["systemInstruction"] = {"parts": system_parts}

        # 工具调用支持
        if tools:
            payload["tools"] = self._convert_tools_to_gemini(tools)

        return payload

    def _convert_tools_to_gemini(self, tools: List[Any]) -> List[Dict[str, Any]]:
        """将 LangChain tools 转换为 Gemini function_declarations 格式。"""
        declarations = []
        for tool in tools:
            try:
                args_schema = tool.get_input_schema().model_json_schema()
            except (AttributeError, NotImplementedError):
                args_schema = getattr(tool, "args", None) or {}

            declaration: Dict[str, Any] = {
                "name": tool.name,
                "description": tool.description,
            }
            if args_schema:
                declaration["parameters"] = args_schema
            declarations.append(declaration)

        return [{"function_declarations": declarations}]

    def _extract_text(self, response_data: Dict[str, Any]) -> str:
        texts: List[str] = []

        for candidate in response_data.get("candidates", []):
            parts = candidate.get("content", {}).get("parts", [])
            for part in parts:
                text = part.get("text")
                if text:
                    texts.append(text)

        return "".join(texts)

    def _extract_function_calls(self, response_data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """从 Gemini 响应中提取 functionCall。"""
        calls = []
        for candidate in response_data.get("candidates", []):
            parts = candidate.get("content", {}).get("parts", [])
            for part in parts:
                if "functionCall" in part:
                    fc = part["functionCall"]
                    calls.append({
                        "name": fc.get("name"),
                        "arguments": fc.get("args", {}),
                    })
        return calls

    def chat(
        self,
        messages: List[Dict[str, str]],
        **kwargs
    ) -> str:
        self._initialize_model()

        response = requests.post(
            self._build_url(streaming=False),
            headers=self._build_headers(),
            json=self._build_payload(messages),
            timeout=kwargs.pop("timeout", self.timeout),
        )
        response.raise_for_status()
        data = response.json()

        text = self._extract_text(data)

        # 尝试从 Gemini 响应中提取 token 用量
        usage = TokenUsage()
        if "usageMetadata" in data:
            meta = data["usageMetadata"]
            usage.prompt_tokens = int(meta.get("promptTokenCount", 0))
            usage.completion_tokens = int(meta.get("candidatesTokenCount", 0))
            usage.total_tokens = int(meta.get("totalTokenCount", usage.prompt_tokens + usage.completion_tokens))
        self._record_usage(usage)

        if text:
            return text

        return json.dumps(data, ensure_ascii=False)

    def chat_stream(
        self,
        messages: List[Dict[str, str]],
        **kwargs
    ) -> Iterator[str]:
        self._initialize_model()

        response = requests.post(
            self._build_url(streaming=True),
            headers=self._build_headers(),
            json=self._build_payload(messages),
            timeout=kwargs.pop("timeout", self.timeout),
            stream=True,
        )
        response.raise_for_status()

        full_response = ""
        total_prompt_tokens = 0
        total_completion_tokens = 0

        for raw_line in response.iter_lines(decode_unicode=True):
            if not raw_line or not raw_line.startswith("data: "):
                continue

            payload = raw_line[6:].strip()
            if payload == "[DONE]":
                break

            try:
                data = json.loads(payload)
            except json.JSONDecodeError:
                continue

            text = self._extract_text(data)
            if text:
                full_response += text
                yield text

            # Gemini 流式可能在每个 chunk 都带 usageMetadata
            if "usageMetadata" in data:
                meta = data["usageMetadata"]
                total_prompt_tokens = max(total_prompt_tokens, int(meta.get("promptTokenCount", 0)))
                total_completion_tokens = max(total_completion_tokens, int(meta.get("candidatesTokenCount", 0)))

        # 记录流式用量
        usage = TokenUsage(
            prompt_tokens=total_prompt_tokens,
            completion_tokens=total_completion_tokens,
            total_tokens=total_prompt_tokens + total_completion_tokens,
        )
        if usage.total_tokens > 0:
            self._record_usage(usage)
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
    ) -> str:
        """支持工具调用。将 tools 转为 Gemini function_declarations，解析返回的 functionCall。"""
        self._initialize_model()

        response = requests.post(
            self._build_url(streaming=False),
            headers=self._build_headers(),
            json=self._build_payload(messages, tools=tools),
            timeout=kwargs.pop("timeout", self.timeout),
        )
        response.raise_for_status()
        data = response.json()

        usage = TokenUsage()
        if "usageMetadata" in data:
            meta = data["usageMetadata"]
            usage.prompt_tokens = int(meta.get("promptTokenCount", 0))
            usage.completion_tokens = int(meta.get("candidatesTokenCount", 0))
            usage.total_tokens = int(meta.get("totalTokenCount", usage.prompt_tokens + usage.completion_tokens))
        self._record_usage(usage)

        text = self._extract_text(data)
        function_calls = self._extract_function_calls(data)

        if function_calls:
            return json.dumps({"tool_calls": function_calls}, ensure_ascii=False)

        if text:
            return text

        return json.dumps(data, ensure_ascii=False)

    def get_model_info(self) -> ModelInfo:
        return ModelInfo(
            name=self.model_name,
            model_type="gemini",
            provider="Google Gemini",
            supported_params=[
                "temperature",
                "max_tokens",
            ],
            supports_streaming=True,
            metadata={
                "base_url": self.base_url,
                "temperature": self.temperature,
            }
        )

    def check_connection(self) -> bool:
        try:
            self.chat([{"role": "user", "content": "ping"}])
            # 连接成功后自动探测上下文窗口
            detected = self.detect_context_window()
            if detected:
                print(tr("connection.context_detected", context_window=f"{detected:,}"))
            return True
        except Exception as e:
            print(tr("connection.failed", error=str(e)))
            return False

    def __repr__(self) -> str:
        return f"GeminiModel(model={self.model_name}, base_url={self.base_url})"

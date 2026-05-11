"""
Google Gemini 模型集成

使用官方 REST API 与 Gemini 模型通信，不依赖额外 SDK。
"""

from __future__ import annotations

import json
import os
from typing import List, Dict, Any, Iterator, Optional

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field
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
            url = f"{self.base_url}/{self._model_path()}"
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

    def _model_path(self) -> str:
        model_name = self.model_name.strip("/")
        return model_name if model_name.startswith("models/") else f"models/{model_name}"

    def _build_url(self, streaming: bool = False) -> str:
        action = "streamGenerateContent?alt=sse" if streaming else "generateContent"
        return f"{self.base_url}/{self._model_path()}:{action}"

    def _build_headers(self) -> Dict[str, str]:
        if not self.api_key:
            raise ValueError("Gemini API 需要 API Key，请设置 GEMINI_API_KEY 或在配置中提供。")

        return {
            "Content-Type": "application/json",
            "x-goog-api-key": self.api_key,
        }

    def bind_tools(self, tools: List[Any], **kwargs: Any) -> "GeminiChatModel":
        """Return a LangChain-compatible chat model with tools bound."""
        return GeminiChatModel(
            gemini_model=self,
            bound_tools=list(tools or []),
            tool_choice=kwargs.get("tool_choice"),
        )

    def as_chat_model(self) -> "GeminiChatModel":
        """Expose this REST wrapper as a LangChain BaseChatModel."""
        return GeminiChatModel(gemini_model=self)

    def _post_generate_content(
        self,
        payload: Dict[str, Any],
        *,
        streaming: bool = False,
        timeout: Optional[float] = None,
        **request_kwargs: Any,
    ) -> Dict[str, Any]:
        response = requests.post(
            self._build_url(streaming=streaming),
            headers=self._build_headers(),
            json=payload,
            timeout=timeout if timeout is not None else self.timeout,
            **request_kwargs,
        )
        response.raise_for_status()
        return response.json()

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

    def _build_payload_from_langchain_messages(
        self,
        messages: List[BaseMessage],
        tools: Optional[List[Any]] = None,
    ) -> Dict[str, Any]:
        contents: list[Dict[str, Any]] = []
        system_parts: list[Dict[str, str]] = []
        tool_call_names: dict[str, str] = {}

        for message in messages:
            if isinstance(message, SystemMessage):
                text = _content_to_text(message.content)
                if text:
                    system_parts.append({"text": text})
                continue

            if isinstance(message, ToolMessage):
                name = getattr(message, "name", None) or tool_call_names.get(str(message.tool_call_id))
                if not name:
                    name = str(message.tool_call_id or "tool_result")
                contents.append({
                    "role": "user",
                    "parts": [{
                        "functionResponse": {
                            "name": name,
                            "response": _coerce_function_response(message.content),
                        },
                    }],
                })
                continue

            role = "user"
            if isinstance(message, AIMessage):
                role = "model"

            parts: list[Dict[str, Any]] = []
            text = _content_to_text(message.content)
            if text:
                parts.append({"text": text})

            if isinstance(message, AIMessage):
                for call in _iter_message_tool_calls(message):
                    name = call.get("name") or ""
                    if not name:
                        continue
                    call_id = call.get("id")
                    if call_id:
                        tool_call_names[str(call_id)] = name
                    parts.append({
                        "functionCall": {
                            "name": name,
                            "args": _coerce_tool_args(call.get("args", {})),
                        },
                    })

            if parts:
                contents.append({"role": role, "parts": parts})

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
        if tools:
            payload["tools"] = self._convert_tools_to_gemini(tools)
        return payload

    def _generate_from_langchain_messages(
        self,
        messages: List[BaseMessage],
        *,
        tools: Optional[List[Any]] = None,
        stop: Optional[List[str]] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        payload = self._build_payload_from_langchain_messages(messages, tools=tools)
        if stop:
            payload.setdefault("generationConfig", {})["stopSequences"] = list(stop)
        return self._post_generate_content(payload, timeout=timeout)

    def _convert_tools_to_gemini(self, tools: List[Any]) -> List[Dict[str, Any]]:
        """将 LangChain tools 转换为 Gemini function_declarations 格式。"""
        declarations = []
        for tool in tools:
            try:
                args_schema = tool.get_input_schema().model_json_schema()
            except (AttributeError, NotImplementedError):
                args_schema = getattr(tool, "args", None) or {}
            args_schema = _sanitize_gemini_schema(args_schema)

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

    def _usage_from_response_data(self, response_data: Dict[str, Any]) -> TokenUsage:
        meta = response_data.get("usageMetadata") or {}
        prompt_tokens = int(meta.get("promptTokenCount", 0) or 0)
        completion_tokens = int(meta.get("candidatesTokenCount", 0) or 0)
        total_tokens = int(meta.get("totalTokenCount", prompt_tokens + completion_tokens) or 0)
        return TokenUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
        )

    def chat(
        self,
        messages: List[Dict[str, str]],
        **kwargs
    ) -> str:
        self._initialize_model()

        data = self._post_generate_content(
            self._build_payload(messages),
            timeout=kwargs.pop("timeout", self.timeout),
        )

        text = self._extract_text(data)

        self._record_usage(self._usage_from_response_data(data))

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

        data = self._post_generate_content(
            self._build_payload(messages, tools=tools),
            timeout=kwargs.pop("timeout", self.timeout),
        )

        self._record_usage(self._usage_from_response_data(data))

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


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") == "text" and item.get("text"):
                parts.append(str(item["text"]))
        return "\n".join(parts)
    return str(content)


def _coerce_tool_args(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _coerce_function_response(content: Any) -> Dict[str, Any]:
    if isinstance(content, dict):
        return content
    if isinstance(content, str):
        text = content.strip()
        if text:
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    return parsed
                return {"result": parsed}
            except json.JSONDecodeError:
                pass
        return {"result": content}
    return {"result": content}


def _sanitize_gemini_schema(value: Any) -> Any:
    """Trim common Pydantic JSON Schema fields that Gemini function declarations reject."""
    if isinstance(value, list):
        return [_sanitize_gemini_schema(item) for item in value]
    if not isinstance(value, dict):
        return value

    if "anyOf" in value:
        non_null = [
            item for item in value.get("anyOf", [])
            if not (isinstance(item, dict) and item.get("type") == "null")
        ]
        if len(non_null) == 1:
            merged = {k: v for k, v in value.items() if k != "anyOf"}
            merged.update(non_null[0])
            return _sanitize_gemini_schema(merged)

    unsupported = {"$defs", "definitions", "title", "examples", "default"}
    result: Dict[str, Any] = {}
    for key, item in value.items():
        if key in unsupported:
            continue
        if key == "additionalProperties" and isinstance(item, bool):
            continue
        result[key] = _sanitize_gemini_schema(item)
    return result


def _iter_message_tool_calls(message: AIMessage) -> Iterator[Dict[str, Any]]:
    tool_calls = list(getattr(message, "tool_calls", None) or [])
    if tool_calls:
        for call in tool_calls:
            if isinstance(call, dict):
                yield {
                    "id": call.get("id"),
                    "name": call.get("name"),
                    "args": call.get("args", {}),
                }
        return

    for raw in (message.additional_kwargs or {}).get("tool_calls", []) or []:
        if not isinstance(raw, dict):
            continue
        function = raw.get("function") if isinstance(raw.get("function"), dict) else {}
        yield {
            "id": raw.get("id"),
            "name": raw.get("name") or function.get("name"),
            "args": raw.get("args", function.get("arguments", {})),
        }


class GeminiChatModel(BaseChatModel):
    """LangChain chat adapter for the local Gemini REST wrapper."""

    gemini_model: GeminiModel
    bound_tools: List[Any] = Field(default_factory=list)
    tool_choice: Optional[str] = None

    @property
    def _llm_type(self) -> str:
        return "sayacode-gemini-rest"

    def bind_tools(
        self,
        tools: List[Any],
        *,
        tool_choice: Optional[str] = None,
        **kwargs: Any,
    ) -> "GeminiChatModel":
        return self.__class__(
            gemini_model=self.gemini_model,
            bound_tools=list(tools or []),
            tool_choice=tool_choice or self.tool_choice,
        )

    def _generate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        data = self.gemini_model._generate_from_langchain_messages(
            messages,
            tools=self.bound_tools,
            stop=stop,
            timeout=kwargs.pop("timeout", self.gemini_model.timeout),
        )
        text = self.gemini_model._extract_text(data)
        usage = self.gemini_model._usage_from_response_data(data)
        self.gemini_model._record_usage(usage)

        tool_calls: list[Dict[str, Any]] = []
        for index, call in enumerate(self.gemini_model._extract_function_calls(data)):
            name = call.get("name")
            if not name:
                continue
            args = _coerce_tool_args(call.get("arguments", {}))
            tool_calls.append({
                "name": name,
                "args": args,
                "id": f"gemini_{index}_{name}",
                "type": "tool_call",
            })

        usage_metadata = None
        if usage.total_tokens > 0:
            usage_metadata = {
                "input_tokens": usage.prompt_tokens,
                "output_tokens": usage.completion_tokens,
                "total_tokens": usage.total_tokens,
            }

        first_candidate = (data.get("candidates") or [{}])[0]
        response_metadata = {
            "model_name": self.gemini_model.model_name,
            "finish_reason": first_candidate.get("finishReason"),
        }
        message = AIMessage(
            content=text or "",
            tool_calls=tool_calls,
            usage_metadata=usage_metadata,
            response_metadata=response_metadata,
        )
        generation = ChatGeneration(
            message=message,
            generation_info={"finish_reason": first_candidate.get("finishReason")},
        )
        return ChatResult(
            generations=[generation],
            llm_output={
                "token_usage": {
                    "prompt_tokens": usage.prompt_tokens,
                    "completion_tokens": usage.completion_tokens,
                    "total_tokens": usage.total_tokens,
                },
                "model_name": self.gemini_model.model_name,
            },
        )

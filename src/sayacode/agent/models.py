"""按接口协议构造官方模型的唯一入口。

只做选型和参数拼装，不做重试和鉴权修复。
调用方传入画像即可拿到可直接装进图的模型。"""

from __future__ import annotations

from typing import Any

from langchain.chat_models import init_chat_model

from ..config import Profile

_KEYLESS_API_KEY = "sayacode-keyless-endpoint"


def model_for(profile: Profile, override: Any = None) -> Any:
    """按画像协议选适配器并构造聊天模型。

    参数是画像加可选的外部模型，外部模型存在时直接返回。
    返回值是官方聊天模型，可直接交给图工厂装配。
    坑点是非本地协议必须带占位密钥，否则会误读环境变量。
    本地协议走自有地址和请求头，输出长度字段随适配器而异。"""
    if override is not None:
        return override
    options: dict[str, Any] = {"base_url": profile.base_url}
    if profile.protocol != "ollama_native_chat":
        # 否则会从环境读取厂商密钥。
        # 可能误发到用户自配的地址。
        options["api_key"] = profile.api_key or _KEYLESS_API_KEY
    if profile.protocol == "openai_chat_completions":
        adapter = "openai"
        options["use_responses_api"] = False
        options["max_tokens"] = profile.max_output_tokens
    elif profile.protocol == "openai_responses":
        adapter = "openai"
        options["use_responses_api"] = True
        options["max_tokens"] = profile.max_output_tokens
    elif profile.protocol == "anthropic_messages":
        adapter = "anthropic"
        options["max_tokens"] = profile.max_output_tokens
    elif profile.protocol == "gemini_generate_content":
        adapter = "google_genai"
        options["max_output_tokens"] = profile.max_output_tokens
        options["vertexai"] = False
    elif profile.protocol == "ollama_native_chat":
        adapter = "ollama"
        options["num_predict"] = profile.max_output_tokens
        options["num_ctx"] = profile.context_length
        options["client_kwargs"] = {
            "headers": {"Authorization": f"Bearer {profile.api_key or _KEYLESS_API_KEY}"}
        }
    else:
        raise ValueError(f"unsupported model protocol: {profile.protocol}")
    return init_chat_model(
        profile.model_id,
        model_provider=adapter,
        **options,
    )


def _model_error_message(exc: Exception, profile: Profile | None = None) -> str:
    """把认证失败转成可操作的中文提示，不回显原始错误正文。

    参数是原始异常加可选画像，返回可直接展示的字符串。
    坑点是只看链上的未授权状态，其他错误原样返回并脱敏密钥。"""
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        status = getattr(current, "status_code", None)
        response = getattr(current, "response", None)
        if status is None and response is not None:
            status = getattr(response, "status_code", None)
        if status == 401:
            if profile is not None and profile.api_key is None:
                return (
                    f"HTTP 401：配置 {profile.name} 未设置 API Key。"
                    "请在 WebUI 的模型设置中补填后重试。"
                )
            if profile is not None:
                return (
                    f"HTTP 401：接口拒绝了配置 {profile.name} 的 API Key。"
                    "请在 WebUI 的模型设置中更新后重试。"
                )
            return "HTTP 401：接口拒绝了 API Key。请检查模型配置。"
        current = current.__cause__ or current.__context__
    message = str(exc)
    if profile is not None and profile.api_key:
        message = message.replace(profile.api_key, "***")
    return message

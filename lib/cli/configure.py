"""
模型配置模块

包含模型配置、连接测试、上下文窗口辅助函数等。
"""

import os
from typing import Any, Dict, Optional
from urllib.parse import urlparse


from lib.theme import (
    console,
    print_status,
    print_success,
    print_warning,
    print_error,
    print_info,
    print_banner,
    print_summary_card,
    SayacodeColors,
)
from lib.models import parse_context_window
from lib.models.registry import get_model_provider_registry
from lib.runtime import (
    extract_context_window_from_config as _extract_context_window_from_config,
    LaunchModelOverrides,
    ModelLaunchResolver,
    store_context_window_in_config as _store_context_window_in_config,
)
from lib.api_config import APIConfigManager
from lib.i18n import tr
from lib.cli.parser import select_model_protocol, _protocol_options, _protocol_defaults
from lib.cli.permissions import _supports_interactive_input, _safe_console_input


def _clean_text_value(value: Optional[Any]) -> str:
    """清理输入值中的 BOM、首尾空白和非字符串类型。"""
    if value is None:
        return ""
    return str(value).replace("﻿", "").strip()


def _sanitize_base_url(value: Optional[Any]) -> Optional[str]:
    """校验并标准化 Base URL，不合法时返回 None。"""
    cleaned = _clean_text_value(value)
    if not cleaned or any(char in cleaned for char in ("\r", "\n", "\t")):
        return None

    parsed = urlparse(cleaned)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None

    return cleaned


def _resolve_base_url_default(
    selected_protocol: Dict[str, Any],
    candidate: Optional[Any],
) -> str:
    """解析可用的默认 Base URL，坏数据回退到协议默认值。"""
    return _sanitize_base_url(candidate) or selected_protocol["default_base_url"]


def _get_protocol_option(model_type: Optional[str]) -> Dict[str, Any]:
    """获取指定模型类型的默认配置。"""
    defaults = _protocol_defaults()
    if model_type and model_type in defaults:
        return dict(defaults[model_type])
    return dict(defaults["ollama"])


def _get_protocol_default_index(model_type: Optional[str]) -> int:
    """将模型类型转换为菜单默认选中位置。"""
    for index, option in enumerate(_protocol_options()):
        if option["value"] == model_type:
            return index
    return 3


def _format_context_window_value(value: Optional[int]) -> str:
    """格式化上下文窗口值。"""
    parsed = parse_context_window(value)
    return f"{parsed:,} tokens" if parsed else tr("common.not_set")


def _detect_context_window_from_model(
    model_type: str,
    model_name: str,
    model_config: Dict[str, Any],
) -> Optional[int]:
    """仅通过模型/API 明确返回的信息探测上下文窗口，不做名称默认兜底。"""
    try:
        return get_model_provider_registry().detect_context_window(
            model_type,
            model_name=model_name,
            **model_config,
        )
    except Exception:
        return None


def _prompt_context_window(model_name: str) -> int:
    """要求用户输入准确的模型上下文窗口。"""
    print_warning(tr("configure.context_window_required", model=model_name))
    print_info(tr("configure.context_window_accuracy_hint"))
    print_info(tr("configure.context_window_format_hint"))

    while True:
        console.print(
            f"\n[{SayacodeColors.TEXT_DIM}]"
            f"{tr('configure.context_window_prompt')}[/]"
        )
        raw_value = _safe_console_input("  > ").strip()
        parsed = parse_context_window(raw_value)
        if parsed:
            return parsed
        print_warning(tr("configure.context_window_invalid"))


def _ensure_context_window_configured(
    model_type: str,
    model_name: str,
    model_config: Dict[str, Any],
    *,
    interactive_input: bool,
    announce_detection: bool = True,
) -> int:
    """确保模型配置里有准确上下文窗口；无法探测时要求用户输入。"""
    configured = _extract_context_window_from_config(model_config)
    if configured:
        return _store_context_window_in_config(model_config, configured)

    if announce_detection:
        print_status(tr("configure.context_window_detecting"))

    detected = _detect_context_window_from_model(model_type, model_name, model_config)
    if detected:
        detected = _store_context_window_in_config(model_config, detected)
        print_info(tr("connection.context_detected", context_window=f"{detected:,}"))
        return detected

    if interactive_input:
        entered = _prompt_context_window(model_name)
        entered = _store_context_window_in_config(model_config, entered)
        print_success(tr("configure.context_window_set", context_window=f"{entered:,}"))
        return entered

    raise RuntimeError(tr("configure.context_window_required_non_interactive"))


def _print_saved_profile_summary(profile_name: str, model_type: str, model_name: str, model_config: Dict[str, Any]) -> None:
    """展示当前加载的已保存模型 profile。"""
    protocol = _get_protocol_option(model_type)
    print_summary_card(
        tr("model.saved_profile_title"),
        {
            tr("model.profile"): profile_name,
            tr("model.protocol"): protocol["label"],
            tr("model.base_url"): model_config.get("base_url") or protocol["default_base_url"],
            "Model": model_name,
            tr("model.context_window"): _format_context_window_value(
                _extract_context_window_from_config(model_config)
            ),
            "API key": (
                tr("common.configured")
                if model_config.get("api_key")
                else tr("common.not_set")
            ),
        },
        subtitle=tr("model.saved_profile_subtitle"),
        footer=tr("model.footer"),
    )
    console.print()


def _parse_context_window_arg(args) -> Optional[int]:
    """解析命令行 --context-window。"""
    raw_value = getattr(args, "context_window", None)
    if not raw_value:
        return None

    parsed = parse_context_window(raw_value)
    if not parsed:
        raise ValueError(tr("configure.context_window_invalid"))
    return parsed


def resolve_launch_model_config(
    args,
    user_config,
    api_manager: APIConfigManager,
) -> tuple:
    """解析本次启动应使用的模型配置。"""
    cli_context_window = _parse_context_window_arg(args)

    def ensure_context_window(model_type: str, model_name: str, model_config: Dict[str, Any]) -> int:
        return _ensure_context_window_configured(
            model_type,
            model_name,
            model_config,
            interactive_input=_supports_interactive_input(),
        )

    resolver = ModelLaunchResolver(
        api_manager=api_manager,
        configure_model=configure_model,
        ensure_context_window=ensure_context_window,
        interactive_input=_supports_interactive_input(),
        on_profile_missing_credentials=lambda name: print_warning(
            tr("startup.profile_missing_credentials", name=name)
        ),
        on_saved_profile_summary=_print_saved_profile_summary,
        on_profile_saved=lambda name: print_success(tr("startup.profile_saved", name=name)),
        on_profile_not_saved=lambda: print_warning(tr("startup.profile_not_saved")),
    )
    result = resolver.resolve(
        LaunchModelOverrides(
            model_type=args.model_type,
            model_name=args.model_name,
            base_url=args.base_url,
            api_key=args.api_key,
            context_window=cli_context_window,
        )
    )
    return result.as_tuple()


def configure_model(
    default_model_type: Optional[str] = None,
    default_model_name: Optional[str] = None,
    default_base_url: Optional[str] = None,
    default_api_key: Optional[str] = None,
    default_context_window: Optional[Any] = None,
    lock_model_type: bool = False,
) -> tuple:
    """
    配置模型（简化版本）

    Returns:
        (model_type, model_name, model_config) 元组
    """
    console.print()
    print_banner(tr("configure.title"), tr("configure.subtitle"))

    if default_model_type and (lock_model_type or not _supports_interactive_input()):
        selected_protocol = _get_protocol_option(default_model_type)
    else:
        selected_protocol = select_model_protocol(
            default_index=_get_protocol_default_index(default_model_type)
        )

    model_type = selected_protocol["value"]
    model_name_default = default_model_name or selected_protocol["default_model_name"]
    base_url_default = _resolve_base_url_default(selected_protocol, default_base_url)
    api_key_env_name = selected_protocol.get("api_key_env")
    api_key_from_env = default_api_key or (os.environ.get(api_key_env_name, "") if api_key_env_name else "")

    model_config: Dict[str, Any] = {}
    interactive_input = _supports_interactive_input()

    if default_base_url and _sanitize_base_url(default_base_url) is None and interactive_input:
        print_warning(tr("configure.invalid_saved_base_url"))

    print_summary_card(
        tr("protocol.title"),
        {
            tr("protocol.selected"): selected_protocol["label"],
            tr("protocol.profile"): selected_protocol.get("description", tr("protocol.custom_access")),
            tr("protocol.default_base_url"): base_url_default or tr("common.empty"),
            tr("protocol.default_model"): model_name_default,
            tr("protocol.api_key_source"): (
                tr("protocol.environment_key_source", name=api_key_env_name)
                if api_key_from_env and api_key_env_name
                else tr("protocol.manual_input")
            ),
        },
        subtitle=tr("protocol.step1"),
    )

    if interactive_input:
        while True:
            console.print(
                f"\n[{SayacodeColors.TEXT_DIM}]"
                f"{tr('configure.base_url_prompt', value=base_url_default or tr('common.empty'))}[/]"
            )
            base_url_input = _safe_console_input("  > ", base_url_default)
            base_url = _sanitize_base_url(base_url_input) or (
                base_url_default if not _clean_text_value(base_url_input) else None
            )
            if base_url:
                break
            print_warning(tr("configure.base_url_invalid"))

        api_key_hint = (
            tr("configure.api_key_env_hint")
            if api_key_from_env
            else tr("configure.api_key_optional")
        )
        console.print(f"\n[{SayacodeColors.TEXT_DIM}]{tr('configure.api_key_prompt', hint=api_key_hint)}[/]")
        key_input = _clean_text_value(_safe_console_input("  > "))
        if key_input:
            model_config["api_key"] = key_input
        elif api_key_from_env:
            model_config["api_key"] = api_key_from_env

        console.print(
            f"\n[{SayacodeColors.TEXT_DIM}]"
            f"{tr('configure.model_name_prompt', value=model_name_default)}[/]"
        )
        model_name = _clean_text_value(_safe_console_input("  > ", model_name_default)) or model_name_default
    else:
        base_url = base_url_default
        if api_key_from_env:
            model_config["api_key"] = api_key_from_env
        model_name = model_name_default

    if base_url:
        model_config["base_url"] = base_url

    parsed_context_window = parse_context_window(default_context_window)
    if default_context_window and not parsed_context_window:
        raise ValueError(tr("configure.context_window_invalid"))

    if parsed_context_window:
        _store_context_window_in_config(model_config, parsed_context_window)
    else:
        _ensure_context_window_configured(
            model_type,
            model_name,
            model_config,
            interactive_input=interactive_input,
        )

    console.print()
    print_summary_card(
        tr("configure.ready_title"),
        {
            tr("model.protocol"): selected_protocol["label"],
            tr("model.base_url"): base_url or tr("common.empty"),
            "Model": model_name,
            tr("model.context_window"): _format_context_window_value(
                _extract_context_window_from_config(model_config)
            ),
            "API key": tr("configure.api_key_configured") if model_config.get("api_key") else tr("configure.api_key_not_set"),
        },
        subtitle=tr("protocol.step2"),
        footer=tr("configure.ready_footer"),
    )

    return model_type, model_name, model_config


def test_model_connection(model_type: str, model_name: str, model_config: dict) -> bool:
    """
    测试模型连接

    Args:
        model_type: 模型类型
        model_name: 模型名称
        model_config: 模型配置

    Returns:
        连接是否成功
    """
    print_status(tr("connection.testing", model_type=model_type, model_name=model_name))

    try:
        _ensure_context_window_configured(
            model_type,
            model_name,
            model_config,
            interactive_input=_supports_interactive_input(),
            announce_detection=False,
        )

        model = get_model_provider_registry().create_model(
            model_type,
            model_name=model_name,
            **model_config
        )

        # 尝试简单调用
        test_messages = [{"role": "user", "content": "Hi"}]
        model.chat(test_messages)

        print_success(tr("connection.connected"))

        detected = model.detect_context_window() or _extract_context_window_from_config(model_config)
        if detected:
            _store_context_window_in_config(model_config, detected)
            print_info(tr("connection.context_detected", context_window=f"{detected:,}"))

        return True

    except Exception as e:
        print_error(tr("connection.failed", error=str(e)))
        return False

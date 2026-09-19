"""兼容 shim：主题已搬至 lib.cli.theme，原路径仅做转发。"""

from lib.cli.theme import (
    SAYACODE_LOGO as SAYACODE_LOGO,
    SayacodeColors as SayacodeColors,
    _assemble as _assemble,
    _build_plan_table as _build_plan_table,
    _build_summary_panel as _build_summary_panel,
    _plan_status_cell as _plan_status_cell,
    _safe_markdown as _safe_markdown,
    _safe_text as _safe_text,
    agent_status_text as agent_status_text,
    confirm_action as confirm_action,
    console as console,
    format_token_hint as format_token_hint,
    plain_console as plain_console,
    print_agent_message as print_agent_message,
    print_banner as print_banner,
    print_delegate_notice as print_delegate_notice,
    print_divider as print_divider,
    print_error as print_error,
    print_farewell as print_farewell,
    print_feature_guide as print_feature_guide,
    print_help as print_help,
    print_info as print_info,
    print_logo as print_logo,
    print_message_header as print_message_header,
    print_plan_table as print_plan_table,
    print_split_summary_cards as print_split_summary_cards,
    print_status as print_status,
    print_status_info as print_status_info,
    print_success as print_success,
    print_summary_card as print_summary_card,
    print_thinking as print_thinking,
    print_tool_call as print_tool_call,
    print_user_message as print_user_message,
    print_warning as print_warning,
    print_welcome as print_welcome,
    render_streaming_agent_message as render_streaming_agent_message,
    reset_logo_state as reset_logo_state,
    short_prompt as short_prompt,
)

__all__ = [
    'console', 'plain_console', 'SayacodeColors', 'SAYACODE_LOGO',
    'reset_logo_state',
    '_assemble', '_safe_text', '_safe_markdown',
    'print_logo', 'print_welcome', 'print_farewell',
    'print_summary_card', 'print_split_summary_cards', 'print_message_header',
    'print_help', 'short_prompt', 'print_status', 'print_success',
    'print_warning', 'print_error', 'print_info', 'print_divider',
    'print_banner', 'confirm_action', 'print_user_message',
    'print_agent_message', 'render_streaming_agent_message',
    'print_tool_call', 'print_thinking', 'print_status_info',
    'print_feature_guide', 'format_token_hint', 'agent_status_text',
    '_plan_status_cell', '_build_plan_table', 'print_plan_table',
    'print_delegate_notice',
]


def __getattr__(name: str):
    """未显式列出的属性一律转发到新模块，保证旧引用继续生效。"""
    import importlib

    return getattr(importlib.import_module("lib.cli.theme"), name)

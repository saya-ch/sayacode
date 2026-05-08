"""
系统提示词模块

提供各种风格的系统提示词。
"""

from .system_prompt import (
    SUPPORTED_PROMPT_STYLES,
    normalize_prompt_style,
    prompt_style_label,
    list_prompt_styles,
    get_system_prompt,
    get_tsundere_prompt,
    get_concise_prompt,
    get_genki_prompt,
    get_mesugaki_prompt,
    get_onee_san_prompt,
    get_idol_prompt,
    get_catgirl_prompt,
    get_mukuchi_prompt,
    get_prompt_by_style,
)

__all__ = [
    'SUPPORTED_PROMPT_STYLES',
    'normalize_prompt_style',
    'prompt_style_label',
    'list_prompt_styles',
    'get_system_prompt',
    'get_tsundere_prompt',
    'get_concise_prompt',
    'get_genki_prompt',
    'get_mesugaki_prompt',
    'get_onee_san_prompt',
    'get_idol_prompt',
    'get_catgirl_prompt',
    'get_mukuchi_prompt',
    'get_prompt_by_style',
]

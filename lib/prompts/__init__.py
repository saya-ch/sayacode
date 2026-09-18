"""系统提示词包入口。

职责是汇出 prompt style 调用链：`system_prompt` 负责按行为层与人格层
组装完整提示词，`reminders` 按运行时状态追加提醒。核心函数为
`get_system_prompt` / `get_prompt_by_style` / `normalize_prompt_style`，
由 Agent 运行时按工作模式调用。
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

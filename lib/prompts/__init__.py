"""系统提示词包入口。

职责是汇出 prompt style 调用链：`system_prompt` 负责按行为层与人格层
组装完整提示词，条件 system 扩展（原 reminders 字符串注入）改走
dynamic_prompt，由 `build_conditional_system_extras` 按运行时状态
推导、经 SayaPromptMiddleware 挂载。核心函数为
`get_system_prompt` / `get_prompt_by_style` / `normalize_prompt_style` /
`build_conditional_system_extras` / `build_dynamic_context_section`，
由 Agent 运行时按工作模式调用。
"""

from .system_prompt import (
    SUPPORTED_PROMPT_STYLES,
    normalize_prompt_style,
    prompt_style_label,
    list_prompt_styles,
    build_conditional_system_extras,
    build_dynamic_context_section,
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
    'build_conditional_system_extras',
    'build_dynamic_context_section',
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

"""SAYACODE CLI 包。

子模块说明：
parser 是参数解析，configure 是模型配置，
workspace 是工作区解析，permissions 是权限确认，main 是主入口。

包根用惰性导出：import 子模块时不再连带拖入 configure
等重模块，避免和顶层垫片形成循环导入。
"""

_LAZY = {
    "_get_protocol_option": "lib.cli.configure",
    "CLI_VERSION": "lib.cli.parser",
    "BUILTIN_COMMANDS": "lib.cli.parser",
    "PROTOCOL_DEFAULTS": "lib.cli.parser",
    "PROTOCOL_OPTIONS": "lib.cli.parser",
    "USER_VISIBLE_MODEL_TYPES": "lib.cli.parser",
    "LocalizedHelpFormatter": "lib.cli.parser",
    "build_cli_parser": "lib.cli.parser",
    "main": "lib.cli.main",
}


def __getattr__(name: str):
    """首次访问时才解析对应子模块。"""
    if name in _LAZY:
        import importlib

        module = importlib.import_module(_LAZY[name])
        value = getattr(module, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "CLI_VERSION",
    "BUILTIN_COMMANDS",
    "PROTOCOL_DEFAULTS",
    "PROTOCOL_OPTIONS",
    "USER_VISIBLE_MODEL_TYPES",
    "LocalizedHelpFormatter",
    "_get_protocol_option",
    "build_cli_parser",
    "main",
]

import warnings

from lib.core.modes import apply_agent_mode_permissions
from lib.core.symbols import SymbolIndex, render_symbols, summarize_symbol_index
from lib.tools import configure_tool_workspace, find_symbol, list_symbols


def test_symbol_index_finds_python_and_javascript_symbols(tmp_path):
    (tmp_path / "service.py").write_text(
        "\n".join(
            [
                "class Service:",
                "    def run(self, value):",
                "        return value",
                "",
                "async def load_items(path):",
                "    return []",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "ui.js").write_text(
        "\n".join(
            [
                "export class Widget {}",
                "export function renderApp(root) { return root }",
                "const helper = (value) => value",
            ]
        ),
        encoding="utf-8",
    )

    index = SymbolIndex(tmp_path)
    symbols = index.scan()
    names = {symbol.qualified_name for symbol in symbols}

    assert "Service" in names
    assert "Service.run" in names
    assert "load_items" in names
    assert "Widget" in names
    assert "renderApp" in names
    assert "helper" in names
    assert index.find("Service")[0].path == "service.py"


def test_symbol_rendering_and_summary(tmp_path):
    (tmp_path / "module.py").write_text(
        "def build(value):\n    return value\n",
        encoding="utf-8",
    )

    symbols = SymbolIndex(tmp_path).scan()
    rendered = render_symbols(symbols, title="Test Symbols")
    summary = summarize_symbol_index(tmp_path)

    assert "Test Symbols" in rendered
    assert "function build" in rendered
    assert "module.py:1" in rendered
    assert "Total: 1" in summary


def test_symbol_index_suppresses_invalid_escape_warnings(tmp_path):
    (tmp_path / "regex_module.py").write_text(
        'pattern = "\\d+"\n\n'
        "def build(value):\n"
        "    return value\n",
        encoding="utf-8",
    )

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        symbols = SymbolIndex(tmp_path).scan()

    assert not [
        warning for warning in caught
        if issubclass(warning.category, (SyntaxWarning, DeprecationWarning))
    ]
    assert any(symbol.name == "build" for symbol in symbols)


def test_symbol_tools_use_configured_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "home"))
    (tmp_path / "module.py").write_text(
        "class Service:\n    pass\n",
        encoding="utf-8",
    )
    apply_agent_mode_permissions("build")
    configure_tool_workspace(str(tmp_path))

    listed = list_symbols.invoke({"root_dir": ".", "query": "Service"})
    found = find_symbol.invoke({"name": "Service", "root_dir": "."})

    assert "Service" in listed
    assert "module.py:1" in found

import ast
from pathlib import Path

from lib.i18n import TRANSLATIONS


ROOT = Path(__file__).resolve().parents[1]


def _literal_translation_keys() -> set[str]:
    keys: set[str] = set()
    for path in (ROOT / "lib").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue

            func = node.func
            func_name = ""
            if isinstance(func, ast.Name):
                func_name = func.id
            elif isinstance(func, ast.Attribute):
                func_name = func.attr

            first_arg = node.args[0]
            if func_name == "tr" and isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str):
                keys.add(first_arg.value)
    return keys


def test_translation_tables_have_matching_keys():
    english_keys = set(TRANSLATIONS["en"])
    chinese_keys = set(TRANSLATIONS["zh-CN"])

    assert sorted(english_keys - chinese_keys) == []
    assert sorted(chinese_keys - english_keys) == []


def test_literal_translation_keys_exist_in_all_languages():
    literal_keys = _literal_translation_keys()
    missing = {
        language: sorted(literal_keys - set(entries))
        for language, entries in TRANSLATIONS.items()
    }

    assert missing == {language: [] for language in TRANSLATIONS}


def test_user_visible_status_printers_do_not_use_literal_strings():
    guarded_roots = [
        ROOT / "lib" / "cli.py",
        ROOT / "lib" / "commands",
        ROOT / "lib" / "runtime",
        ROOT / "lib" / "api_config",
    ]
    printer_names = {"print_error", "print_warning", "print_info", "print_success"}
    offenders = []

    for root in guarded_roots:
        paths = [root] if root.is_file() else list(root.rglob("*.py"))
        for path in paths:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not node.args:
                    continue
                func = node.func
                func_name = func.id if isinstance(func, ast.Name) else ""
                if func_name not in printer_names:
                    continue
                if isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}:{func_name}")

    assert offenders == []


def test_cli_argparse_help_does_not_use_literal_strings():
    offenders = []

    for py_file in (ROOT / "lib" / "cli").glob("*.py"):
        tree = ast.parse(py_file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if keyword.arg in {"description", "epilog", "help"}:
                    if isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
                        offenders.append(f"lib/cli/{py_file.name}:{node.lineno}:{keyword.arg}")

    assert offenders == []

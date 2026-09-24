"""检查 wheel 和源码包中的 Web 界面是否可直接使用。"""

from __future__ import annotations

import sys
import tarfile
import zipfile
from collections.abc import Callable
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
_OLD_CLI = (
    "interactive.py", "commands.py", "completion.py", "display.py", "help.py",
    "input.py", "selection.py", "theme.py", "turn.py", "result_views.py",
    "model_setup.py", "approvals.py", "memory.py", "reviewer.py", "team.py",
)


class _AssetReferences(HTMLParser):
    """收集首页实际引用的本地资源，而不是只检查文件数量。"""

    def __init__(self) -> None:
        super().__init__()
        self.paths: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        reference = values.get("src") if tag == "script" else values.get("href")
        if not reference or tag not in {"script", "link"}:
            return
        parsed = urlsplit(reference)
        if parsed.scheme or parsed.netloc or not parsed.path or parsed.path == "/":
            return
        path = PurePosixPath(unquote(parsed.path).lstrip("/"))
        if ".." in path.parts:
            raise ValueError(f"首页引用了包目录外的资源：{reference}")
        self.paths.append(path.as_posix())


def _check_archive(path: Path) -> None:
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            _check_contents(path, names, archive.read)
        return
    if path.name.endswith(".tar.gz"):
        with tarfile.open(path, "r:gz") as archive:
            names = {member.name for member in archive.getmembers() if member.isfile()}

            def read(name: str) -> bytes:
                member = archive.extractfile(name)
                if member is None:
                    raise ValueError(f"无法读取 {name}")
                return member.read()

            _check_contents(path, names, read)
        return
    raise ValueError(f"未知的分发格式：{path}")


def _check_contents(path: Path, names: set[str], read: Callable[[str], bytes]) -> None:
    if any("/node_modules/" in name for name in names):
        raise ValueError(f"{path} 意外包含前端依赖目录")
    legacy = [name for name in names if any(name.endswith(f"sayacode/cli/{old}") for old in _OLD_CLI)]
    if legacy:
        raise ValueError(f"{path} 仍包含旧终端模块：{', '.join(sorted(legacy))}")
    indexes = [name for name in names if name.endswith("sayacode/web/static/index.html")]
    if len(indexes) != 1:
        raise ValueError(f"{path} 缺少唯一的 Web 首页")
    index_name = indexes[0]
    prefix = index_name[: -len("index.html")]
    parser = _AssetReferences()
    parser.feed(read(index_name).decode("utf-8"))
    if not any(name.endswith(".js") for name in parser.paths):
        raise ValueError(f"{path} 的首页没有引用 JavaScript 产物")
    missing = [name for name in parser.paths if prefix + name not in names]
    if missing:
        raise ValueError(f"{path} 的首页引用了缺失资源：{', '.join(missing)}")
    static = ROOT / "src" / "sayacode" / "web" / "static"
    if static.is_dir():
        expected = {file.relative_to(static).as_posix() for file in static.rglob("*") if file.is_file()}
        packaged = {name.removeprefix(prefix) for name in names if name.startswith(prefix)}
        if expected != packaged:
            absent = sorted(expected - packaged)
            extra = sorted(packaged - expected)
            raise ValueError(f"{path} 的资源清单与源码不一致；缺少 {absent}，多出 {extra}")
    if path.name.endswith(".tar.gz"):
        if not any(name.endswith("frontend/package-lock.json") for name in names):
            raise ValueError(f"{path} 缺少前端锁文件")
        if not any(name.endswith("/uv.lock") for name in names):
            raise ValueError(f"{path} 缺少 Python 依赖锁文件")
    print(f"Web 资源检查通过：{path}")


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("用法：python scripts/check_web_distribution.py <dist 目录>")
    directory = Path(sys.argv[1])
    archives = sorted((*directory.glob("sayacode-*.whl"), *directory.glob("sayacode-*.tar.gz")))
    if not archives:
        raise SystemExit(f"找不到 SAYACODE 安装包：{directory}")
    for path in archives:
        _check_archive(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

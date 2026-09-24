"""从已安装的 wheel 启动本机 WebUI 并请求实际 HTTP 资源。"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from html.parser import HTMLParser
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPCookieProcessor, build_opener


class _Scripts(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.paths: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script":
            path = dict(attrs).get("src")
            if path:
                self.paths.append(path)


def _unused_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("用法：python scripts/smoke_installed_web.py <已安装 wheel 的 Python>")
    interpreter = Path(sys.argv[1]).resolve()
    if not interpreter.is_file():
        raise SystemExit(f"找不到安装环境的 Python：{interpreter}")
    with tempfile.TemporaryDirectory(prefix="sayacode-web-smoke-") as temporary:
        root = Path(temporary)
        workspace = root / "workspace"
        workspace.mkdir()
        port = _unused_port()
        origin = f"http://127.0.0.1:{port}"
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        env["SAYACODE_HOME"] = str(root / "home")
        env["PYTHONIOENCODING"] = "utf-8"
        log_path = root / "server.log"
        with log_path.open("w", encoding="utf-8") as log:
            process = subprocess.Popen(
                [
                    str(interpreter),
                    "-m",
                    "sayacode",
                    "--workspace",
                    str(workspace),
                    "--no-open",
                    "--port",
                    str(port),
                ],
                cwd=root,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=(
                    subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
                ),
            )
            try:
                opener = build_opener(HTTPCookieProcessor(CookieJar()))
                deadline = time.monotonic() + 40
                while True:
                    if process.poll() is not None:
                        raise RuntimeError(f"Web 服务提前退出，退出码 {process.returncode}")
                    try:
                        with opener.open(origin + "/api/health", timeout=2) as response:
                            health = json.load(response)
                        break
                    except (OSError, URLError):
                        if time.monotonic() >= deadline:
                            raise TimeoutError("Web 服务未在 40 秒内启动") from None
                        time.sleep(0.25)
                if health.get("ok") is not True:
                    raise ValueError(f"健康检查未通过：{health}")
                with opener.open(origin + "/", timeout=5) as response:
                    if response.headers.get("Cache-Control") != "no-cache":
                        raise ValueError("Web 首页必须在更新时重新验证")
                    html = response.read().decode("utf-8")
                scripts = _Scripts()
                scripts.feed(html)
                if not scripts.paths:
                    raise ValueError("安装后的首页没有 JavaScript 资源")
                resource = urljoin(origin + "/", scripts.paths[0])
                if urlsplit(resource).netloc != urlsplit(origin).netloc:
                    raise ValueError("首页 JavaScript 地址不属于本机服务")
                with opener.open(resource, timeout=5) as response:
                    if "immutable" not in response.headers.get("Cache-Control", ""):
                        raise ValueError("哈希资源缺少长期缓存策略")
                    if not response.read(1):
                        raise ValueError("安装后的 JavaScript 资源为空")
                print(f"wheel Web HTTP 冒烟通过：{health['version']}")
            finally:
                if process.poll() is None:
                    process.send_signal(
                        signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT
                    )
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        # 错误时保留的是具体异常；日志可用本地重跑查看，不回显配置或凭据。
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

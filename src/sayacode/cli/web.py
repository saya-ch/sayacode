"""启动随命令行进程存在的本机 Web 服务。"""

from __future__ import annotations

import asyncio
import socket
import sys
import webbrowser
from pathlib import Path
from urllib.parse import quote

import uvicorn

from ..host.application import WebHost
from ..web.app import create_web_app


async def serve_web(workspace: Path, *, port: int = 0, open_browser: bool = True) -> int:
    """Web 页面只观察宿主持有的运行任务；浏览器断开不会停止 Agent。"""
    host = await WebHost.open(workspace)
    try:
        static = Path(__file__).resolve().parents[1] / "web" / "static"
        app = create_web_app(host, static)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", port))
            listener.listen(128)
            listener.setblocking(False)
            actual_port = int(listener.getsockname()[1])
            server = uvicorn.Server(
                uvicorn.Config(
                    app,
                    host="127.0.0.1",
                    port=actual_port,
                    access_log=False,
                    log_level="warning",
                    lifespan="off",
                )
            )
            serving = asyncio.create_task(server.serve(sockets=[listener]))
            try:
                async with asyncio.timeout(15):
                    while not server.started:
                        if serving.done():
                            await serving
                            raise RuntimeError("Web 服务启动前已退出")
                        await asyncio.sleep(0.05)
                url = f"http://127.0.0.1:{actual_port}/#token={quote(app.state.launch_token)}"
                print(f"SAYACODE WebUI: {url}", flush=True)
                if open_browser:
                    try:
                        opened = await asyncio.to_thread(webbrowser.open, url)
                    except Exception:
                        opened = False
                    if not opened:
                        print("浏览器未自动打开，请使用上方地址。", file=sys.stderr)
                await serving
                return 0
            finally:
                server.should_exit = True
                if not serving.done():
                    try:
                        await asyncio.wait_for(serving, timeout=10)
                    except TimeoutError:
                        serving.cancel()
                        await asyncio.gather(serving, return_exceptions=True)
    finally:
        await host.aclose()


__all__ = ["serve_web"]

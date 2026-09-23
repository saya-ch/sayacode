"""真实 CLI 进程、官方模型适配器和 SQLite Store 的跨进程记忆验收。"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from sayacode.config import Config, ConfigRepository, MemoryConfig, Profile


def _completion(message: dict[str, Any], finish_reason: str = "stop") -> bytes:
    return json.dumps(
        {
            "id": "chatcmpl-memory-test",
            "object": "chat.completion",
            "created": 1,
            "model": "local-contract",
            "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 7, "total_tokens": 19},
        }
    ).encode("utf-8")


def test_real_cli_process_learns_and_reuses_memory(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "state"
    requests: list[dict[str, Any]] = []
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            assert self.path == "/v1/chat/completions"
            length = int(self.headers["Content-Length"])
            request = json.loads(self.rfile.read(length))
            with lock:
                requests.append(request)
            text = json.dumps(request.get("messages", []), ensure_ascii=False)
            tools = {item.get("function", {}).get("name") for item in request.get("tools", [])}
            source = re.search(r"user-[a-f0-9]{32}", text)
            if "MemoryContent" in tools and "跨项目适用的用户长期偏好" in text and source:
                message = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "memory-result-1",
                            "type": "function",
                            "function": {
                                "name": "MemoryContent",
                                "arguments": json.dumps(
                                    {
                                        "subject": "注释语言",
                                        "text": "以后代码注释使用中文",
                                        "scope_kind": "user",
                                        "evidence_ids": [source.group()],
                                    },
                                    ensure_ascii=False,
                                ),
                            },
                        }
                    ],
                }
                payload = _completion(message, "tool_calls")
            else:
                payload = _completion({"role": "assistant", "content": "完成"})
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        config = Config(
            default_profile="local",
            profiles={
                "local": Profile(
                    name="local",
                    protocol="openai_chat_completions",
                    base_url=f"http://127.0.0.1:{server.server_port}/v1",
                    api_key="local-test-key",
                    model_id="local-contract",
                    context_length=8192,
                    max_output_tokens=512,
                    file_search=False,
                    summary_trigger_ratio=None,
                    summary_trigger_tokens=None,
                    tool_selector_max_tools=None,
                    model_retries=0,
                    tool_retries=0,
                )
            },
            memory=MemoryConfig(
                enabled=True, learn="auto", idle_seconds=0, headless_timeout_seconds=15
            ),
        )
        repository = ConfigRepository(home)
        asyncio.run(repository.save(config))
        environment = os.environ.copy()
        environment["SAYACODE_HOME"] = str(home)

        def run_cli(prompt: str) -> list[dict[str, Any]]:
            process = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "sayacode",
                    "--workspace",
                    str(workspace),
                    "--new-session",
                    "-p",
                    prompt,
                    "--output-format",
                    "jsonl",
                    "--no-stream",
                ],
                env=environment,
                capture_output=True,
                text=True,
                timeout=35,
                check=False,
            )
            assert process.returncode == 0, process.stderr or process.stdout
            return [json.loads(line) for line in process.stdout.splitlines()]

        first = run_cli("以后代码注释使用中文")
        assert first[-1]["type"] == "run.completed"
        assert any(event["type"] == "memory.updated" for event in first)
        config.memory.learn = "off"
        asyncio.run(repository.save(config))
        second = run_cli("实现一个函数")
        assert second[-1]["type"] == "run.completed"
        with lock:
            assert any(
                message.get("role") == "tool"
                and "以后代码注释使用中文" in str(message.get("content"))
                for request in requests
                for message in request.get("messages", [])
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

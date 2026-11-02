"""Docker 后端的参数映射与 CLI 回退测试。"""

from __future__ import annotations

import subprocess
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from evals.docker_backend import DockerBackend


class FakeImage:
    def __init__(self, image_id: str = "sha256:abc") -> None:
        self.id = image_id


class FakeContainer:
    def __init__(self, name: str) -> None:
        self.name = name
        self.exec_calls: list[Any] = []
        self.stopped: list[int] = []
        self.removed: list[bool] = []
        self.exec_result: tuple[int, bytes] = (0, b"ok")

    def exec_run(self, cmd: Any) -> tuple[int, bytes]:
        self.exec_calls.append(cmd)
        return self.exec_result

    def stop(self, timeout: int = 10) -> None:
        self.stopped.append(timeout)

    def remove(self, force: bool = False) -> None:
        self.removed.append(force)


class FakeImages:
    def __init__(self) -> None:
        self.build_kwargs: dict[str, Any] = {}
        self.get_tags: list[str] = []

    def build(self, **kwargs: Any) -> tuple[FakeImage, list[str]]:
        self.build_kwargs = kwargs
        return FakeImage("sha256:built"), []

    def get(self, tag: str) -> FakeImage:
        self.get_tags.append(tag)
        return FakeImage(f"id-{tag}")


class FakeContainers:
    def __init__(self) -> None:
        self.run_kwargs: dict[str, Any] = {}
        self.containers: dict[str, FakeContainer] = {}

    def run(self, image: str, **kwargs: Any) -> FakeContainer:
        self.run_kwargs = {"image": image, **kwargs}
        container = FakeContainer(kwargs.get("name", "x"))
        self.containers[container.name] = container
        return container

    def get(self, name: str) -> FakeContainer:
        if name not in self.containers:
            self.containers[name] = FakeContainer(name)
        return self.containers[name]


class FakeClient:
    def __init__(self) -> None:
        self.images = FakeImages()
        self.containers = FakeContainers()
        self.pinged = 0

    def ping(self) -> bool:
        self.pinged += 1
        return True

    def version(self) -> dict[str, str]:
        return {"Version": "29.7.2"}


def install_fake_docker(monkeypatch: pytest.MonkeyPatch) -> FakeClient:
    client = FakeClient()
    monkeypatch.setitem(sys.modules, "docker", types.SimpleNamespace(from_env=lambda: client))
    return client


def runner_recorder(
    state: dict[str, Any],
    *,
    stdout: str = "",
) -> Any:
    def runner(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        state.setdefault("calls", []).append((list(args), kwargs))
        return subprocess.CompletedProcess(args, 0, stdout, "")

    return runner


def test_sdk_mode_builds_and_runs_with_mapped_arguments(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = install_fake_docker(monkeypatch)
    backend = DockerBackend()
    assert backend.mode == "sdk"
    assert backend.info() == "29.7.2"

    (tmp_path / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    image_id = backend.build(tmp_path, tmp_path / "Dockerfile", "tag:1", {"k": "v"})
    assert image_id == "sha256:built"
    assert client.images.build_kwargs["dockerfile"] == "Dockerfile"
    assert client.images.build_kwargs["labels"] == {"k": "v"}
    assert client.images.build_kwargs["rm"] is True

    backend.run_detached(
        name="c1",
        image="img",
        mounts=[("/h/w", "/workspace")],
        env={"HOME": "/h"},
        cpus=2,
        memory="2g",
        pids_limit=256,
        cap_drop=["ALL"],
        security_opt=["no-new-privileges"],
        tmpfs={"/tmp": "rw,nosuid,size=256m"},
        read_only=True,
        add_host={"a": "1.2.3.4"},
    )
    kwargs = client.containers.run_kwargs
    assert kwargs["image"] == "img"
    assert kwargs["detach"] is True
    assert kwargs["volumes"] == {"/h/w": {"bind": "/workspace", "mode": "rw"}}
    assert kwargs["environment"] == {"HOME": "/h"}
    assert kwargs["nano_cpus"] == 2_000_000_000
    assert kwargs["mem_limit"] == "2g"
    assert kwargs["pids_limit"] == 256
    assert kwargs["cap_drop"] == ["ALL"]
    assert kwargs["security_opt"] == ["no-new-privileges"]
    assert kwargs["tmpfs"] == {"/tmp": "rw,nosuid,size=256m"}
    assert kwargs["read_only"] is True
    assert kwargs["extra_hosts"] == {"a": "1.2.3.4"}


def test_sdk_exec_pipes_stdin_through_base64(monkeypatch: pytest.MonkeyPatch) -> None:
    client = install_fake_docker(monkeypatch)
    backend = DockerBackend()
    assert backend.exec("c1", ["python", "-"], input_text="print(1)") == (0, "ok")
    command = client.containers.containers["c1"].exec_calls[0]
    assert command[0] == "sh"
    assert "base64 -d" in command[2]
    assert "python -" in command[2]


def test_sdk_stop_and_remove_are_tolerant_of_missing_containers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_docker(monkeypatch)
    backend = DockerBackend()

    class Missing:
        def get(self, name: str) -> Any:
            raise RuntimeError("no such container")

    backend.client.containers = Missing()
    assert backend.stop("gone") is False
    assert backend.remove("gone", force=True) is False


def test_sdk_exec_timeout_raises_subprocess_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_docker(monkeypatch)
    backend = DockerBackend()

    class Slow:
        def exec_run(self, cmd: Any) -> tuple[int, bytes]:
            import time

            time.sleep(0.2)
            return 0, b"late"

    backend.client.containers.get = lambda name: Slow()  # type: ignore[assignment]
    with pytest.raises(subprocess.TimeoutExpired):
        backend.exec("c1", ["sleep"], timeout=0.01)


def test_missing_docker_sdk_falls_back_to_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "docker", None)
    state: dict[str, Any] = {}
    backend = DockerBackend(runner=runner_recorder(state, stdout="29.7.2\n"))
    assert backend.mode == "cli"
    assert backend.info() == "29.7.2"
    assert state["calls"][0][0] == ["docker", "info", "--format", "{{.ServerVersion}}"]


def test_sdk_connection_failure_falls_back_to_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail() -> Any:
        raise RuntimeError("permission denied npipe")

    monkeypatch.setitem(sys.modules, "docker", types.SimpleNamespace(from_env=fail))
    backend = DockerBackend(runner=lambda args, **kwargs: subprocess.CompletedProcess(args, 0, "", ""))
    assert backend.mode == "cli"


def test_cli_fallback_commands_preserve_previous_docker_flags(tmp_path: Path) -> None:
    state: dict[str, Any] = {}
    backend = DockerBackend(prefer_sdk=False, runner=runner_recorder(state, stdout="sha256:cli\n"))

    backend.build(tmp_path, tmp_path / "Dockerfile", "tag:1", {"sayacode.eval.source": "abc"})
    build_args = state["calls"][0][0]
    assert build_args == [
        "docker",
        "build",
        "-f",
        str(tmp_path / "Dockerfile"),
        "--label",
        "sayacode.eval.source=abc",
        "-t",
        "tag:1",
        str(tmp_path),
    ]
    assert state["calls"][1][0] == [
        "docker",
        "image",
        "inspect",
        "tag:1",
        "--format",
        "{{.Id}}",
    ]

    backend.run_detached(
        name="c1",
        image="img",
        mounts=[("/h/w", "/workspace"), ("/h/s", "/state")],
        env={"HOME": "/state/home"},
        tmpfs={"/tmp": "rw,nosuid,size=256m"},
        read_only=True,
    )
    run_args = state["calls"][2][0]
    assert run_args[:4] == ["docker", "run", "--detach", "--name"]
    assert run_args[4] == "c1"
    assert "--read-only" in run_args
    assert run_args[run_args.index("--cpus") + 1] == "2"
    assert run_args[run_args.index("--memory") + 1] == "2g"
    assert run_args[run_args.index("--pids-limit") + 1] == "256"
    assert "--mount" in run_args
    assert "type=bind,src=/h/w,dst=/workspace" in run_args
    assert run_args[run_args.index("--env") + 1] == "HOME=/state/home"
    assert run_args[-1] == "img"

    backend.exec("c1", ["git", "status"], timeout=5)
    assert state["calls"][3][0] == ["docker", "exec", "c1", "git", "status"]
    backend.exec("c1", ["python", "-"], input_text="print(1)", timeout=5)
    assert state["calls"][4][0] == ["docker", "exec", "-i", "c1", "python", "-"]
    assert state["calls"][4][1]["input_text"] == "print(1)"

    backend.stop_many(["a", "b"], timeout=2)
    assert state["calls"][5][0] == ["docker", "stop", "--timeout", "2", "a", "b"]
    backend.remove("c1", force=True)
    assert state["calls"][6][0] == ["docker", "rm", "--force", "c1"]

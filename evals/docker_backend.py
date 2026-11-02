"""Docker 后端：优先 Docker SDK，SDK 不可用或连不上时回退命令行。

DSH 沙箱里 Python 子进程访问 Windows named pipe 会被拒绝，而 Docker SDK 走
socket 仍可能可用；两侧都不可用时保留原来的 CLI 行为，便于本地终端直接运行。
所有方法只做参数映射，不解释业务语义。"""

from __future__ import annotations

import base64
import concurrent.futures
import shlex
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

CliRunner = Callable[..., subprocess.CompletedProcess[str]]


def _default_cli_runner(
    args: Sequence[str],
    *,
    input_text: str | None = None,
    timeout: float = 60,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """启动 docker CLI，失败语义与 run.command 保持一致。"""
    result = subprocess.run(
        list(args),
        input=input_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if check and result.returncode:
        raise RuntimeError(
            f"命令失败：{args[0]}，退出码 {result.returncode}：{result.stderr[-2000:]}"
        )
    return result


class DockerBackend:
    """统一封装 docker 调用：默认 SDK，连接失败回退 CLI。"""

    def __init__(
        self,
        *,
        prefer_sdk: bool = True,
        runner: CliRunner | None = None,
    ) -> None:
        self._runner: CliRunner = runner or _default_cli_runner
        self.client: Any = None
        self.mode = "cli"
        if prefer_sdk:
            self._try_connect()

    def _try_connect(self) -> None:
        """尝试建立 SDK 客户端；任何失败都静默回退 CLI。"""
        try:
            import docker

            client = docker.from_env()
            client.ping()
        except Exception:
            self.client = None
            self.mode = "cli"
            return
        self.client = client
        self.mode = "sdk"

    # --- 查询 -------------------------------------------------------------
    def info(self) -> str:
        if self.mode == "sdk":
            version = self.client.version() or {}
            return str(version.get("Version", ""))
        result = self._runner(["docker", "info", "--format", "{{.ServerVersion}}"])
        return result.stdout.strip()

    def inspect_image(self, tag: str) -> str:
        if self.mode == "sdk":
            return str(self.client.images.get(tag).id)
        result = self._runner(["docker", "image", "inspect", tag, "--format", "{{.Id}}"])
        return result.stdout.strip()

    # --- 构建与运行 -------------------------------------------------------
    def build(
        self,
        context: Path | str,
        dockerfile: Path | str,
        tag: str,
        labels: dict[str, str] | None = None,
    ) -> str:
        labels = labels or {}
        if self.mode == "sdk":
            image, _logs = self.client.images.build(
                path=str(context),
                dockerfile=self._relative_dockerfile(context, dockerfile),
                tag=tag,
                labels=labels,
                rm=True,
            )
            return str(image.id)
        args = ["docker", "build", "-f", str(dockerfile)]
        for key, value in labels.items():
            args.extend(["--label", f"{key}={value}"])
        args.extend(["-t", tag, str(context)])
        self._runner(args, check=True)
        return self.inspect_image(tag)

    @staticmethod
    def _relative_dockerfile(context: Path | str, dockerfile: Path | str) -> str:
        try:
            return Path(dockerfile).resolve().relative_to(Path(context).resolve()).as_posix()
        except ValueError:
            return str(dockerfile)

    def run_detached(
        self,
        *,
        name: str,
        image: str,
        mounts: Sequence[tuple[str, str]],
        env: dict[str, str],
        cpus: float = 2,
        memory: str = "2g",
        pids_limit: int = 256,
        cap_drop: Sequence[str] = ("ALL",),
        security_opt: Sequence[str] = ("no-new-privileges",),
        tmpfs: dict[str, str] | None = None,
        read_only: bool = True,
        add_host: dict[str, str] | None = None,
    ) -> None:
        tmpfs = dict(tmpfs or {})
        if self.mode == "sdk":
            self.client.containers.run(
                image,
                name=name,
                detach=True,
                volumes={source: {"bind": target, "mode": "rw"} for source, target in mounts},
                environment=dict(env),
                nano_cpus=int(cpus * 1_000_000_000),
                mem_limit=memory,
                pids_limit=pids_limit,
                cap_drop=list(cap_drop),
                security_opt=list(security_opt),
                tmpfs=tmpfs,
                read_only=read_only,
                extra_hosts=dict(add_host or {}),
            )
            return
        args = ["docker", "run", "--detach", "--name", name]
        if read_only:
            args.append("--read-only")
        args.extend(["--cpus", str(cpus), "--memory", memory, "--pids-limit", str(pids_limit)])
        for value in cap_drop:
            args.extend(["--cap-drop", value])
        for value in security_opt:
            args.extend(["--security-opt", value])
        for target, options in tmpfs.items():
            args.extend(["--tmpfs", f"{target}:{options}"])
        for source, target in mounts:
            args.extend(["--mount", f"type=bind,src={source},dst={target}"])
        for key, value in env.items():
            args.extend(["--env", f"{key}={value}"])
        for host, address in (add_host or {}).items():
            args.extend(["--add-host", f"{host}:{address}"])
        args.append(image)
        self._runner(args, check=True)

    # --- 执行 -------------------------------------------------------------
    def exec(
        self,
        name: str,
        cmd: Sequence[str],
        *,
        timeout: float = 60,
        input_text: str | None = None,
    ) -> tuple[int, str]:
        if self.mode == "sdk":
            return self._sdk_exec(name, list(cmd), timeout=timeout, input_text=input_text)
        args = ["docker", "exec"]
        if input_text is not None:
            args.append("-i")
        args.extend([name, *cmd])
        result = self._runner(args, input_text=input_text, timeout=timeout, check=False)
        return result.returncode, result.stdout + result.stderr

    def _sdk_exec(
        self,
        name: str,
        cmd: list[str],
        *,
        timeout: float,
        input_text: str | None,
    ) -> tuple[int, str]:
        container = self.client.containers.get(name)
        actual = cmd
        if input_text is not None:
            # SDK 的 exec_run 不直接接收 stdin：把脚本 base64 后经 sh 管道送入。
            encoded = base64.b64encode(input_text.encode("utf-8")).decode("ascii")
            actual = ["sh", "-c", f"printf %s {encoded} | base64 -d | {shlex.join(cmd)}"]
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = pool.submit(container.exec_run, actual)
        try:
            result = future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            pool.shutdown(wait=False)
            raise subprocess.TimeoutExpired(list(cmd), timeout) from None
        pool.shutdown(wait=True)
        exit_code, output = result
        if isinstance(output, bytes):
            output = output.decode("utf-8", "replace")
        return int(exit_code or 0), str(output)

    # --- 停止与清理 -------------------------------------------------------
    def stop(self, name: str, *, timeout: int = 2) -> bool:
        if self.mode == "sdk":
            try:
                self.client.containers.get(name).stop(timeout=timeout)
            except Exception:
                return False
            return True
        result = self._runner(
            ["docker", "stop", "--timeout", str(timeout), name],
            timeout=max(20, timeout + 15),
            check=False,
        )
        return result.returncode == 0

    def stop_many(self, names: Sequence[str], *, timeout: int = 2) -> bool:
        names = list(names)
        if not names:
            return True
        if self.mode == "sdk":
            return all(self.stop(name, timeout=timeout) for name in names)
        result = self._runner(
            ["docker", "stop", "--timeout", str(timeout), *names],
            timeout=max(20, timeout + 15),
            check=False,
        )
        return result.returncode == 0

    def remove(self, name: str, *, force: bool = False) -> bool:
        if self.mode == "sdk":
            try:
                self.client.containers.get(name).remove(force=force)
            except Exception:
                return False
            return True
        args = ["docker", "rm"]
        if force:
            args.append("--force")
        args.append(name)
        result = self._runner(args, timeout=20, check=False)
        return result.returncode == 0


__all__ = ["DockerBackend"]

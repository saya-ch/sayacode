"""终端语言偏好与配置仓库的并发写入合同。"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from sayacode.cli.preferences import save_preferences
from sayacode.config import Config, ConfigRepository, MemoryConfig
from sayacode.prompts import PromptPreferences


@pytest.mark.asyncio
async def test_language_save_preserves_memory_and_other_config_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path))
    repository = ConfigRepository(tmp_path)
    await repository.save(
        Config(
            memory=MemoryConfig(enabled=True, use=False),
            preferences={"output_limit_bytes": "65536"},
        )
    )

    await asyncio.to_thread(save_preferences, PromptPreferences(language="zh"))

    current = await ConfigRepository(tmp_path).load()
    assert current.memory.enabled is True and current.memory.use is False
    assert current.preferences == {"output_limit_bytes": "65536", "language": "zh"}
    assert not (tmp_path / "config.json.tmp").exists()


@pytest.mark.asyncio
async def test_language_and_memory_writes_from_two_processes_merge(
    tmp_path: Path,
) -> None:
    await ConfigRepository(tmp_path).save(Config(memory=MemoryConfig(enabled=True)))
    script = r'''
import asyncio
import sys
from pathlib import Path
from sayacode.cli.preferences import save_preferences
from sayacode.config import ConfigRepository
from sayacode.prompts import PromptPreferences

async def main():
    root = Path(sys.argv[1])
    role = sys.argv[2]
    repository = ConfigRepository(root)
    config = await repository.load()
    if role == "memory":
        config.memory.use = False
    (root / f"ready-{role}").write_text("ready", encoding="ascii")
    for _ in range(500):
        if (root / "go").exists():
            break
        await asyncio.sleep(0.01)
    else:
        raise RuntimeError("timed out waiting for peer")
    if role == "memory":
        await repository.save(config)
    else:
        save_preferences(PromptPreferences(language="zh"))

asyncio.run(main())
'''
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
    environment["SAYACODE_HOME"] = str(tmp_path)
    processes = [
        await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            script,
            str(tmp_path),
            role,
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        for role in ("language", "memory")
    ]
    try:
        for _ in range(500):
            if all((tmp_path / f"ready-{role}").exists() for role in ("language", "memory")):
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("both processes did not load their baseline")
        (tmp_path / "go").write_text("go", encoding="ascii")
        results = await asyncio.gather(
            *(asyncio.wait_for(process.communicate(), 15) for process in processes)
        )
        for process, (_, stderr) in zip(processes, results, strict=True):
            assert process.returncode == 0, stderr.decode("utf-8", errors="replace")
    finally:
        for process in processes:
            if process.returncode is None:
                process.kill()
                await process.wait()

    current = await ConfigRepository(tmp_path).load()
    assert current.memory.enabled is True and current.memory.use is False
    assert current.preferences["language"] == "zh"
    assert not (tmp_path / "config.json.tmp").exists()

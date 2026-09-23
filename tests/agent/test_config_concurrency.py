"""两个 CLI 进程写同一份配置时保留彼此修改的字段。"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sayacode.config import Config, ConfigRepository, MemoryConfig, Profile


def _profile(name: str) -> Profile:
    return Profile(
        name=name,
        protocol="openai_chat_completions",
        base_url="https://example.test/v1",
        api_key="test-key",
        model_id=name,
        context_length=32_000,
        max_output_tokens=4_000,
    )


async def _seed(root: Path) -> None:
    await ConfigRepository(root).save(
        Config(
            default_profile="main",
            profiles={"main": _profile("main")},
            memory=MemoryConfig(enabled=True),
        )
    )


@pytest.mark.asyncio
async def test_two_loaded_configs_merge_memory_with_profile_and_preference(tmp_path: Path) -> None:
    await _seed(tmp_path)
    first_repository = ConfigRepository(tmp_path)
    second_repository = ConfigRepository(tmp_path)
    first = await first_repository.load()
    second = await second_repository.load()
    first.memory.enabled = False
    second.profiles["secondary"] = _profile("secondary")
    second.preferences["language"] = "zh"

    await asyncio.gather(first_repository.save(first), second_repository.save(second))

    current = await ConfigRepository(tmp_path).load()
    assert current.memory.enabled is False
    assert set(current.profiles) == {"main", "secondary"}
    assert current.preferences["language"] == "zh"
    identity = id(first)
    await first_repository.refresh(first)
    assert id(first) == identity
    assert "secondary" in first.profiles


@pytest.mark.asyncio
async def test_two_stale_memory_subkey_updates_are_merged(tmp_path: Path) -> None:
    await _seed(tmp_path)
    first_repository = ConfigRepository(tmp_path)
    second_repository = ConfigRepository(tmp_path)
    first = await first_repository.load()
    second = await second_repository.load()
    first.memory.use = False
    second.memory.learn = "explicit"

    await asyncio.gather(first_repository.save(first), second_repository.save(second))

    current = await ConfigRepository(tmp_path).load()
    assert current.memory.use is False
    assert current.memory.learn == "explicit"
    assert first_repository.path.exists()

    # 同一个子键两边都显式改动时，以最后完成的保存为准。
    third_repository = ConfigRepository(tmp_path)
    fourth_repository = ConfigRepository(tmp_path)
    third = await third_repository.load()
    fourth = await fourth_repository.load()
    third.memory.learn = "off"
    fourth.memory.learn = "auto"
    await third_repository.save(third)
    await fourth_repository.save(fourth)
    assert (await ConfigRepository(tmp_path).load()).memory.learn == "auto"


@pytest.mark.asyncio
async def test_stale_configs_merge_nested_deletions_and_additions(tmp_path: Path) -> None:
    await _seed(tmp_path)
    initial = ConfigRepository(tmp_path)
    config = await initial.load()
    config.preferences["old"] = "remove-me"
    config.mcp_servers["old"] = {"command": "old-server", "args": []}
    await initial.save(config)

    first_repository = ConfigRepository(tmp_path)
    second_repository = ConfigRepository(tmp_path)
    first = await first_repository.load()
    second = await second_repository.load()
    first.preferences.pop("old")
    first.mcp_servers.pop("old")
    second.preferences["language"] = "zh"
    second.mcp_servers["new"] = {"command": "new-server", "args": []}
    await asyncio.gather(first_repository.save(first), second_repository.save(second))

    current = await ConfigRepository(tmp_path).load()
    assert current.preferences == {"language": "zh"}
    assert current.mcp_servers == {"new": {"command": "new-server", "args": []}}


@pytest.mark.asyncio
async def test_explicit_memory_patch_overrides_stale_baseline_and_keeps_profiles(
    tmp_path: Path,
) -> None:
    await ConfigRepository(tmp_path).save(Config())
    first_repository = ConfigRepository(tmp_path)
    second_repository = ConfigRepository(tmp_path)
    first = await first_repository.load()
    await second_repository.load()  # 第二个 CLI 的基线仍是 disabled。

    first.memory.enabled = True
    first.profiles["coder"] = _profile("coder")
    first.default_profile = "coder"
    await first_repository.save(first)

    updated, previous = await second_repository.update_memory({"enabled": False})
    assert previous.enabled is True
    assert updated.memory.enabled is False
    assert updated.default_profile == "coder"
    assert "coder" in updated.profiles

    revoked_at = datetime.now(UTC).isoformat()
    updated, previous = await second_repository.update_memory({"revoked_before": revoked_at})
    assert previous.enabled is False
    assert updated.memory.revoked_before == revoked_at
    with pytest.raises(ValueError, match="unknown memory fields"):
        await second_repository.update_memory({"typo": True})
    current = await ConfigRepository(tmp_path).load()
    assert current.memory.enabled is False
    assert current.memory.revoked_before == revoked_at
    assert "coder" in current.profiles


@pytest.mark.asyncio
async def test_two_python_processes_merge_memory_subkeys(tmp_path: Path) -> None:
    await _seed(tmp_path)
    script = r'''
import asyncio
import sys
from pathlib import Path
from sayacode.config import ConfigRepository

async def main():
    root = Path(sys.argv[1])
    name = sys.argv[2]
    repository = ConfigRepository(root)
    config = await repository.load()
    if name == "use":
        config.memory.use = False
    else:
        config.memory.learn = "explicit"
    (root / f"ready-{name}").write_text("ready", encoding="ascii")
    for _ in range(500):
        if (root / "go").exists():
            break
        await asyncio.sleep(0.01)
    else:
        raise RuntimeError("timed out waiting for peer")
    await repository.save(config)

asyncio.run(main())
'''
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
    processes = [
        await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            script,
            str(tmp_path),
            name,
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        for name in ("use", "learn")
    ]
    try:
        for _ in range(500):
            if all((tmp_path / f"ready-{name}").exists() for name in ("use", "learn")):
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
    assert current.memory.use is False
    assert current.memory.learn == "explicit"

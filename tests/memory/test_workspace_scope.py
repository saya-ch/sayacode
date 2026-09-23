"""同一用户的多个工作区从真实应用与 SQLite Store 领取各自来源。"""

from __future__ import annotations

import subprocess
from pathlib import Path

from sayacode.agent import AgentRuntime
from sayacode.application import SayacodeApp
from sayacode.config import ConfigRepository
from sayacode.memory.records import MemorySource
from tests.support import ContractModel, contract_app


def _source(ref: str, order: int, thread_id: str, project_id: str) -> MemorySource:
    return MemorySource(
        ref=ref,
        kind="user",
        order=f"2026-01-01T00:00:{order:02d}+00:00",
        thread_id=thread_id,
        project_id=project_id,
    )


async def test_repository_root_subdir_and_other_project_claim_only_local_threads(
    tmp_path: Path,
) -> None:
    root_app = await contract_app(tmp_path, ContractModel(), session_id="root-thread")
    root = root_app.workspace
    subdir = root / "src"
    subdir.mkdir()
    other = tmp_path / "other-project"
    other.mkdir()
    subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
    paths = root_app.paths
    root_user, root_project = root_app.memory.repository.scopes_for(root)
    await root_app.aclose()

    async def open_at(workspace: Path, thread_id: str) -> SayacodeApp:
        repository = ConfigRepository(paths.home)
        config = await repository.load()
        runtime = await AgentRuntime.open(paths.home)
        app = SayacodeApp(
            paths=paths,
            repository=repository,
            config=config,
            runtime=runtime,
            workspace=workspace,
            session_id=thread_id,
            trust_level="ask",
            profile_name=config.default_profile,
            model_override=ContractModel(),
        )
        return await app.initialize()

    subdir_app = await open_at(subdir, "subdir-thread")
    try:
        subdir_user, subdir_project = subdir_app.memory.repository.scopes_for(subdir)
        assert subdir_user == root_user
        assert subdir_project == root_project
    finally:
        await subdir_app.aclose()

    other_app = await open_at(other, "other-thread")
    try:
        other_user, other_project = other_app.memory.repository.scopes_for(other)
        assert other_user == root_user
        assert other_project != root_project
        # 外项目用户来源先占租约，当前项目仍须能领取自己的用户来源。
        foreign = _source("foreign", 1, "other-thread", other_project.identity)
        await other_app.memory.repository.aenqueue(root_user, foreign)
        foreign_claim = await other_app.memory.learning._claim_local(root_user, None)
        assert foreign_claim is not None and foreign_claim.source.ref == foreign.ref
    finally:
        await other_app.aclose()

    reopened_root = await open_at(root, "root-thread")
    try:
        repo = reopened_root.memory.repository
        subdir_source = _source("subdir", 2, "subdir-thread", root_project.identity)
        root_source = _source("root", 3, "root-thread", root_project.identity)
        await repo.aenqueue(root_user, subdir_source)
        await repo.aenqueue(root_user, root_source)
        await repo.aenqueue(root_project, subdir_source)
        await repo.aenqueue(root_project, root_source)

        root_user_claim = await reopened_root.memory.learning._claim_local(root_user, None)
        root_project_claim = await reopened_root.memory.learning._claim_local(root_project, None)
        assert root_user_claim is not None and root_user_claim.source.ref == root_source.ref
        assert root_project_claim is not None and root_project_claim.source.ref == root_source.ref
        user_snapshot = await repo.aread(root_user)
        statuses = {job.source.ref: job.status for job in user_snapshot.pending.values()}
        assert statuses["subdir"] == "pending"
        assert statuses["foreign"] == "claimed"
    finally:
        await reopened_root.aclose()

    reopened_subdir = await open_at(subdir, "subdir-thread")
    try:
        user_claim = await reopened_subdir.memory.learning._claim_local(root_user, None)
        project_claim = await reopened_subdir.memory.learning._claim_local(root_project, None)
        assert user_claim is not None and user_claim.source.ref == "subdir"
        assert project_claim is not None and project_claim.source.ref == "subdir"
    finally:
        await reopened_subdir.aclose()

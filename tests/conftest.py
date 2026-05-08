import pytest


@pytest.fixture(autouse=True)
def isolate_sayacode_home(tmp_path, monkeypatch):
    """Keep test-created profiles, sessions, trust files, and audit logs out of the real home."""
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "sayacode-home"))

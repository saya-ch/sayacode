from lib.core.session import SessionManager
from lib.runtime import sync_session_model_runtime


class DummyModel:
    context_window = 4096

    def chat(self, messages):
        return "summary"


def test_sync_session_model_runtime_updates_context_and_compact_fn():
    session = SessionManager()
    model = DummyModel()

    sync_session_model_runtime(session, model)

    assert session.model_context_limit == 4096
    assert session._compact_fn([{"role": "user", "content": "compact"}]) == "summary"

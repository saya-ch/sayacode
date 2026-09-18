# probing 全覆盖：fake httpx 走完四协议。

import sys
from types import SimpleNamespace


import lib.models.probing as pb
from lib.models.probing import (
    probe_anthropic,
    probe_context_window,
    probe_gemini,
    probe_ollama,
    probe_openai_compatible,
)


def _resp(status=200, payload=None, exc=None):
    # 固定响应或抛错的假 httpx。
    if exc is not None:
        raise exc
    return SimpleNamespace(status_code=status, json=lambda: payload)


def _fake_httpx(monkeypatch, get=None, post=None):
    fake = SimpleNamespace()
    fake.get = get or (lambda *a, **k: _resp(status=404, payload={}))
    fake.post = post or (lambda *a, **k: _resp(status=404, payload={}))
    monkeypatch.setitem(sys.modules, "httpx", fake)
    return fake


class TestNested:
    def test_direct(self):
        from lib.models.probing import _search_nested

        assert _search_nested({"max_model_len": 8000}, pb._OPENAI_COMPATIBLE_FIELDS) == 8000

    def test_nested(self):
        from lib.models.probing import _search_nested

        assert _search_nested({"a": {"b": {"context_length": 4000}}}, pb._OPENAI_COMPATIBLE_FIELDS) == 4000

    def test_list(self):
        from lib.models.probing import _search_nested

        assert _search_nested([{"n_positions": 2000}], pb._OPENAI_COMPATIBLE_FIELDS) == 2000

    def test_too_deep(self):
        from lib.models.probing import _search_nested

        deep = {"l1": {"l2": {"l3": {"l4": {"max_model_len": 1}}}}}
        assert _search_nested(deep, pb._OPENAI_COMPATIBLE_FIELDS) is None
        assert _search_nested("oops", pb._OPENAI_COMPATIBLE_FIELDS) is None
        assert _search_nested({}, pb._OPENAI_COMPATIBLE_FIELDS) is None

    def test_find_entry(self):
        from lib.models.probing import _find_model_entry

        assert _find_model_entry({"data": [{"id": "m"}]}, "m") == {"id": "m"}
        assert _find_model_entry({"models": [{"name": "m"}]}, "m") == {"name": "m"}
        assert _find_model_entry({"items": [{"model": "m"}]}, "m") == {"model": "m"}
        assert _find_model_entry([{"id": "m"}], "m") == {"id": "m"}
        assert _find_model_entry({"data": ["oops"]}, "m") is None
        assert _find_model_entry({"other": []}, "m") is None
        assert _find_model_entry("oops", "m") is None
        assert _find_model_entry({"data": [{"id": "x"}]}, "m") is None


class TestOpenAI:
    def test_route1_hit(self, monkeypatch):
        _fake_httpx(monkeypatch, get=lambda *a, **k: _resp(payload={"max_model_len": 32000}))
        assert probe_openai_compatible("http://x", "m", "k") == 32000

    def test_route2_hit(self, monkeypatch):
        def get(url, **kw):
            if url.endswith("/models/m"):
                return _resp(payload={})
            return _resp(payload={"data": [{"id": "m", "context_length": 16000}]})

        _fake_httpx(monkeypatch, get=get)
        assert probe_openai_compatible("http://x", "m", None) == 16000

    def test_route2_404(self, monkeypatch):
        _fake_httpx(monkeypatch, get=lambda *a, **k: _resp(status=404, payload={}))
        assert probe_openai_compatible("http://x", "m", None) is None

    def test_no_entry(self, monkeypatch):
        _fake_httpx(monkeypatch, get=lambda *a, **k: _resp(payload={"data": []}))
        assert probe_openai_compatible("http://x", "m", None) is None

    def test_crash(self, monkeypatch):
        _fake_httpx(monkeypatch, get=lambda *a, **k: _resp(exc=RuntimeError("down")))
        assert probe_openai_compatible("http://x", "m", None) is None


class TestAnthropic:
    def test_hit(self, monkeypatch):
        _fake_httpx(monkeypatch, get=lambda *a, **k: _resp(payload={"max_input_tokens": 200000}))
        assert probe_anthropic("http://x", "m", "k") == 200000

    def test_404(self, monkeypatch):
        _fake_httpx(monkeypatch, get=lambda *a, **k: _resp(status=404, payload={}))
        assert probe_anthropic("http://x", "m", "k") is None

    def test_nested(self, monkeypatch):
        _fake_httpx(monkeypatch, get=lambda *a, **k: _resp(payload={"model": {"max_input_tokens": 100000}}))
        assert probe_anthropic("http://x", "m", "k") == 100000

    def test_nested_not_dict(self, monkeypatch):
        _fake_httpx(monkeypatch, get=lambda *a, **k: _resp(payload={"model": "x"}))
        assert probe_anthropic("http://x", "m", "k") is None

    def test_crash(self, monkeypatch):
        _fake_httpx(monkeypatch, get=lambda *a, **k: _resp(exc=RuntimeError("down")))
        assert probe_anthropic("http://x", "m", "k") is None


class TestGemini:
    def test_no_key(self, monkeypatch):
        _fake_httpx(monkeypatch)
        assert probe_gemini("http://x", "m", None) is None

    def test_hit(self, monkeypatch):
        _fake_httpx(monkeypatch, get=lambda *a, **k: _resp(payload={"inputTokenLimit": 1000000}))
        assert probe_gemini("http://x", "m", "k") == 1000000

    def test_404(self, monkeypatch):
        _fake_httpx(monkeypatch, get=lambda *a, **k: _resp(status=404, payload={}))
        assert probe_gemini("http://x", "m", "k") is None

    def test_crash(self, monkeypatch):
        _fake_httpx(monkeypatch, get=lambda *a, **k: _resp(exc=RuntimeError("down")))
        assert probe_gemini("http://x", "m", "k") is None


class TestOllama:
    def test_min(self, monkeypatch):
        payload = {"model_info": {"llama.context_length": 8192}, "modelfile": "FROM x\nnum_ctx 4096\n"}
        _fake_httpx(monkeypatch, post=lambda *a, **k: _resp(payload=payload))
        assert probe_ollama("http://x", "m") == 4096

    def test_native_only(self, monkeypatch):
        payload = {"model_info": {"other.key": 1, "llama.context_length": 8192}, "modelfile": "FROM x\n"}
        _fake_httpx(monkeypatch, post=lambda *a, **k: _resp(payload=payload))
        assert probe_ollama("http://x", "m") == 8192

    def test_runtime_bare_line(self, monkeypatch):
        payload = {"model_info": {}, "modelfile": "num_ctx\n"}
        _fake_httpx(monkeypatch, post=lambda *a, **k: _resp(payload=payload))
        assert probe_ollama("http://x", "m", "k") is None

    def test_runtime_value(self, monkeypatch):
        payload = {"model_info": {}, "modelfile": "FROM x\nNUM_CTX 4096\n"}
        _fake_httpx(monkeypatch, post=lambda *a, **k: _resp(payload=payload))
        assert probe_ollama("http://x", "m") == 4096

    def test_neither(self, monkeypatch):
        _fake_httpx(monkeypatch, post=lambda *a, **k: _resp(payload={}))
        assert probe_ollama("http://x", "m") is None

    def test_404(self, monkeypatch):
        _fake_httpx(monkeypatch, post=lambda *a, **k: _resp(status=404, payload={}))
        assert probe_ollama("http://x", "m") is None

    def test_crash(self, monkeypatch):
        _fake_httpx(monkeypatch, post=lambda *a, **k: _resp(exc=RuntimeError("down")))
        assert probe_ollama("http://x", "m") is None


class TestDispatch:
    def test_unknown(self):
        assert probe_context_window("ghost", "http://x", "m") is None

    def test_known(self, monkeypatch):
        _fake_httpx(monkeypatch, get=lambda *a, **k: _resp(payload={"max_model_len": 8000}))
        assert probe_context_window("openai", "http://x", "m") == 8000

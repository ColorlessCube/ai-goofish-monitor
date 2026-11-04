"""Release smoke must never target a production origin or follow redirects."""
import importlib.util
from pathlib import Path

import pytest


def smoke_module():
    path = Path(__file__).resolve().parents[2] / "scripts" / "smoke_native_mcp.py"
    assert path.is_file(), "native MCP release smoke is missing"
    spec = importlib.util.spec_from_file_location("smoke_native_mcp", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("url", ["https://monitor.example.test:18567", "http://10.0.0.1:8000", "http://127.0.0.1.evil.test:8000", "http://user:secret@127.0.0.1:8000", "http://127.0.0.1:8000/?token=secret", "http://127.0.0.1:8000/api", "file:///etc/passwd"])
def test_smoke_rejects_non_fixture_origins(url):
    module = smoke_module()
    with pytest.raises(module.SmokeFailure):
        module.validate_origin(url)


def test_smoke_accepts_explicit_numeric_loopback_origin():
    assert smoke_module().validate_origin("http://127.0.0.1:19876") == "http://127.0.0.1:19876"

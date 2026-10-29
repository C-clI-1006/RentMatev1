"""Keep collection-time app wiring away from local credentials and data."""
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


_sandbox = tempfile.TemporaryDirectory(prefix="rentmate-tests-")
_env = patch.dict(os.environ, {
    "DATABASE_PATH": str(Path(_sandbox.name) / "app.db"),
    "EVIDENCE_STORAGE_ROOT": str(Path(_sandbox.name) / "storage"),
    "RENTMATE_AUTH_TOKENS": "{}",
    "RENTMATE_SCHEDULER_TOKEN": "",
    "RENTMATE_EVIDENCE_DISPATCH_MANAGERS": "{}",
    "LLM_BASE_URL": "",
    "LLM_API_KEY": "",
    "LLM_MODEL": "test-only",
})
_env.start()
_dotenv = patch("dotenv.load_dotenv", return_value=False)
_dotenv.start()


@pytest.fixture(autouse=True)
def block_external_connections(monkeypatch):
    import socket

    original_connect = socket.socket.connect

    def blocked(sock, address):
        # asyncio on Windows implements socketpair through loopback TCP.
        if isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1"):
            return original_connect(sock, address)
        raise AssertionError("Tests must not open external connections")

    monkeypatch.setattr(socket.socket, "connect", blocked)


def pytest_unconfigure(config):
    # Windows cannot remove a database while the module-level app holds it.
    import sys

    if "main" in sys.modules:
        sys.modules["main"].app.state.evidence_service._repo.close()
    _dotenv.stop()
    _env.stop()
    _sandbox.cleanup()

"""Transport tests do not import Blender or start an external service."""

import importlib.util
import socket
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "compact_transport", ROOT / "addon/compact_blender/transport.py"
)
transport = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = transport
spec.loader.exec_module(transport)


class SlowRun:
    """Stands in for a visible-mode batch: one step per advance()."""

    def __init__(self, steps):
        self.left = steps
        self.advanced = 0

    def advance(self):
        self.advanced += 1
        self.left -= 1
        return None if self.left else {"ok": True, "steps": self.advanced}


class FakeEngine:
    def __init__(self, root):
        self.output_dir = root
        self.calls = 0

    def needs_approval(self, method, params):
        return "risky" if params.get("risky") else None

    def dispatch(self, method, params):
        self.calls += 1
        if params.get("slow"):
            return SlowRun(params["slow"])
        return {"method": method, "calls": self.calls}


@pytest.fixture
def server(tmp_path):
    instance = transport.Server(FakeEngine(tmp_path / "exports"), tmp_path / "connection.json")
    yield instance
    instance.close()


def request(server, **changes):
    return {"id": "a", "token": server.token, "method": "execute", "params": {}, **changes}


def test_authorization_precedes_execution(server):
    assert server.handle(request(server, token="wrong"))["error"] == "Unauthorized"
    assert server.engine.calls == 0


def test_replay_does_not_duplicate_execution(server):
    first = server.handle(request(server))
    assert server.handle(request(server)) == first
    assert server.engine.calls == 1


def test_reused_id_cannot_change_operation(server):
    server.handle(request(server))
    assert not server.handle(request(server, params={"different": True}))["ok"]
    assert server.engine.calls == 1


def test_descriptor_lifecycle_and_loopback(server):
    assert server.socket.getsockname()[0] == "127.0.0.1"
    assert server.descriptor.exists()
    server.close()
    assert not server.descriptor.exists()


def test_network_invalid_json_is_rejected(server):
    with socket.create_connection(server.socket.getsockname()) as client:
        client.sendall(b"not JSON\n")
        server.poll()
        client.settimeout(1)
        assert b"Invalid JSON" in client.recv(4096)
    assert server.engine.calls == 0


def test_request_size_limit(server):
    with socket.create_connection(server.socket.getsockname()) as client:
        client.sendall(b"x" * (transport.MAX_REQUEST + 1))
        for _ in range(50):  # loopback delivers the bytes in pieces; give it time to arrive
            server.poll()
            time.sleep(0.01)
        client.settimeout(1)
        assert b"Request too large" in client.recv(4096)
    assert server.engine.calls == 0


def test_cache_bounded(server):
    for i in range(140):
        server.handle(request(server, id=str(i)))
    assert len(server.cache) == 128


def test_bad_shapes_rejected(server):
    for value in ([], None, "text", 42):
        assert not server.handle(value)["ok"]
    assert not server.handle(request(server, params=[]))["ok"]
    assert not server.handle(request(server, id=""))["ok"]


def ask(server, conn, **params):
    conn.sendall(
        transport.encode({"id": "held", "token": server.token, "method": "execute", "params": params})
    )
    for _ in range(3):
        server.poll()


def test_risky_request_waits_for_approval_then_runs(server):
    with socket.create_connection(server.socket.getsockname()) as client:
        ask(server, client, risky=True)
        assert server.engine.calls == 0
        assert [p.summary for p in server.pending] == ["risky"]
        assert server.decide(True)
        server.poll()
        client.settimeout(1)
        assert b'"ok":true' in client.recv(4096)
    assert server.engine.calls == 1
    assert server.pending == []


def test_denied_request_never_runs(server):
    with socket.create_connection(server.socket.getsockname()) as client:
        ask(server, client, risky=True)
        server.decide(False)
        server.poll()
        client.settimeout(1)
        assert b"Denied" in client.recv(4096)
    assert server.engine.calls == 0


def test_unanswered_request_is_denied_after_the_timeout(server, monkeypatch):
    monkeypatch.setattr(transport, "APPROVAL_TIMEOUT", -1)
    with socket.create_connection(server.socket.getsockname()) as client:
        ask(server, client, risky=True)
        server.poll()
        client.settimeout(1)
        assert b"Denied" in client.recv(4096)
    assert server.engine.calls == 0


def test_safe_request_is_not_held(server):
    with socket.create_connection(server.socket.getsockname()) as client:
        ask(server, client)
        client.settimeout(1)
        assert b'"ok":true' in client.recv(4096)
    assert server.pending == []


def test_nothing_to_decide(server):
    assert server.decide(True) is False


def test_visible_batch_runs_one_step_per_tick(server):
    with socket.create_connection(server.socket.getsockname()) as client:
        ask(server, client, slow=3)  # three polls: read the request, then two steps
        assert len([c for c in server.clients.values() if c.get("job")]) == 1
        client.settimeout(0.2)
        with pytest.raises(TimeoutError):
            client.recv(4096)
        server.poll()
        client.settimeout(1)
        assert b'"steps":3' in client.recv(4096)


def test_paused_bridge_refuses_new_requests_without_running_them(server):
    server.paused = True
    reply = server.handle(request(server))
    assert not reply["ok"] and "Paused" in reply["error"]
    assert server.engine.calls == 0
    server.paused = False
    assert server.handle(request(server))["ok"]

"""Bounded, loopback-only JSON transport, polled by Blender's main-thread timer.

No background thread calls bpy. One JSON line per connection; no implicit retries.
"""

import hashlib
import hmac
import json
import os
import secrets
import socket
import time
from collections import OrderedDict
from pathlib import Path

MAX_REQUEST = 262144
MAX_CLIENTS = 8
APPROVAL_TIMEOUT = 120  # shorter than the client's default 180 s, so a denial reaches it in time


def state_dir():
    base = (
        os.environ.get("LOCALAPPDATA")
        or os.environ.get("XDG_STATE_HOME")
        or str(Path.home() / ".local/state")
    )
    return Path(base) / "blender-compact-mcp"


def encode(value):
    return (json.dumps(value, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


class Pending:
    """A request held until the user approves or denies it in Blender."""

    def __init__(self, request_id, digest, method, params, summary):
        self.request_id, self.digest, self.method, self.params, self.summary = (
            request_id,
            digest,
            method,
            params,
            summary,
        )
        self.since = time.monotonic()
        self.allowed = None


class Job:
    """A batch that runs one step per timer tick, so the user can watch it."""

    def __init__(self, request_id, digest, run):
        self.request_id, self.digest, self.run = request_id, digest, run


class Server:
    def __init__(self, engine, descriptor=None):
        self.engine = engine
        self.paused = False
        self.pending = []
        self.notify = None  # called when the list of pending approvals changes
        self.token = secrets.token_urlsafe(32)
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(MAX_CLIENTS)
        self.socket.setblocking(False)
        self.clients = {}
        self.cache = OrderedDict()
        self.closed = False
        self.descriptor = Path(descriptor) if descriptor else state_dir() / f"connection-{os.getpid()}.json"
        self.descriptor.parent.mkdir(parents=True, exist_ok=True)
        self.engine.output_dir.mkdir(parents=True, exist_ok=True)
        info = {
            "protocol": 1,
            "host": "127.0.0.1",
            "port": self.socket.getsockname()[1],
            "token": self.token,
            "pid": os.getpid(),
            "output_dir": str(self.engine.output_dir.resolve()),
            "crash_dir": self.engine.crash_dir,
        }
        fd = os.open(self.descriptor, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(info, out)

    def handle(self, request):
        if not isinstance(request, dict):
            return {"ok": False, "error": "Request must be an object"}
        token = request.get("token")
        if not isinstance(token, str) or not hmac.compare_digest(token, self.token):
            return {"ok": False, "error": "Unauthorized"}
        request_id = request.get("id")
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 80:
            return {"ok": False, "error": "Request needs an id of 1..80 characters"}
        params = request.get("params", {})
        if not isinstance(params, dict):
            return {"ok": False, "error": "params must be an object"}
        digest = hashlib.sha256(encode([request.get("method"), params])).hexdigest()
        if request_id in self.cache:
            previous_digest, result = self.cache[request_id]
            if previous_digest != digest:
                return {"ok": False, "error": "Request id reused with different parameters"}
            return result
        method = request.get("method")
        if self.paused:
            return {"ok": False, "error": "Paused by the user in Blender. Nothing ran; try again later."}
        if any(p.request_id == request_id for p in self.pending):
            return {"ok": False, "error": "Request is already waiting for approval"}
        summary = self.engine.needs_approval(method, params)
        if summary:
            held = Pending(request_id, digest, method, params, summary)
            self.pending.append(held)
            self.changed()
            return held
        return self.finish(request_id, digest, method, params)

    def finish(self, request_id, digest, method, params):
        try:
            value = self.engine.dispatch(method, params)
            if hasattr(value, "advance"):
                return Job(request_id, digest, value)
            result = {"ok": True, "result": value}
        except Exception as exc:
            result = {"ok": False, "error": str(exc), "type": type(exc).__name__}
        return self.remember(request_id, digest, result)

    def remember(self, request_id, digest, result):
        self.cache[request_id] = (digest, result)
        if len(self.cache) > 128:
            self.cache.popitem(last=False)
        return result

    def step(self, job):
        """Run the next step of a visible batch. Returns the reply once it is over, else None."""
        try:
            value = job.run.advance()
        except Exception as exc:
            return self.remember(
                job.request_id, job.digest, {"ok": False, "error": str(exc), "type": type(exc).__name__}
            )
        if value is None:
            return None
        return self.remember(job.request_id, job.digest, {"ok": True, "result": value})

    def accept(self, state, reply):
        if isinstance(reply, Pending):
            state["held"] = reply
        elif isinstance(reply, Job):
            state["job"] = reply
            self.changed()
        else:
            state["output"] = encode(reply)

    def changed(self):
        if self.notify:
            self.notify()

    def decide(self, allowed):
        """Approve or deny the oldest waiting request. Returns False when none is waiting."""
        for held in self.pending:
            if held.allowed is None:
                held.allowed = bool(allowed)
                return True
        return False

    def resolve(self, held):
        """The reply for a held request once it is decided or has timed out, else None."""
        if held.allowed is None and time.monotonic() - held.since > APPROVAL_TIMEOUT:
            held.allowed = False
        if held.allowed is None:
            return None
        self.pending.remove(held)
        self.changed()
        if not held.allowed:
            return {"ok": False, "error": "Denied in Blender, or not approved in time", "type": "Denied"}
        return self.finish(held.request_id, held.digest, held.method, held.params)

    def drop(self, conn):
        state = self.clients.pop(conn, None)
        if state and state.get("job"):
            # Nobody is waiting for the rest of the batch. What already ran stays; the interface unlocks.
            state["job"].run.engine.running = None
        if state and state.get("held") in self.pending:
            # The client is gone, so there is nobody to answer. Nothing has run.
            self.pending.remove(state["held"])
            self.changed()
        conn.close()

    def poll(self):
        if self.closed:
            return None
        # Each tick bounds network work. Blender operations themselves can block.
        for _ in range(MAX_CLIENTS):
            try:
                conn, _ = self.socket.accept()
            except BlockingIOError:
                break
            conn.setblocking(False)
            if len(self.clients) >= MAX_CLIENTS:
                conn.close()
            else:
                self.clients[conn] = {"input": bytearray(), "output": None, "since": time.monotonic()}
        executed = False
        for conn, state in list(self.clients.items()):
            try:
                held, job = state.get("held"), state.get("job")
                if held:
                    reply = None if executed else self.resolve(held)
                    if reply is not None:
                        state.update(held=None, since=time.monotonic())
                        self.accept(state, reply)
                        executed = True
                elif job:
                    reply = None if executed else self.step(job)
                    executed = True
                    if reply is not None:
                        state.update(job=None, since=time.monotonic())
                        self.accept(state, reply)
                        self.changed()
                elif state["output"] is None:
                    if time.monotonic() - state["since"] > 10:
                        self.drop(conn)
                        continue
                    try:
                        data = conn.recv(65536)
                    except BlockingIOError:
                        data = None
                    if data == b"":
                        self.drop(conn)
                        continue
                    if data:
                        state["input"].extend(data)
                    if len(state["input"]) > MAX_REQUEST:
                        state["output"] = encode({"ok": False, "error": "Request too large"})
                    elif b"\n" in state["input"] and not executed:
                        try:
                            request = json.loads(
                                state["input"].split(b"\n", 1)[0],
                                parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
                            )
                            self.accept(state, self.handle(request))
                        except (ValueError, TypeError, UnicodeError):
                            state["output"] = encode({"ok": False, "error": "Invalid JSON request"})
                        state["since"] = time.monotonic()
                        executed = True
                if state["output"] is not None:
                    try:
                        count = conn.send(state["output"])
                        state["output"] = state["output"][count:]
                    except BlockingIOError:
                        pass
                    if not state["output"] or time.monotonic() - state["since"] > 10:
                        self.drop(conn)
            except OSError:
                self.drop(conn)
        if any(state.get("job") for state in self.clients.values()):
            return max(getattr(self.engine, "pace", 0), 0.02)
        return 0.02 if self.clients else 0.1

    def close(self):
        if self.closed:
            return
        self.closed = True
        for conn in list(self.clients):
            self.drop(conn)
        self.socket.close()
        try:
            if json.loads(self.descriptor.read_text())["token"] == self.token:
                self.descriptor.unlink()
        except (OSError, ValueError, KeyError):
            pass

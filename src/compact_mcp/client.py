"""Authenticated local client. Timeout means unknown outcome, never automatic replay."""

import json
import os
import socket
import tempfile
import time
import uuid
from pathlib import Path


def state_dir():
    base = (
        os.environ.get("LOCALAPPDATA")
        or os.environ.get("XDG_STATE_HOME")
        or str(Path.home() / ".local/state")
    )
    return Path(base) / "blender-compact-mcp"


def stale(path):
    """True only when nothing listens on the descriptor's port, e.g. after a Blender crash."""
    try:
        port = json.loads(path.read_text(encoding="utf-8"))["port"]
        # Windows retries a refused loopback connect for about 2 seconds.
        socket.create_connection(("127.0.0.1", port), timeout=3).close()
    except ConnectionRefusedError:
        return True
    except (OSError, ValueError, KeyError):
        pass
    return False


def crash_report(crash_dir, lines=12):
    """What to tell the agent when Blender has gone: its newest crash log, if Blender wrote one."""
    logs = [p for p in Path(crash_dir).glob("*.crash.txt") if time.time() - p.stat().st_mtime < 600]
    if not logs:
        return "Blender closed or crashed during the call; no crash log found. Unsaved work may be lost."
    log = max(logs, key=lambda p: p.stat().st_mtime)
    tail = log.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-lines:]
    return f"Blender crashed during the call. Crash log: {log}\n" + "\n".join(tail)


class Client:
    def __init__(self, descriptor=None):
        explicit = descriptor or os.environ.get("BLENDER_COMPACT_CONNECTION")
        if explicit:
            path = Path(explicit)
        else:
            paths = sorted(state_dir().glob("connection-*.json"))
            wanted = os.environ.get("FRUGAL_HEADLESS")
            if not paths and wanted:
                # Opt-in: with FRUGAL_HEADLESS=1 (or a permission list) the server starts its own Blender.
                from . import headless

                headless.ensure(state_dir(), wanted)
                paths = sorted(state_dir().glob("connection-*.json"))
            if len(paths) > 1:
                for path in [p for p in paths if stale(p)]:
                    path.unlink(missing_ok=True)
                    paths.remove(path)
            if len(paths) != 1:
                raise RuntimeError(
                    "Start the Blender bridge. For multiple/stale instances, set BLENDER_COMPACT_CONNECTION."
                )
            path = paths[0]
        self.path = path
        self.info = json.loads(path.read_text(encoding="utf-8"))
        if self.info.get("protocol") != 1 or self.info.get("host") != "127.0.0.1":
            raise ValueError("Only protocol 1 loopback connections are supported")

    def call(self, method, params=None, request_id=None, timeout=180):
        rid = request_id or str(uuid.uuid4())
        data = (
            json.dumps(
                {"id": rid, "token": self.info["token"], "method": method, "params": params or {}},
                separators=(",", ":"),
                allow_nan=False,
            ).encode()
            + b"\n"
        )
        if len(data) > 262144:
            raise ValueError("Request exceeds 256 KiB")
        try:
            with socket.create_connection(("127.0.0.1", self.info["port"]), timeout=timeout) as conn:
                conn.sendall(data)
                received = bytearray()
                while b"\n" not in received:
                    chunk = conn.recv(65536)
                    if not chunk:
                        raise ConnectionError("Connection closed before a complete result")
                    received.extend(chunk)
                    if len(received) > 2 * 1024 * 1024:
                        raise ValueError("Response exceeds 2 MiB")
        except (TimeoutError, ConnectionError, OSError) as exc:
            if stale(self.path):
                report = crash_report(self.info.get("crash_dir") or tempfile.gettempdir())
                raise RuntimeError(f"{report}\nThe outcome may be unknown. Request id: {rid}") from exc
            raise RuntimeError(
                f"Transport failed; outcome may be unknown. Inspect before retrying. Request id: {rid}"
            ) from exc
        result = json.loads(received.split(b"\n", 1)[0])
        if not result.get("ok"):
            raise RuntimeError(result.get("error", "Bridge rejected request"))
        return result["result"]

    def image_path(self, filename):
        root = Path(self.info["output_dir"]).resolve()
        path = (root / filename).resolve()
        if path.parent != root or path.suffix.lower() != ".png" or path.is_symlink():
            raise ValueError("Invalid image output path")
        return path

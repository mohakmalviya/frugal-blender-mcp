import json
from pathlib import Path

import pytest

from compact_mcp.client import Client


def descriptor(tmp_path, **changes):
    path = tmp_path / "connection.json"
    path.write_text(
        json.dumps(
            {
                "protocol": 1,
                "host": "127.0.0.1",
                "port": 1,
                "output_dir": str(tmp_path / "exports"),
                "token": "fake",
                **changes,
            }
        )
    )
    return path


def test_remote_endpoint_refused(tmp_path):
    with pytest.raises(ValueError, match="loopback"):
        Client(descriptor(tmp_path, host="example.com"))


def test_capture_path_traversal_refused(tmp_path):
    client = Client(descriptor(tmp_path))
    with pytest.raises(ValueError):
        client.image_path("../private.png")
    assert client.image_path("preview.png") == Path(tmp_path / "exports/preview.png")


def test_cli_accepts_bom_from_powershell_pipe(monkeypatch, capsys):
    import io
    import sys

    from compact_mcp import cli

    calls = []

    class FakeClient:
        def __init__(self, connection=None):
            pass

        def call(self, method, params):
            calls.append((method, params))
            return {"ok": True}

    monkeypatch.setattr(cli, "Client", FakeClient)
    monkeypatch.setattr(sys, "argv", ["blender-compact", "execute", "--params", "-"])
    monkeypatch.setattr(sys, "stdin", io.StringIO('﻿{"steps": []}'))
    cli.main()
    assert calls == [("execute", {"steps": []})]
    assert capsys.readouterr().out.strip() == '{"ok":true}'


def test_dead_server_does_not_silently_retry(tmp_path):
    client = Client(descriptor(tmp_path))
    with pytest.raises(RuntimeError, match="outcome may be unknown"):
        client.call("execute", timeout=0.1)


def test_stale_descriptor_from_a_crash_is_ignored_and_removed(tmp_path, monkeypatch):
    import socket

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.delenv("BLENDER_COMPACT_CONNECTION", raising=False)
    folder = tmp_path / "blender-compact-mcp"
    folder.mkdir()
    dead = socket.socket()
    dead.bind(("127.0.0.1", 0))
    dead_port = dead.getsockname()[1]
    dead.close()
    with socket.socket() as live:
        live.bind(("127.0.0.1", 0))
        live.listen(1)
        base = {"protocol": 1, "host": "127.0.0.1", "output_dir": "x", "token": "t"}
        (folder / "connection-1.json").write_text(json.dumps({**base, "port": dead_port}))
        (folder / "connection-2.json").write_text(json.dumps({**base, "port": live.getsockname()[1]}))
        client = Client()
    assert client.info["port"] != dead_port
    assert [p.name for p in folder.glob("connection-*.json")] == ["connection-2.json"]


def test_crash_report_names_the_newest_crash_log(tmp_path):
    from compact_mcp import client

    assert "no crash log" in client.crash_report(tmp_path)
    (tmp_path / "scene.crash.txt").write_text("header\n# Python backtrace\n  File engine.py, line 323 in finish\n")
    report = client.crash_report(tmp_path)
    assert str(tmp_path / "scene.crash.txt") in report and "line 323 in finish" in report

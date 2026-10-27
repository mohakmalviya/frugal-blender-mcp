import os
import time

import pytest

from compact_mcp import headless
from compact_mcp.client import Client

needs_blender = pytest.mark.skipif(not os.environ.get("BLENDER_EXE"), reason="Set BLENDER_EXE")


def wait_for_bridge(folder, process):
    deadline = time.monotonic() + 60
    while not list(folder.glob("connection-*.json")):
        if process.poll() is not None or time.monotonic() > deadline:
            pytest.fail("Headless Blender did not start")
        time.sleep(0.2)


@needs_blender
def test_headless_blender_serves_the_bridge_without_a_window(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    process = headless.spawn(output=tmp_path / "out", permissions="write,render,save")
    try:
        wait_for_bridge(tmp_path / "blender-compact-mcp", process)
        client = Client()
        result = client.call("execute", {"steps": [{"op": "primitive", "name": "Quiet", "kind": "cube"}]})
        assert result["ok"]
        info = client.call("inspect", {"names": ["Quiet"]})
        assert info["total"] == 1 and info["permissions"] == ["render", "save", "write"]
    finally:
        process.terminate()
        process.wait(timeout=20)


@needs_blender
def test_server_can_start_its_own_headless_blender(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("FRUGAL_HEADLESS", "1")
    try:
        assert Client().call("inspect")["scene"]
    finally:
        headless._started.terminate()
        headless._started.wait(timeout=20)


def test_missing_blender_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("BLENDER_EXE", raising=False)
    monkeypatch.setattr(headless.shutil, "which", lambda name: None)
    monkeypatch.setattr(headless.sys, "platform", "linux")
    with pytest.raises(FileNotFoundError, match="--blender"):
        headless.find_blender()

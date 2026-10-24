import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from compact_mcp.client import Client

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(
    not os.environ.get("BLENDER_TEST_UI"), reason="Opt-in real Blender UI timer and undo test"
)
def test_real_timer_and_undo(tmp_path):
    exe = os.environ["BLENDER_EXE"]
    info = None
    if os.name == "nt":
        info = subprocess.STARTUPINFO()
        info.dwFlags = subprocess.STARTF_USESHOWWINDOW
        info.wShowWindow = 0  # Hidden test helper, never the user's interactive Blender window.
    (ROOT / "artifacts").mkdir(exist_ok=True)
    with (ROOT / "artifacts/blender-ui.log").open("w") as log:
        process = subprocess.Popen(
            [
                exe,
                "--factory-startup",
                "--python",
                str(ROOT / "scripts/blender_ui_host.py"),
                "--",
                str(tmp_path),
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
            startupinfo=info,
        )
        try:

            def wait_for(path):
                deadline = time.monotonic() + 40
                while not path.exists():
                    if process.poll() is not None or time.monotonic() > deadline:
                        pytest.fail("UI helper stopped or timed out; see artifacts/blender-ui.log")
                    time.sleep(0.1)

            connection = tmp_path / "connection.json"
            wait_for(connection)
            client = Client(connection)

            def execute(*steps):
                return client.call("execute", {"steps": list(steps)})

            def cube(name):
                return {"op": "primitive", "name": name, "kind": "cube"}

            def count(name):
                return client.call("inspect", {"names": [name]})["total"]

            # A failed batch is rolled back, including the steps that worked.
            failed = execute(cube("Ghost"), {"op": "transform", "name": "Missing", "location": [0, 0, 0]})
            assert failed["ok"] is False and failed["rolled_back"] is True
            assert count("Ghost") == 0
            # Delete is held until the user decides, and cleans up the mesh it leaves behind.
            assert execute(cube("Doomed"))["ok"]
            deleted = execute({"op": "delete", "names": ["Doomed"], "confirm": True})
            assert deleted["ok"], deleted
            assert count("Doomed") == 0
            (tmp_path / "deny").touch()
            assert execute(cube("Spared"))["ok"]
            with pytest.raises(RuntimeError, match="Denied"):
                execute({"op": "delete", "names": ["Spared"], "confirm": True})
            (tmp_path / "deny").unlink()
            assert count("Spared") == 1
            assert execute(cube("Doomed2"))["ok"]
            assert client.call(
                "execute",
                {"steps": [{"op": "primitive", "name": "UITest", "kind": "cube", "location": [1, 2, 3]}]},
            )["ok"]
            assert client.call(
                "execute", {"steps": [{"op": "transform", "name": "UITest", "location": [7, 8, 9]}]}
            )["ok"]
            (tmp_path / "undo").touch()
            wait_for(tmp_path / "ui-result.json")
            result = json.loads((tmp_path / "ui-result.json").read_text())
            assert result == {
                "selected": ["UITest"],
                "log": ["primitive", "transform"],
                "running": False,
                "poll": True,
                "location": [1, 2, 3],
                "timer_removed": True,
                "descriptor_removed": True,
            }
        finally:
            (tmp_path / "stop").touch()
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=10)

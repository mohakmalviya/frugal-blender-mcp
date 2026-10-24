import os
import shutil
import subprocess
import time

import pytest

from compact_mcp.client import Client
from compact_mcp.install import addon_source

SETUP = """
import bpy
bpy.ops.preferences.addon_enable(module="compact_blender")
prefs = bpy.context.preferences.addons["compact_blender"].preferences
prefs.auto_start = True
bpy.ops.wm.save_as_mainfile(filepath=r"{blend}")
bpy.ops.wm.save_userpref()
"""


@pytest.mark.skipif(not os.environ.get("BLENDER_TEST_UI"), reason="Opt-in real Blender UI test")
def test_bridge_starts_itself_from_preferences(tmp_path):
    exe = os.environ["BLENDER_EXE"]
    scripts = tmp_path / "scripts"
    shutil.copytree(addon_source(), scripts / "addons/compact_blender")
    env = {
        **os.environ,
        "BLENDER_USER_CONFIG": str(tmp_path / "config"),
        "BLENDER_USER_SCRIPTS": str(scripts),
        "LOCALAPPDATA": str(tmp_path / "state"),
    }
    setup = tmp_path / "setup.py"
    setup.write_text(SETUP.format(blend=tmp_path / "scene.blend"))
    subprocess.run(
        [exe, "--background", "--factory-startup", "--python", str(setup)], env=env, check=True, timeout=90
    )

    info = None
    if os.name == "nt":
        info = subprocess.STARTUPINFO()
        info.dwFlags = subprocess.STARTF_USESHOWWINDOW
        info.wShowWindow = 0
    # Opening a file at launch covers both the startup timer and the load_post handler.
    process = subprocess.Popen([exe, str(tmp_path / "scene.blend")], env=env, startupinfo=info)
    try:
        deadline = time.monotonic() + 60
        found = []
        while not found:
            found = list((tmp_path / "state/blender-compact-mcp").glob("connection-*.json"))
            if process.poll() is not None or time.monotonic() > deadline:
                pytest.fail("Bridge did not start by itself")
            time.sleep(0.2)
        assert Client(found[0]).call("inspect")["permissions"] == ["render", "write"]
    finally:
        process.terminate()
        process.wait(timeout=20)

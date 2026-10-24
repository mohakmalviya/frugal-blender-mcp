"""Run the bridge in a Blender with no window, for servers, CI and agents that need no viewport."""

import atexit
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import install

DEFAULT_PERMISSIONS = "write,render"
_started = None


def find_blender(explicit=None):
    """The Blender executable: the argument, then BLENDER_EXE, then the usual install places."""
    candidates = [explicit, os.environ.get("BLENDER_EXE")]
    if sys.platform == "win32":
        base = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Blender Foundation"
        candidates += sorted(base.glob("Blender */blender.exe"), reverse=True)
    elif sys.platform == "darwin":
        candidates.append("/Applications/Blender.app/Contents/MacOS/Blender")
    candidates.append(shutil.which("blender"))
    for path in candidates:
        if path and Path(path).is_file():
            return str(path)
    raise FileNotFoundError("Blender not found. Pass --blender PATH or set BLENDER_EXE.")


def spawn(blender=None, blend_file=None, permissions=DEFAULT_PERMISSIONS, output=None, quiet=True):
    """Start Blender in the background with the bridge running. Returns the process."""
    command = [find_blender(blender), "--background", "--factory-startup"]
    if blend_file:
        command.append(str(blend_file))
    host = Path(__file__).with_name("headless_host.py")
    command += ["--python", str(host), "--", str(install.addon_source().parent), permissions]
    if output:
        command.append(str(output))
    sink = subprocess.DEVNULL if quiet else None  # on the command line, show Blender's own messages
    return subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=sink, stderr=sink)


def run(blender=None, blend_file=None, permissions=DEFAULT_PERMISSIONS):
    process = spawn(blender, blend_file, permissions, quiet=False)
    print(f"Headless Blender is running (pid {process.pid}). Press Ctrl+C to stop it.")
    try:
        process.wait()
    except KeyboardInterrupt:
        process.terminate()
        process.wait(timeout=20)


def ensure(descriptors, permissions):
    """Start one headless Blender for this MCP server when none is running and the user asked for it."""
    global _started
    if _started is None or _started.poll() is not None:
        _started = spawn(permissions=permissions if permissions != "1" else DEFAULT_PERMISSIONS)
        atexit.register(_started.terminate)
    deadline = time.monotonic() + 60
    while not list(descriptors.glob("connection-*.json")):
        if _started.poll() is not None or time.monotonic() > deadline:
            raise RuntimeError(
                "Headless Blender did not start. Try `frugal-blender-mcp headless` to see why."
            )
        time.sleep(0.2)

"""Runs inside Blender (`blender --background`): serves the bridge until the process is stopped.

Not importable outside Blender. `frugal-blender-mcp headless` starts it.
"""

import sys
import time
from pathlib import Path

import bpy  # noqa: F401  (proves we are inside Blender)

addon, permissions, *rest = sys.argv[sys.argv.index("--") + 1 :]
sys.path.insert(0, addon)
import compact_blender  # noqa: E402

compact_blender.register()
server = compact_blender.start(
    Path(rest[0]) if rest else None, None, tuple(p for p in permissions.split(",") if p), timer=False
)
try:
    while True:
        server.poll()
        time.sleep(0.01)
finally:
    compact_blender.unregister()

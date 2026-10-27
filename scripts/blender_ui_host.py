"""Isolated UI/timer/Undo check. No user scene or preferences are loaded or saved."""

import json
import sys
import time
from pathlib import Path

import bpy

folder = Path(sys.argv[sys.argv.index("--") + 1])
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "addon"))
import compact_blender  # noqa: E402

compact_blender.register()
compact_blender.start(
    folder / "exports", folder / "connection.json", ("write", "render", "delete"), ask=True, visible=True
)
deadline = time.monotonic() + 90


def lifecycle():
    # Stands in for the user pressing Approve or Deny in the sidebar.
    if compact_blender._server and compact_blender._server.pending:
        bpy.ops.compact_mcp.decide(allow=not (folder / "deny").exists())
    if (folder / "undo").exists():
        (folder / "undo").unlink()
        server = compact_blender._server
        result = {
            "selected": [o.name for o in bpy.context.selected_objects],
            "log": [e["op"] for e in server.engine.log][-2:],
            "running": server.engine.running is not None,
            "poll": bpy.ops.ed.undo.poll(),
        }
        if result["poll"]:
            bpy.ops.ed.undo()
            result["location"] = list(bpy.data.objects["UITest"].location)
        callback = compact_blender._timer
        compact_blender.stop()
        result["timer_removed"] = not bpy.app.timers.is_registered(callback)
        result["descriptor_removed"] = not (folder / "connection.json").exists()
        (folder / "ui-result.json").write_text(json.dumps(result))
    if (folder / "stop").exists() or time.monotonic() > deadline:
        compact_blender.unregister()
        bpy.ops.wm.quit_blender()
        return None
    return 0.1


bpy.app.timers.register(lifecycle, first_interval=0.1)

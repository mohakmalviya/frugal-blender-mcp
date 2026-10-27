"""Install this directory as a legacy Blender add-on (ZIP provided by package script)."""

import atexit
from pathlib import Path

import bpy
from bpy.app.handlers import persistent

from .engine import Engine
from .transport import Server, state_dir

bl_info = {
    "name": "Frugal Blender MCP",
    "author": "Mohak Malviya, doolecg",
    "version": (0, 2, 0),
    "blender": (4, 2, 0),
    "location": "View3D > Sidebar > Frugal MCP",
    "description": "Let AI agents build scenes through four compact MCP tools",
    "category": "Interface",
}

PERMISSIONS = ("write", "render", "delete", "save", "python")
PRESETS = {
    "SAFE": ((("write", "render")), False),
    "BUILD": (("write", "render", "delete", "save"), True),
    "EVERYTHING": (PERMISSIONS, False),
}

_server = None
_timer = None


def prefs():
    entry = bpy.context.preferences.addons.get(__package__)
    return entry.preferences if entry else None


def allowed():
    """Permissions come from Preferences, never from the open .blend file."""
    p = prefs()
    if p is None:
        return ("write", "render")
    return tuple(key for key in PERMISSIONS if getattr(p, f"allow_{key}"))


def stop():
    global _server, _timer
    if _server is not None:
        if _timer is not None and bpy.app.timers.is_registered(_timer):
            bpy.app.timers.unregister(_timer)
        _server.close()
        _server = None
        _timer = None


def options():
    """Everything the bridge takes from Preferences when it starts."""
    p = prefs()
    return {
        "permissions": allowed(),
        "ask": bool(p and p.ask_before_risky),
        "send_dir": bpy.path.abspath(p.send_folder) if p and p.send_folder else None,
        "visible": bool(p and p.visible_mode),
        "pace": p.step_pause if p else 0.0,
    }


def redraw():
    """Show or clear the approval notice, and refresh the sidebar so the buttons appear."""
    waiting = _server.pending if _server else []
    text = "Frugal MCP is waiting for your approval: open the Frugal MCP sidebar tab" if waiting else None
    for window in bpy.context.window_manager.windows:
        window.workspace.status_text_set(text)
        for area in window.screen.areas:
            area.tag_redraw()


def start(output_dir=None, descriptor=None, permissions=("write", "render"), timer=True, **engine_options):
    global _server, _timer
    if _server is not None:
        raise RuntimeError("Frugal MCP bridge already running")
    _server = Server(Engine(output_dir or state_dir() / "exports", permissions, **engine_options), descriptor)
    _server.notify = _server.engine.refresh = redraw
    if timer:
        _timer = _server.poll
        # Persistent so a file load cannot drop the timer between load_pre and load_post.
        bpy.app.timers.register(_timer, first_interval=0.1, persistent=True)
    return _server


def close_at_exit():
    # Blender does not call unregister on quit, which would leave a stale connection file behind.
    if _server is not None:
        _server.close()


def autostart():
    p = prefs()
    if p and p.auto_start and _server is None and not bpy.app.background:
        start(**options())


@persistent
def on_load_pre(_):
    stop()


@persistent
def on_load_post(_):
    # A new file never inherits the old bridge. Only the user's own preference starts a new one.
    autostart()


def changed(self, context):
    # Blender only auto-saves preferences it knows changed, and changes made from Python
    # (the preset buttons) do not tell it. Without this the settings vanish on quit.
    context.preferences.is_dirty = True
    # A running bridge follows the user's ticks at once. Only the user can tick them: the bridge
    # gives no operation that writes preferences, and python is already full access.
    if _server is not None:
        engine, now = _server.engine, options()
        engine.permissions = set(now["permissions"])
        engine.ask, engine.visible, engine.pace = now["ask"], now["visible"], now["pace"]
        engine.send_dir = Path(now["send_dir"]) if now["send_dir"] else None
        redraw()


class COMPACT_Preferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    auto_start: bpy.props.BoolProperty(
        name="Start bridge when Blender opens",
        description="Start the bridge on launch and after each file load, with the permissions below",
        default=False,
        update=changed,
    )
    allow_write: bpy.props.BoolProperty(name="Allow write", default=True, update=changed)
    allow_render: bpy.props.BoolProperty(name="Allow render", default=True, update=changed)
    allow_delete: bpy.props.BoolProperty(name="Allow delete", default=False, update=changed)
    allow_save: bpy.props.BoolProperty(name="Allow save", default=False, update=changed)
    allow_python: bpy.props.BoolProperty(name="Allow python", default=False, update=changed)
    send_folder: bpy.props.StringProperty(
        name="Send exports to",
        description="Also copy each export step's file into this folder, for example your game project's assets folder",
        subtype="DIR_PATH",
        update=changed,
    )
    visible_mode: bpy.props.BoolProperty(
        name="Show what the agent is doing",
        description="Run batches one step at a time, selecting each object as it is changed, so you can watch",
        default=False,
        update=changed,
    )
    step_pause: bpy.props.FloatProperty(
        name="Pause between steps (seconds)",
        min=0.0,
        max=1.0,
        default=0.3,
        description="Used by visible mode",
        update=changed,
    )
    ask_before_risky: bpy.props.BoolProperty(
        name="Ask me before delete, save or python steps",
        description="Hold those steps until you press Approve in the Frugal MCP sidebar tab (denied after 2 minutes)",
        default=False,
        update=changed,
    )

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "auto_start")
        box = layout.box()
        box.label(text="Permissions, applied each time the bridge starts")
        row = box.row()
        for kind in PRESETS:
            row.operator("compact_mcp.preset", text=kind.title()).kind = kind
        for key in PERMISSIONS:
            box.prop(self, f"allow_{key}")
        box.prop(self, "ask_before_risky")
        layout.prop(self, "send_folder")
        if self.send_folder:
            layout.label(text="Files are copied there and never overwritten.")
        layout.prop(self, "visible_mode")
        if self.visible_mode:
            layout.prop(self, "step_pause")
        if self.allow_python:
            layout.label(text="Python grants unrestricted local code access", icon="ERROR")
            if self.auto_start:
                layout.label(text="With auto-start, that access is on every time Blender opens", icon="ERROR")
        notice = layout.box()
        notice.label(text='Provided "as is", without warranty, under the MIT licence.')
        notice.label(text="To the extent permitted by law, the authors are not liable for")
        notice.label(text="any loss or damage from using it. Back up your work.")
        if _server:
            layout.label(text="Bridge running; changes apply straight away")
            layout.operator("compact_mcp.stop")
        else:
            layout.operator("compact_mcp.start")


class COMPACT_OT_start(bpy.types.Operator):
    bl_idname = "compact_mcp.start"
    bl_label = "Start bridge"

    def execute(self, context):
        start(**options())
        self.report({"INFO"}, "Bridge started on loopback.")
        return {"FINISHED"}


class COMPACT_OT_preset(bpy.types.Operator):
    bl_idname = "compact_mcp.preset"
    bl_label = "Permission preset"
    bl_description = (
        "Safe: write and render. Build: adds delete and save, with approval. Everything: all five"
    )

    kind: bpy.props.EnumProperty(items=[(k, k.title(), "") for k in PRESETS])

    def execute(self, context):
        p = prefs()
        allowed_now, ask = PRESETS[self.kind]
        for key in PERMISSIONS:
            setattr(p, f"allow_{key}", key in allowed_now)
        p.ask_before_risky = ask
        return {"FINISHED"}


class COMPACT_OT_decide(bpy.types.Operator):
    bl_idname = "compact_mcp.decide"
    bl_label = "Decide"

    allow: bpy.props.BoolProperty()

    def execute(self, context):
        if _server:
            _server.decide(self.allow)
        return {"FINISHED"}


class COMPACT_OT_pause(bpy.types.Operator):
    bl_idname = "compact_mcp.pause"
    bl_label = "Pause or resume the agent"
    bl_description = "While paused, new requests are refused and nothing runs"

    def execute(self, context):
        if _server:
            _server.paused = not _server.paused
            redraw()
        return {"FINISHED"}


class COMPACT_OT_cancel(bpy.types.Operator):
    bl_idname = "compact_mcp.cancel"
    bl_label = "Stop this batch"
    bl_description = "End the running batch after the current step and undo it"

    def execute(self, context):
        if _server and _server.engine.running:
            _server.engine.running.cancelled = True
        return {"FINISHED"}


class COMPACT_OT_stop(bpy.types.Operator):
    bl_idname = "compact_mcp.stop"
    bl_label = "Stop bridge"

    def execute(self, context):
        stop()
        return {"FINISHED"}


class COMPACT_PT_panel(bpy.types.Panel):
    bl_label = "Frugal MCP"
    bl_idname = "COMPACT_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Frugal MCP"

    def draw(self, context):
        layout = self.layout
        if _server:
            run = _server.engine.running
            if _server.paused:
                layout.label(text="Paused: new requests are refused", icon="PAUSE")
            elif run:
                layout.label(text=f"Running step {run.index + 1} of {len(run.steps)}", icon="PLAY")
            else:
                layout.label(text="Connected locally; active scene access", icon="CHECKMARK")
            layout.label(text="Enabled: " + ", ".join(sorted(_server.engine.permissions)))
            row = layout.row()
            row.operator("compact_mcp.pause", text="Resume" if _server.paused else "Pause agent")
            if run:
                row.operator("compact_mcp.cancel")
            if _server.engine.log:
                box = layout.box()
                box.label(text="Recent steps")
                for entry in list(_server.engine.log)[:-9:-1]:
                    text = f"{entry['op']} {entry['name']}".strip()
                    box.label(text=entry["error"] or text, icon="ERROR" if entry["error"] else "DOT")
            for held in _server.pending:
                box = layout.box()
                box.label(text="Agent asks to run:", icon="QUESTION")
                box.label(text=held.summary)
                row = box.row()
                row.operator("compact_mcp.decide", text="Approve").allow = True
                row.operator("compact_mcp.decide", text="Deny").allow = False
            layout.operator("compact_mcp.stop")
        else:
            p = prefs()
            if p:
                for key in PERMISSIONS:
                    layout.prop(p, f"allow_{key}")
                layout.prop(p, "ask_before_risky")
                layout.prop(p, "auto_start")
            layout.label(text="Python grants unrestricted local code access")
            layout.operator("compact_mcp.start")


CLASSES = (
    COMPACT_Preferences,
    COMPACT_OT_start,
    COMPACT_OT_preset,
    COMPACT_OT_decide,
    COMPACT_OT_pause,
    COMPACT_OT_cancel,
    COMPACT_OT_stop,
    COMPACT_PT_panel,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    atexit.register(close_at_exit)
    bpy.app.handlers.load_pre.append(on_load_pre)
    bpy.app.handlers.load_post.append(on_load_post)
    # Preferences are not readable yet while an add-on registers at startup, so check a moment later.
    bpy.app.timers.register(autostart, first_interval=1.0, persistent=True)


def unregister():
    stop()
    atexit.unregister(close_at_exit)
    if bpy.app.timers.is_registered(autostart):
        bpy.app.timers.unregister(autostart)
    for handlers, handler in (
        (bpy.app.handlers.load_pre, on_load_pre),
        (bpy.app.handlers.load_post, on_load_post),
    ):
        if handler in handlers:
            handlers.remove(handler)
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)

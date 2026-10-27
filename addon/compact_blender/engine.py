"""Blender operations. Called ONLY on Blender's main thread."""

import contextlib
import math
import re
import shutil
import time
from collections import deque
from pathlib import Path

import bpy
from mathutils import Vector

from .catalog import CATALOG, PERMISSIONS, discover

OWNER = "blender_compact_mcp"
RISKY = ("delete", "save", "python", "export")
EXPORTS = {"godot": ".glb", "unity": ".fbx", "unreal": ".fbx"}
PREVIEW = re.compile(r"preview-[0-9a-f]{32}\.png")
KEEP_PREVIEWS = 20


def number(value, lo=-100000, hi=100000):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Expected finite number")
    if not math.isfinite(value) or not lo <= value <= hi:
        raise ValueError(f"Number must be finite and between {lo} and {hi}")
    return float(value)


def vector(value, length=3):
    if not isinstance(value, list) or len(value) != length:
        raise ValueError(f"Expected {length}-element numeric list")
    return [number(v) for v in value]


def name(value):
    if not isinstance(value, str) or not value or "\x00" in value or len(value.encode("utf-8")) > 255:
        raise ValueError("Name must be nonempty UTF-8 text, at most 255 bytes, without NUL")
    return value


def owned(value):
    obj = bpy.data.objects.get(name(value))
    if obj is None:
        raise ValueError(f"Object not found: {value}")
    if obj.name not in bpy.context.scene.objects:
        raise ValueError("Object is outside the active scene")
    return obj


def output_file(root, filename, suffix):
    if not isinstance(filename, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,100}", filename):
        raise ValueError("Output must be a simple filename, not a path")
    if not filename.endswith(suffix):
        raise ValueError(f"Output must end in {suffix}")
    root = Path(root).resolve()
    path = root / filename
    if path.exists() or path.is_symlink():
        raise ValueError("Refusing to overwrite an existing output")
    root.mkdir(parents=True, exist_ok=True)
    return path


def validate_step(step):
    if not isinstance(step, dict):
        raise ValueError("Each step must be an object")
    op = step.get("op")
    if op not in CATALOG:
        raise ValueError(f"Unknown operation: {op}")
    extra = set(step) - {"op"} - set(CATALOG[op]["args"])
    if extra:
        raise ValueError(f"Unknown arguments: {sorted(extra)}")
    from .extended import validate

    if validate(op, step):
        return
    required = {
        "primitive": ["name", "kind"],
        "transform": ["name"],
        "material": ["name", "color"],
        "assign_material": ["name", "material"],
        "array": ["name", "count", "offset", "prefix"],
        "light": ["name", "location", "energy"],
        "camera": ["name", "location", "target"],
        "delete": ["names", "confirm"],
        "save": ["filename"],
        "export": ["engine", "filename"],
        "sprite_sheet": ["filename"],
    }
    for key in required[op]:
        if key not in step:
            raise ValueError(f"{op} needs {key}")
    for key in ("name", "prefix", "material"):
        if key in step:
            name(step[key])
    for key in ("location", "rotation", "scale", "offset", "target"):
        if key in step:
            vector(step[key])
    if "color" in step:
        for v in vector(step["color"], 4):
            number(v, 0, 1)
    for key, lo, hi in (
        ("metallic", 0, 1),
        ("roughness", 0, 1),
        ("energy", 0, 10000),
        ("size", 0.01, 100),
        ("lens", 10, 200),
    ):
        if key in step:
            number(step[key], lo, hi)
    if op == "primitive" and step["kind"] not in ("cube", "sphere", "plane", "cylinder", "cone"):
        raise ValueError("Unknown primitive kind")
    if op == "light" and step.get("kind", "AREA") not in ("AREA", "POINT", "SUN"):
        raise ValueError("Unknown light kind")
    if op == "array" and (type(step["count"]) is not int or not 1 <= step["count"] <= 200):
        raise ValueError("count must be an integer from 1 to 200")
    if op == "sprite_sheet":
        for key, lo, hi in (("frames", 1, 64), ("columns", 1, 64), ("size", 32, 512)):
            if key in step and (type(step[key]) is not int or not lo <= step[key] <= hi):
                raise ValueError(f"{key} must be an integer from {lo} to {hi}")
    if op == "export":
        if step["engine"] not in EXPORTS:
            raise ValueError(f"engine must be one of {sorted(EXPORTS)}")
        if "names" in step:
            if not isinstance(step["names"], list) or not 1 <= len(step["names"]) <= 200:
                raise ValueError("names must contain 1..200 names")
            for v in step["names"]:
                name(v)
    if op == "delete":
        if step["confirm"] is not True:
            raise ValueError("Deletion needs confirm=true")
        if not isinstance(step["names"], list) or not 1 <= len(step["names"]) <= 200:
            raise ValueError("names must contain 1..200 names")
        for v in step["names"]:
            name(v)


def export_file(engine, path):
    if engine == "godot":
        bpy.ops.export_scene.gltf(
            filepath=path, export_format="GLB", use_selection=True, export_apply=True, export_yup=True
        )
        return
    # Unity is Y-up with -Z forward, Unreal is Z-up. Both want the scale baked into the mesh.
    axes = (
        {"axis_forward": "-Z", "axis_up": "Y"}
        if engine == "unity"
        else {"axis_forward": "-Y", "axis_up": "Z"}
    )
    bpy.ops.export_scene.fbx(
        filepath=path,
        use_selection=True,
        apply_scale_options="FBX_SCALE_ALL",
        bake_space_transform=True,
        use_mesh_modifiers=True,
        add_leaf_bones=False,
        object_types={"MESH", "EMPTY"},
        **axes,
    )


def game_checks(objects):
    """Problems a game engine would trip over. Only what is wrong is listed; triangles are before modifiers."""
    meshes = [o for o in objects if o.type == "MESH"]
    triangles, no_uv, big = 0, [], set()
    for obj in meshes:
        obj.data.calc_loop_triangles()
        triangles += len(obj.data.loop_triangles)
        if not obj.data.uv_layers:
            no_uv.append(obj.name)
        for mat in obj.data.materials:
            for node in mat.node_tree.nodes if mat and mat.node_tree else ():
                if node.type == "TEX_IMAGE" and node.image and max(node.image.size) > 2048:
                    big.add(node.image.name)
    result = {"objects": len(objects), "triangles": triangles}
    if no_uv:
        result["no_uv"] = no_uv[:10]
    if big:
        result["large_textures"] = sorted(big)[:10]
    return result


@contextlib.contextmanager
def preview_settings(scene, size, engine=None):
    """Point the scene at a small, fast render and give back (width, height). Everything is restored after."""
    render = scene.render
    attrs = (
        "filepath",
        "resolution_x",
        "resolution_y",
        "resolution_percentage",
        "engine",
        "use_file_extension",
    )
    old = {a: getattr(render, a) for a in attrs}
    old_format = render.image_settings.file_format
    # Cycles on an unlit scene yields a black frame. Prefer EEVEE for previews
    # and only use Cycles when the caller explicitly asks for it.
    if engine is None:
        engine = (
            "BLENDER_EEVEE_NEXT"
            if "BLENDER_EEVEE_NEXT"
            in {i.identifier for i in bpy.types.RenderSettings.bl_rna.properties["engine"].enum_items}
            else "BLENDER_EEVEE"
        )
    # Keep the scene's aspect ratio so previews match final framing; size is the longest edge.
    scale = size / max(render.resolution_x, render.resolution_y)
    width = max(16, round(render.resolution_x * scale))
    height = max(16, round(render.resolution_y * scale))
    samples = None
    try:
        render.engine = engine
        if render.engine == "CYCLES":
            samples = scene.cycles.samples
            scene.cycles.samples = 16
        render.resolution_x, render.resolution_y = width, height
        render.resolution_percentage = 100
        render.image_settings.file_format = "PNG"
        render.use_file_extension = True
        yield width, height
    finally:
        if samples is not None:
            scene.cycles.samples = samples
        for attr, value in old.items():
            setattr(render, attr, value)
        render.image_settings.file_format = old_format


def stitch(files, columns, rows, width, height, path):
    """Lay equally sized PNG tiles out in a grid, left to right then top to bottom, and save one PNG."""
    import numpy

    images = []
    try:
        sheet = numpy.zeros((rows * height, columns * width, 4), dtype=numpy.float32)
        sheet[..., 3] = 1
        for index, tile in enumerate(files):
            image = bpy.data.images.load(str(tile))
            images.append(image)
            pixels = numpy.empty(width * height * 4, dtype=numpy.float32)
            image.pixels.foreach_get(pixels)
            row, column = divmod(index, columns)
            top = (rows - 1 - row) * height  # image rows count from the bottom
            sheet[top : top + height, column * width : (column + 1) * width] = pixels.reshape(
                height, width, 4
            )
        out = bpy.data.images.new(".frugal-sheet", columns * width, rows * height)
        images.append(out)
        out.pixels.foreach_set(sheet.ravel())
        out.filepath_raw, out.file_format = str(path), "PNG"
        out.save()
    finally:
        for image in images:
            bpy.data.images.remove(image)


def prune_previews(root):
    """Captures made by the MCP server get a random name each time; keep only the newest few."""
    found = [p for p in Path(root).iterdir() if PREVIEW.fullmatch(p.name)]
    for path in sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)[KEEP_PREVIEWS:]:
        path.unlink(missing_ok=True)


def user_marker():
    """Changes when the user runs an operator in Blender. Operators the bridge calls are not recorded."""
    ops = bpy.context.window_manager.operators
    return len(ops), ops[-1].as_pointer() if len(ops) else 0


class Run:
    """One batch, advanced a step at a time so visible mode can redraw between steps."""

    def __init__(self, engine, steps):
        self.engine, self.steps = engine, steps
        self.index = self.completed = self.changed = 0
        self.results, self.error, self.failed_at, self.cancelled = [], None, 0, False
        # Blender's interactive undo stack is context-dependent. Where it exists, a failed batch is
        # rolled back to the checkpoint. Files are never undone, and background mode makes no promise.
        self.can_undo = not bpy.app.background and bpy.ops.ed.undo_push.poll()
        if self.can_undo:
            bpy.ops.ed.undo_push(message="Before Frugal MCP batch")
        self.started = time.monotonic()
        self.user_marker = user_marker()

    def advance(self):
        """Run one step. Returns the response once the batch is over, otherwise None."""
        if self.error is None and not self.cancelled:
            step = self.steps[self.index]
            try:
                result = self.engine.step(step)
                self.changed += result.get("changed", 0)
                if "file" in result:
                    self.results.append(result)
                if "result" in result:
                    self.results.append({"index": self.index, "result": result["result"]})
                self.completed += 1
                self.engine.note(step)
            except Exception as exc:
                self.error, self.failed_at = str(exc), self.index
                self.engine.note(step, exc)
            self.index += 1
            if self.error is None and self.index < len(self.steps):
                return None
        return self.finish()

    def finish(self):
        self.engine.running = None
        response = self.outcome()
        response["ms"] = round((time.monotonic() - self.started) * 1000)
        return response

    def outcome(self):
        if self.cancelled and self.error is None:
            self.error, self.failed_at = "Stopped by the user", self.index
        if self.error is None:
            if self.can_undo:
                bpy.ops.ed.undo_push(message="Frugal MCP batch")
            response = {
                "ok": True,
                "completed": self.completed,
                "changed": self.changed,
                "files": [r for r in self.results if "file" in r],
            }
            values = [r for r in self.results if "result" in r]
            if values:
                response["results"] = values
            return response
        response = {
            "ok": False,
            "completed": self.completed,
            "failed_index": self.failed_at,
            "error": self.error,
            "changed": self.changed,
        }
        if self.can_undo and user_marker() != self.user_marker:
            # Undo would also take back what the user did in Blender while the batch ran.
            response.update(
                atomic=False,
                rolled_back=False,
                note="Not undone because the user worked in Blender during the batch. Ask them to press Ctrl+Z.",
            )
        elif self.can_undo:
            bpy.ops.ed.undo()
            response.update(changed=0, atomic=True, rolled_back=True)
        else:
            response.update(
                atomic=False, note="Earlier operations persist; failed operation may have partial effects."
            )
        return response


class Engine:
    def __init__(
        self, output_dir, permissions=("write", "render"), ask=False, send_dir=None, visible=False, pace=0.0
    ):
        self.output_dir = Path(output_dir)
        self.permissions = set(permissions)
        self.ask = ask
        self.send_dir = Path(send_dir) if send_dir else None
        self.visible, self.pace = visible, pace
        self.running = None  # the Run in progress in visible mode
        self.log = deque(maxlen=30)  # what the agent did lately, newest last
        self.refresh = None  # set by the add-on to redraw the interface
        self.namespace = {}  # python steps share it, so later calls can reuse earlier set-up
        # Blender writes its crash log next to this session's temp folder, wherever the user set that.
        self.crash_dir = str(Path(bpy.app.tempdir).parent)

    def needs_approval(self, method, params):
        """A short description of the risky steps in a request, or None when it can run straight away."""
        steps = params.get("steps")
        if not self.ask or method != "execute" or params.get("dry_run") or not isinstance(steps, list):
            return None
        risky = [s for s in steps if isinstance(s, dict) and s.get("op") in RISKY]
        if not risky:
            return None
        text = ", ".join(sorted({s["op"] for s in risky}))
        code = next(
            (s.get("code", s.get("file")) for s in risky if s["op"] == "python" and isinstance(s.get("code", s.get("file")), str)),
            "",
        )
        return f"{text}: {code.strip()[:60]}" if code else text

    def collection(self):
        scene = bpy.context.scene
        for col in scene.collection.children:
            if col.get(OWNER) is True:
                return col
        col = bpy.data.collections.new("Frugal MCP")
        col[OWNER] = True
        scene.collection.children.link(col)
        return col

    def attach(self, obj):
        obj[OWNER] = True
        for col in list(obj.users_collection):
            col.objects.unlink(obj)
        self.collection().objects.link(obj)
        return obj

    def inspect(self, names=None, limit=20, offset=0):
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
            raise ValueError("limit must be 1..100 and offset nonnegative")
        if names is not None and (not isinstance(names, list) or len(names) > 100):
            raise ValueError("names must be a list of at most 100 names")
        if names is not None:
            for value in names:
                name(value)
        scene = bpy.context.scene
        objects = sorted((o for o in scene.objects if names is None or o.name in names), key=lambda o: o.name)
        rows = []
        for obj in objects[offset : offset + limit]:
            row = {
                "name": obj.name,
                "type": obj.type,
                "managed": obj.get(OWNER) is True,
                "location": [round(v, 4) for v in obj.location],
            }
            if names is not None:
                row.update(
                    scale=[round(v, 4) for v in obj.scale], rotation=[round(v, 4) for v in obj.rotation_euler]
                )
                if obj.type == "MESH":
                    row.update(
                        vertices=len(obj.data.vertices),
                        polygons=len(obj.data.polygons),
                        materials=[m.name if m else None for m in obj.data.materials],
                    )
            rows.append(row)
        return {
            "blender": bpy.app.version_string,
            "scene": scene.name,
            "permissions": sorted(self.permissions),
            "total": len(objects),
            "objects": rows,
            "next_offset": offset + limit if offset + limit < len(objects) else None,
        }

    def execute(self, steps, dry_run=False):
        if not isinstance(steps, list) or not 1 <= len(steps) <= 100:
            raise ValueError("steps must contain 1..100 operations")
        if type(dry_run) is not bool:
            raise ValueError("dry_run must be boolean")
        # Validate syntax and permissions for the entire batch before any writes.
        # Existence checks remain per-operation to permit references to earlier creations.
        for step in steps:
            validate_step(step)
            if PERMISSIONS[step["op"]] is not None and PERMISSIONS[step["op"]] not in self.permissions:
                raise PermissionError(f"Disabled permission: {PERMISSIONS[step['op']]}")
        if sum(s.get("count", 1) for s in steps) > 500:
            raise ValueError("Batch expands to more than 500 operations")
        if bpy.context.mode != "OBJECT" and any(s["op"] not in ("python", "frame", "rna") for s in steps):
            raise ValueError("Switch Blender to Object Mode first")
        if dry_run:
            return {"validated": len(steps), "executed": 0, "scope": "syntax and permissions only"}
        run = Run(self, steps)
        if self.visible:
            self.running = run
            return run
        while (response := run.advance()) is None:
            pass
        return response

    def note(self, step, error=None):
        self.log.append(
            {"op": step["op"], "name": step.get("name", ""), "error": str(error) if error else None}
        )
        if self.visible and error is None:
            names = {step.get("name"), *step.get("names", [])}
            bpy.context.view_layer.update()  # deleted objects linger in the layer until it refreshes
            shown = [o for o in bpy.context.view_layer.objects if o.name in names]
            for obj in bpy.context.view_layer.objects:
                obj.select_set(obj in shown)
            if shown:
                bpy.context.view_layer.objects.active = shown[0]
        if self.refresh:
            self.refresh()

    def step(self, s):
        op = s["op"]
        from .extended import run

        result = run(self, s)
        if result is not None:
            return result
        if op in ("primitive", "light", "camera") and s["name"] in bpy.data.objects:
            raise ValueError(f"Object already exists: {s['name']}")
        if op == "primitive":
            operators = {
                "cube": bpy.ops.mesh.primitive_cube_add,
                "sphere": bpy.ops.mesh.primitive_uv_sphere_add,
                "plane": bpy.ops.mesh.primitive_plane_add,
                "cylinder": bpy.ops.mesh.primitive_cylinder_add,
                "cone": bpy.ops.mesh.primitive_cone_add,
            }
            operators[s["kind"]](location=s.get("location", [0, 0, 0]))
            obj = self.attach(bpy.context.object)
            obj.name = s["name"]
            obj.scale = s.get("scale", [1, 1, 1])
        elif op == "transform":
            obj = owned(s["name"])
            for key, attr in (("location", "location"), ("rotation", "rotation_euler"), ("scale", "scale")):
                if key in s:
                    setattr(obj, attr, s[key])
        elif op == "material":
            mat = bpy.data.materials.get(s["name"])
            mat = mat or bpy.data.materials.new(s["name"])
            mat[OWNER] = True
            mat.use_nodes = True
            node = next(n for n in mat.node_tree.nodes if n.type == "BSDF_PRINCIPLED")
            node.inputs["Base Color"].default_value = s["color"]
            node.inputs["Metallic"].default_value = s.get("metallic", 0)
            node.inputs["Roughness"].default_value = s.get("roughness", 0.5)
            mat.diffuse_color = s["color"]
        elif op == "assign_material":
            obj = owned(s["name"])
            mat = bpy.data.materials.get(s["material"])
            if obj.type != "MESH" or mat is None:
                raise ValueError("Need mesh and existing material")
            # Array copies share mesh data. Detach before changing material slots.
            if obj.data.users > 1:
                obj.data = obj.data.copy()
            obj.data.materials.clear()
            obj.data.materials.append(mat)
        elif op == "array":
            obj = owned(s["name"])
            names = [f"{s['prefix']}_{i:03d}" for i in range(1, s["count"] + 1)]
            if any(len(n) > 63 or n in bpy.data.objects for n in names):
                raise ValueError("Array copy names collide or exceed 63 characters")
            for i, n in enumerate(names, 1):
                copy = obj.copy()
                copy.name = n
                copy.location = obj.location + Vector(s["offset"]) * i
                self.attach(copy)
            return {"changed": len(names)}
        elif op in ("light", "camera"):
            data = (
                bpy.data.lights.new(s["name"], s.get("kind", "AREA"))
                if op == "light"
                else bpy.data.cameras.new(s["name"])
            )
            obj = self.attach(bpy.data.objects.new(s["name"], data))
            obj.location = s["location"]
            if "target" in s:
                direction = Vector(s["target"]) - obj.location
                if direction.length > 0:
                    obj.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
            if op == "camera":
                data.lens = s.get("lens", 50)
                bpy.context.scene.camera = obj
            else:
                data.energy = s["energy"]
                if data.type == "AREA":
                    data.size = s.get("size", 5)
        elif op == "delete":
            objects = [owned(n) for n in dict.fromkeys(s["names"])]
            # Removing an object leaves its mesh, light or camera data and our materials behind.
            # Data goes first so the materials it held are seen as unused.
            data = {o.data for o in objects if o.data is not None}
            materials = {
                m for o in objects if o.type == "MESH" for m in o.data.materials if m and m.get(OWNER)
            }
            for obj in objects:
                bpy.data.objects.remove(obj, do_unlink=True)
            bpy.data.batch_remove({d for d in data if d.users == 0})
            bpy.data.batch_remove({m for m in materials if m.users == 0})
            return {"changed": len(objects)}
        elif op == "export":
            return self.export(s)
        elif op == "sprite_sheet":
            return self.sprite_sheet(s)
        elif op == "save":
            path = output_file(self.output_dir, s["filename"], ".blend")
            bpy.ops.wm.save_as_mainfile(filepath=str(path), copy=True)
            return {"changed": 0, "file": path.name}
        return {"changed": 1}

    def export(self, s):
        path = output_file(self.output_dir, s["filename"], EXPORTS[s["engine"]])
        scene = bpy.context.scene
        wanted = s.get("names")
        objects = [owned(n) for n in dict.fromkeys(wanted)] if wanted else []
        objects = objects or [o for o in scene.objects if o.get(OWNER) is True]
        if not objects:
            raise ValueError("Nothing to export: no managed objects in the scene")
        sent = None
        if self.send_dir:
            sent = self.send_dir / path.name
            if sent.exists() or not self.send_dir.is_dir():
                raise ValueError("The send folder is missing, or already has a file with that name")
        chosen = {o.name for o in objects}
        before = {o.name: o.select_get() for o in scene.objects}
        try:
            for obj in scene.objects:
                obj.select_set(obj.name in chosen)
            export_file(s["engine"], str(path))
        finally:
            for obj in scene.objects:
                obj.select_set(before.get(obj.name, False))
        if not path.is_file():
            raise RuntimeError("Export produced no file")
        if sent:
            shutil.copy2(path, sent)
        return {"changed": 0, "result": {"file": path.name, "sent": sent is not None, **game_checks(objects)}}

    def sheet(self, path, scene, width, height):
        """Front, right, top and camera views of the whole scene in one PNG, two tiles across."""
        render = scene.render
        corners = [o.matrix_world @ Vector(c) for o in scene.objects if o.type == "MESH" for c in o.bound_box]
        low = Vector(min(c[i] for c in corners) for i in range(3)) if corners else Vector()
        high = Vector(max(c[i] for c in corners) for i in range(3)) if corners else Vector()
        centre, span = (low + high) / 2, max((high - low).length, 1.0)
        # Default cameras look down -Z with Y up, which is the top view; the others are turns of that.
        views = (
            ("front", (math.pi / 2, 0, 0), (0, -1, 0)),
            ("right", (math.pi / 2, 0, math.pi / 2), (1, 0, 0)),
            ("top", (0, 0, 0), (0, 0, 1)),
        )
        original, made, files, tiles = scene.camera, [], [], []
        try:
            for label, rotation, side in views:
                data = bpy.data.cameras.new(".frugal-view")
                data.type, data.ortho_scale, data.clip_end = "ORTHO", span * 1.1, span * 8
                obj = bpy.data.objects.new(".frugal-view", data)
                scene.collection.objects.link(obj)
                obj.location, obj.rotation_euler = centre + Vector(side) * span * 2, rotation
                made.append(obj)
                tiles.append((obj, path.with_name(f".{path.stem}-{label}.png")))
            if original:
                tiles.append((original, path.with_name(f".{path.stem}-camera.png")))
            for camera, tile in tiles:
                scene.camera = camera
                render.filepath = str(tile)
                bpy.ops.render.render(write_still=True)
                files.append(tile)
            stitch(files, 2, 2, width, height, path)
        finally:
            scene.camera = original
            for obj in made:
                data = obj.data
                bpy.data.objects.remove(obj, do_unlink=True)
                bpy.data.cameras.remove(data)
            for tile in files:
                tile.unlink(missing_ok=True)

    def sprite_sheet(self, s):
        """Render evenly spaced frames of the animation range into one PNG grid, for 2D game sprites."""
        path = output_file(self.output_dir, s["filename"], ".png")
        scene = bpy.context.scene
        if scene.camera is None:
            raise ValueError("Scene has no active camera")
        count = s.get("frames", 8)
        columns = min(s.get("columns", 8), count)
        first, last = scene.frame_start, scene.frame_end
        picks = [round(first + (last - first) * i / max(count - 1, 1)) for i in range(count)]
        original, files = scene.frame_current, []
        try:
            with preview_settings(scene, s.get("size", 128)) as (width, height):
                for pick in picks:
                    scene.frame_set(pick)
                    tile = path.with_name(f".{path.stem}-{pick}.png")
                    scene.render.filepath = str(tile)
                    bpy.ops.render.render(write_still=True)
                    files.append(tile)
                rows = -(-count // columns)
                stitch(files, columns, rows, width, height, path)
        finally:
            scene.frame_set(original)
            for tile in files:
                tile.unlink(missing_ok=True)
        return {
            "changed": 0,
            "result": {"file": path.name, "width": columns * width, "height": rows * height, "frames": picks},
        }

    def capture(self, filename="preview.png", size=512, view="camera", engine=None):
        if "render" not in self.permissions:
            raise PermissionError("Disabled permission: render")
        if type(size) is not int or not 64 <= size <= 1024:
            raise ValueError("size must be an integer from 64 to 1024")
        scene = bpy.context.scene
        if view not in ("camera", "viewport", "sheet"):
            raise ValueError("view must be camera, viewport or sheet")
        if view == "viewport" and bpy.app.background:
            raise ValueError("Viewport capture requires interactive Blender")
        if view == "camera" and scene.camera is None:
            raise ValueError("Scene has no active camera")
        path = output_file(self.output_dir, filename, ".png")
        with preview_settings(scene, size, engine) as (width, height):
            scene.render.filepath = str(path)
            if view == "sheet":
                self.sheet(path, scene, width, height)
                width, height = width * 2, height * 2
            elif view == "camera":
                bpy.ops.render.render(write_still=True)
            else:
                area = next((a for a in bpy.context.screen.areas if a.type == "VIEW_3D"), None)
                if area is None:
                    raise ValueError("Open a 3D Viewport first")
                region = next(r for r in area.regions if r.type == "WINDOW")
                with bpy.context.temp_override(area=area, region=region):
                    bpy.ops.render.opengl(write_still=True, view_context=True)
            if not path.is_file():
                raise RuntimeError("Render did not produce a PNG")
            prune_previews(self.output_dir)
            return {"file": path.name, "width": width, "height": height, "bytes": path.stat().st_size}

    def dispatch(self, method, params):
        if method == "discover":
            return discover(**params)
        if method == "inspect":
            return self.inspect(**params)
        if method == "execute":
            return self.execute(**params)
        if method == "capture":
            return self.capture(**params)
        raise ValueError("Unknown method")

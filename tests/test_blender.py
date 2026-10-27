import asyncio
import json
import os
import shutil
import sys
import uuid
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from compact_mcp.client import Client

ROOT = Path(__file__).resolve().parents[1]


def create(blender, name):
    return blender.call("execute", {"steps": [{"op": "primitive", "kind": "cube", "name": name}]})


def test_real_blender_discovery_and_read(blender):
    assert "primitive" in blender.call("discover")
    assert "args" in blender.call("discover", {"operation": "array"})
    assert blender.call("inspect")["blender"]


def test_prevalidation_prevents_earlier_writes(blender):
    with pytest.raises(RuntimeError, match="Unknown operation"):
        blender.call(
            "execute",
            {"steps": [{"op": "primitive", "kind": "cube", "name": "ShouldNotExist"}, {"op": "run_python"}]},
        )
    assert blender.call("inspect", {"names": ["ShouldNotExist"]})["total"] == 0


def test_dry_run_is_explicitly_limited(blender):
    result = blender.call(
        "execute", {"steps": [{"op": "primitive", "kind": "sphere", "name": "Dry"}], "dry_run": True}
    )
    assert result["executed"] == 0
    assert blender.call("inspect", {"names": ["Dry"]})["total"] == 0


def test_runtime_partial_failure_is_reported(blender):
    result = blender.call(
        "execute",
        {
            "steps": [
                {"op": "primitive", "kind": "cube", "name": "Partial"},
                {"op": "transform", "name": "DoesNotExist", "scale": [2, 2, 2]},
            ]
        },
    )
    assert result["ok"] is False and result["completed"] == 1 and result["failed_index"] == 1
    assert blender.call("inspect", {"names": ["Partial"]})["total"] == 1


def test_real_server_replay(blender):
    rid = str(uuid.uuid4())
    params = {"steps": [{"op": "primitive", "kind": "cube", "name": "Replay"}]}
    a = blender.call("execute", params, rid)
    assert blender.call("execute", params, rid) == a
    assert blender.call("inspect", {"names": ["Replay"]})["total"] == 1
    with pytest.raises(RuntimeError, match="reused"):
        blender.call("inspect", {}, rid)


def test_array_material_copy_isolation(blender):
    result = blender.call(
        "execute",
        {
            "steps": [
                {"op": "primitive", "kind": "cube", "name": "ArraySeed"},
                {"op": "material", "name": "Red", "color": [1, 0, 0, 1]},
                {"op": "material", "name": "Blue", "color": [0, 0, 1, 1]},
                {"op": "assign_material", "name": "ArraySeed", "material": "Red"},
                {"op": "array", "name": "ArraySeed", "count": 2, "offset": [2, 0, 0], "prefix": "ArrayCopy"},
                {"op": "assign_material", "name": "ArrayCopy_001", "material": "Blue"},
            ]
        },
    )
    assert result["ok"]
    rows = {
        o["name"]: o
        for o in blender.call("inspect", {"names": ["ArraySeed", "ArrayCopy_001", "ArrayCopy_002"]})[
            "objects"
        ]
    }
    assert rows["ArraySeed"]["materials"] == ["Red"]
    assert rows["ArrayCopy_001"]["materials"] == ["Blue"]
    assert rows["ArrayCopy_002"]["location"] == [4, 0, 0]


def test_delete_confirmation_and_path_validation(blender):
    create(blender, "DeleteMe")
    with pytest.raises(RuntimeError, match="confirm"):
        blender.call("execute", {"steps": [{"op": "delete", "names": ["DeleteMe"], "confirm": False}]})
    assert blender.call("execute", {"steps": [{"op": "delete", "names": ["DeleteMe"], "confirm": True}]})[
        "ok"
    ]
    result = blender.call("execute", {"steps": [{"op": "save", "filename": "../outside.blend"}]})
    assert result["ok"] is False


def test_existing_objects_can_be_changed(blender):
    result = blender.call(
        "execute", {"steps": [{"op": "transform", "name": "UnmanagedSentinel", "location": [0, 0, 0]}]}
    )
    assert result["ok"] is True
    assert blender.call("inspect", {"names": ["UnmanagedSentinel"]})["objects"][0]["location"] == [
        0,
        0,
        0,
    ]


def test_readonly_connection_enforces_permissions(blender):
    readonly = Client(Path(blender.info["output_dir"]).parent / "readonly.json")
    assert readonly.call("inspect")["permissions"] == []
    with pytest.raises(RuntimeError, match="Disabled permission: write"):
        create(readonly, "Denied")
    with pytest.raises(RuntimeError, match="Disabled permission: render"):
        readonly.call("capture")


def test_mcp_stdio_roundtrip(blender, monkeypatch):
    # Real SDK client -> subprocess MCP bridge -> local socket -> actual Blender.
    connection = Path(blender.info["output_dir"]).parent / "connection.json"
    monkeypatch.setenv("BLENDER_COMPACT_CONNECTION", str(connection))

    async def run():
        params = StdioServerParameters(
            command=sys.executable, args=["-m", "compact_mcp.server"], env=dict(os.environ)
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert {t.name for t in tools.tools} == {"inspect", "discover", "execute", "capture"}
                result = await session.call_tool("discover", {"operation": "primitive"})
                assert not result.isError
                assert "kind" in result.content[0].text
                result = await session.call_tool(
                    "execute", {"steps": [{"op": "primitive", "kind": "cube", "name": "FromMCP"}]}
                )
                assert not result.isError
                assert json.loads(result.content[0].text)["ok"]
                result = await session.call_tool(
                    "execute",
                    {
                        "steps": [
                            {
                                "op": "camera",
                                "name": "MCPTestCamera",
                                "location": [8, -8, 8],
                                "target": [0, 0, 0],
                            }
                        ]
                    },
                )
                assert not result.isError
                result = await session.call_tool("inspect", {"names": ["FromMCP"]})
                assert not result.isError and json.loads(result.content[0].text)["total"] == 1
                result = await session.call_tool("capture", {"size": 64})
                assert not result.isError
                assert result.content[0].type == "image" and result.content[0].mimeType == "image/png"
                # Keep the exact advertised tool schemas for the reproducible overhead report.
                (ROOT / "artifacts/tool-schemas.json").write_text(
                    json.dumps([t.model_dump(exclude_none=True) for t in tools.tools], indent=2)
                )

    asyncio.run(run())


def test_capture_defaults_to_eevee_and_renders_unlit_scene(blender):
    # Regression: capture used to force CYCLES, which returns an almost entirely
    # black frame on a scene with no world lighting. A preview must stay legible.
    blender.call(
        "execute",
        {
            "steps": [
                {
                    "op": "primitive",
                    "kind": "cube",
                    "name": "EeveeProbe",
                    "location": [0, 0, 0.5],
                },
                {
                    "op": "material",
                    "name": "EeveeProbeMat",
                    "color": [0.8, 0.3, 0.1, 1],
                    "roughness": 0.5,
                },
                {"op": "assign_material", "name": "EeveeProbe", "material": "EeveeProbeMat"},
                {
                    "op": "camera",
                    "name": "EeveeProbeCam",
                    "location": [0, -6, 4],
                    "target": [0, 0, 0.5],
                },
                {
                    "op": "light",
                    "name": "EeveeProbeKey",
                    "location": [0, -4, 6],
                    "target": [0, 0, 0.5],
                    "energy": 1200,
                    "size": 4,
                },
            ]
        },
    )
    # No world is created anywhere in this test on purpose.
    image = blender.call("capture", {"filename": "eevee-probe.png", "size": 256})
    path = blender.image_path(image["file"])
    assert path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    # A black 256x256 PNG compresses far smaller than a frame containing a lit cube.
    assert image["bytes"] > 2000

    # The engine override is still honoured for callers who want ray tracing.
    cycles = blender.call("capture", {"filename": "cycles-probe.png", "size": 128, "engine": "CYCLES"})
    assert blender.image_path(cycles["file"]).read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_demo_render_and_save(blender):
    # Move prior test objects away, without deleting anything not created by these tests.
    rows = [o for o in blender.call("inspect", {"limit": 100})["objects"] if o["managed"]]
    if rows:
        result = blender.call(
            "execute",
            {
                "steps": [
                    {"op": "transform", "name": row["name"], "location": [1000, 1000, 1000]} for row in rows
                ]
            },
        )
        assert result["ok"]
    steps = [
        {"op": "material", "name": "Teal", "color": [0.03, 0.5, 0.38, 1], "metallic": 0.3, "roughness": 0.25},
        {"op": "material", "name": "GroundMat", "color": [0.08, 0.1, 0.14, 1], "roughness": 0.8},
    ]
    for row in range(5):
        steps.extend(
            [
                {
                    "op": "primitive",
                    "kind": "cube",
                    "name": f"Tile{row}",
                    "location": [-5.4, (row - 2) * 1.2, 0.4 + row * 0.1],
                    "scale": [0.45, 0.45, 0.4 + row * 0.1],
                },
                {"op": "assign_material", "name": f"Tile{row}", "material": "Teal"},
                {
                    "op": "array",
                    "name": f"Tile{row}",
                    "count": 9,
                    "offset": [1.2, 0, 0],
                    "prefix": f"Row{row}",
                },
            ]
        )
    steps.extend(
        [
            {"op": "primitive", "kind": "plane", "name": "Ground", "scale": [200, 200, 1]},
            {"op": "assign_material", "name": "Ground", "material": "GroundMat"},
            {
                "op": "camera",
                "name": "DemoCamera",
                "location": [12, -15, 13],
                "target": [0, 0, 0.5],
                "lens": 48,
            },
            {
                "op": "light",
                "name": "Key",
                "location": [0, -5, 10],
                "target": [0, 0, 0],
                "energy": 2000,
                "size": 8,
            },
            {
                "op": "light",
                "name": "Rim",
                "location": [0, 6, 8],
                "target": [0, 0, 0],
                "energy": 2500,
                "size": 6,
            },
        ]
    )
    assert blender.call("execute", {"steps": steps})["ok"]
    image = blender.call("capture", {"filename": "demo.png", "size": 768})
    path = blender.image_path(image["file"])
    assert path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert image["bytes"] > 10000
    # Factory scenes are 1920x1080; previews keep that framing with size as the longest edge.
    assert (image["width"], image["height"]) == (768, 432)
    save = blender.call("execute", {"steps": [{"op": "save", "filename": "demo.blend"}]})
    assert save["ok"]
    out = Path(blender.info["output_dir"])
    assert (out / "demo.blend").stat().st_size > 10000
    shutil.copy2(path, ROOT / "artifacts/demo.png")
    shutil.copy2(out / "demo.blend", ROOT / "artifacts/demo.blend")
    (ROOT / "artifacts/demo-steps.json").write_text(json.dumps(steps, indent=2))


def test_old_previews_are_pruned(blender):
    code = (
        "import tempfile, os\n"
        "from pathlib import Path\n"
        "from compact_blender.engine import prune_previews, KEEP_PREVIEWS\n"
        "with tempfile.TemporaryDirectory() as d:\n"
        "    for i in range(KEEP_PREVIEWS + 5):\n"
        "        p = Path(d, f'preview-{i:032x}.png'); p.write_bytes(b'x'); os.utime(p, (i, i))\n"
        "    Path(d, 'preview-mine.png').write_bytes(b'x')\n"
        "    prune_previews(d)\n"
        "    names = sorted(p.name for p in Path(d).iterdir())\n"
        "result = [len(names), 'preview-mine.png' in names, f'preview-{0:032x}.png' in names]\n"
    )
    value = blender.call("execute", {"steps": [{"op": "python", "code": code}]})["results"][0]["result"][
        "value"
    ]
    assert value == [21, True, False]


def test_delete_removes_the_data_it_leaves_behind(blender):
    def mesh_names():
        code = "result = sorted(m.name for m in bpy.data.meshes) + sorted(m.name for m in bpy.data.materials)"
        return blender.call("execute", {"steps": [{"op": "python", "code": code}]})["results"][0]["result"][
            "value"
        ]

    before = mesh_names()
    blender.call(
        "execute",
        {
            "steps": [
                {"op": "primitive", "kind": "cube", "name": "Orphaned"},
                {"op": "material", "name": "OrphanedMat", "color": [1, 0, 0, 1]},
                {"op": "assign_material", "name": "Orphaned", "material": "OrphanedMat"},
            ]
        },
    )
    assert mesh_names() != before
    blender.call("execute", {"steps": [{"op": "delete", "names": ["Orphaned"], "confirm": True}]})
    assert mesh_names() == before


def build_scene(blender, prefix):
    blender.call(
        "execute",
        {
            "steps": [
                {"op": "primitive", "kind": "cube", "name": f"{prefix}Cube", "scale": [1, 2, 3]},
                {"op": "camera", "name": f"{prefix}Cam", "location": [8, -8, 6], "target": [0, 0, 0]},
                {"op": "light", "name": f"{prefix}Sun", "location": [4, 4, 8], "energy": 3, "kind": "SUN"},
            ]
        },
    )


def exported(blender, **step):
    result = blender.call("execute", {"steps": [{"op": "export", **step}]})
    return result["results"][0]["result"]


def test_export_writes_engine_ready_files_and_reports_checks(blender):
    build_scene(blender, "Ex")
    folder = Path(blender.info["output_dir"])
    godot = exported(blender, engine="godot", filename="ex.glb", names=["ExCube"])
    assert godot["file"] == "ex.glb" and godot["triangles"] == 12 and godot["objects"] == 1
    assert (folder / "ex.glb").read_bytes()[:4] == b"glTF"
    unity = exported(blender, engine="unity", filename="ex-unity.fbx", names=["ExCube"])
    assert unity["objects"] == 1 and (folder / "ex-unity.fbx").read_bytes().startswith(b"Kaydara FBX Binary")
    unreal = exported(blender, engine="unreal", filename="ex-unreal.fbx", names=["ExCube"])
    assert unreal["triangles"] == 12 and (folder / "ex-unreal.fbx").stat().st_size > 0


def test_export_refuses_wrong_extension_unknown_engine_and_overwrite(blender):
    def run(**step):
        return blender.call("execute", {"steps": [{"op": "export", "names": ["Ex2Cube"], **step}]})

    build_scene(blender, "Ex2")
    with pytest.raises(RuntimeError, match="engine must be one of"):
        run(engine="blender", filename="a.glb")
    assert "must end in .glb" in run(engine="godot", filename="a.fbx")["error"]
    assert run(engine="godot", filename="once.glb")["ok"]
    assert "overwrite" in run(engine="godot", filename="once.glb")["error"]


def test_export_copies_to_the_send_folder_without_overwriting(blender, tmp_path):
    build_scene(blender, "Ex3")
    code = (
        "import tempfile\n"
        "from pathlib import Path\n"
        "from compact_blender.engine import Engine\n"
        "send, out = Path(params['send']), Path(params['out'])\n"
        "engine = Engine(out, ('save',), send_dir=send)\n"
        "step = {'op': 'export', 'engine': 'godot', 'filename': 'sent.glb', 'names': ['Ex3Cube']}\n"
        "first = engine.step(step)['result']['sent']\n"
        "step['filename'] = 'sent2.glb'\n"
        "(send / 'sent2.glb').write_bytes(b'mine')\n"
        "try:\n"
        "    engine.step(step)\n"
        "    refused = False\n"
        "except ValueError:\n"
        "    refused = True\n"
        "result = [first, (send / 'sent.glb').is_file(), refused, (send / 'sent2.glb').read_bytes() == b'mine']\n"
    )
    params = {"send": str(tmp_path / "game"), "out": str(tmp_path / "out")}
    (tmp_path / "game").mkdir()
    value = blender.call("execute", {"steps": [{"op": "python", "code": code, "params": params}]})["results"][
        0
    ]["result"]["value"]
    assert value == [True, True, True, True]


def test_capture_sheet_is_one_image_with_four_tiles(blender):
    build_scene(blender, "Sh")
    result = blender.call("capture", {"filename": "sheet-test.png", "size": 128, "view": "sheet"})
    from struct import unpack

    data = (Path(blender.info["output_dir"]) / result["file"]).read_bytes()
    width, height = unpack(">II", data[16:24])
    assert (width, height) == (result["width"], result["height"]) and width == 2 * (width // 2)
    assert (
        result["width"] >= 2 * 100
    )  # two 128-pixel-long tiles across, scene aspect keeps the long edge at 128


def python_value(blender, code):
    return blender.call("execute", {"steps": [{"op": "python", "code": code}]})["results"][0]["result"][
        "value"
    ]


def test_parent_keeps_world_position_and_can_be_cleared(blender):
    blender.call(
        "execute",
        {
            "steps": [
                {"op": "primitive", "kind": "cube", "name": "Par", "location": [5, 0, 0]},
                {"op": "primitive", "kind": "cube", "name": "Kid", "location": [0, 7, 0]},
                {"op": "parent", "name": "Kid", "parent": "Par"},
            ]
        },
    )
    code = "result = [bpy.data.objects['Kid'].parent.name, list(bpy.data.objects['Kid'].matrix_world.translation)]"
    assert python_value(blender, code) == ["Par", [0, 7, 0]]
    blender.call("execute", {"steps": [{"op": "parent", "name": "Kid"}]})
    code = "result = [bpy.data.objects['Kid'].parent, list(bpy.data.objects['Kid'].matrix_world.translation)]"
    assert python_value(blender, code) == [None, [0, 7, 0]]


def test_collection_moves_objects_and_refuses_foreign_collections(blender):
    blender.call(
        "execute",
        {
            "steps": [
                {"op": "primitive", "kind": "cube", "name": "InGroup"},
                {"op": "collection", "name": "Props", "objects": ["InGroup"]},
            ]
        },
    )
    code = "result = [c.name for c in bpy.data.objects['InGroup'].users_collection]"
    assert python_value(blender, code) == ["Props"]
    python_value(blender, "bpy.data.collections.new('Mine'); result = 1")
    refused = blender.call(
        "execute", {"steps": [{"op": "collection", "name": "Mine", "objects": ["InGroup"]}]}
    )
    assert refused["ok"] is False and "not made by this add-on" in refused["error"]


def test_world_and_lighting(blender):
    blender.call(
        "execute",
        {
            "steps": [
                {"op": "world", "color": [0.1, 0.2, 0.3], "strength": 0.5},
                {"op": "lighting", "prefix": "Rig", "target": [0, 0, 1], "distance": 9, "energy": 500},
            ]
        },
    )
    code = (
        "node = next(n for n in bpy.context.scene.world.node_tree.nodes if n.type == 'BACKGROUND')\n"
        "rig = sorted(o.name for o in bpy.data.objects if o.name.startswith('Rig_'))\n"
        "result = [round(node.inputs['Strength'].default_value, 2), rig, bpy.data.objects['Rig_fill'].data.energy]"
    )
    assert python_value(blender, code) == [0.5, ["Rig_fill", "Rig_key", "Rig_rim"], 200.0]
    again = blender.call("execute", {"steps": [{"op": "lighting", "prefix": "Rig"}]})
    assert again["ok"] is False and "already exists" in again["error"]


def test_sprite_sheet_renders_frames_into_one_grid(blender):
    from struct import unpack

    build_scene(blender, "Sp")
    blender.call("execute", {"steps": [{"op": "frame", "start": 1, "end": 10}]})
    result = blender.call(
        "execute",
        {"steps": [{"op": "sprite_sheet", "filename": "sprites.png", "frames": 6, "columns": 3, "size": 64}]},
    )["results"][0]["result"]
    assert result["frames"] == [1, 3, 5, 6, 8, 10]
    data = (Path(blender.info["output_dir"]) / "sprites.png").read_bytes()
    assert unpack(">II", data[16:24]) == (result["width"], result["height"])
    assert result["width"] == 3 * (result["width"] // 3) and result["height"] == 2 * (result["height"] // 2)


def python(blender, **step):
    return blender.call("execute", {"steps": [{"op": "python", **step}]})


def test_python_output_is_quiet_unless_needed(blender):
    loud = "print('importer noise ' * 50)\n"
    assert "stdout" not in python(blender, code=loud + "result = 1")["results"][0]["result"]
    assert "importer noise" in python(blender, code=loud)["results"][0]["result"]["stdout"]
    failed = python(blender, code="print('before it broke')\nraise ValueError('boom')")
    assert failed["ok"] is False
    assert failed["error"] == "boom\nLast output:\nbefore it broke"


def test_python_runs_a_file_and_keeps_values_between_calls(blender, tmp_path):
    script = tmp_path / "setup.py"
    script.write_text("rig = {'gap': params['gap']}\nresult = __file__.endswith('setup.py')\n")
    first = python(blender, file=str(script), params={"gap": 4})
    assert first["results"][0]["result"]["value"] is True
    assert isinstance(first["ms"], int)
    assert python(blender, code="result = rig['gap']")["results"][0]["result"]["value"] == 4
    with pytest.raises(RuntimeError, match="absolute path"):
        python(blender, file="setup.py")
    with pytest.raises(RuntimeError, match="not both"):
        python(blender, code="1", file=str(script))

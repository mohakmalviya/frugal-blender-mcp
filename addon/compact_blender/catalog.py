"""Small operation vocabulary; only discovered schemas enter the agent context."""

from .extended import OPERATIONS

CATALOG = {
    "primitive": {
        "summary": "Create a managed mesh",
        "args": {
            "name": "unique string",
            "kind": "cube|sphere|plane|cylinder|cone",
            "location": "[x,y,z], default [0,0,0]",
            "scale": "[x,y,z], default [1,1,1]",
        },
    },
    "transform": {
        "summary": "Transform one managed object",
        "args": {
            "name": "existing managed name",
            "location": "optional [x,y,z]",
            "rotation": "optional Euler radians [x,y,z]",
            "scale": "optional [x,y,z]",
        },
    },
    "material": {
        "summary": "Create/update a managed Principled material",
        "args": {
            "name": "material name",
            "color": "RGBA [0..1]",
            "metallic": "0..1 default 0",
            "roughness": "0..1 default 0.5",
        },
    },
    "assign_material": {
        "summary": "Replace a managed mesh's material slots",
        "args": {"name": "managed mesh name", "material": "managed material name"},
    },
    "array": {
        "summary": "Create linked copies along an offset in ONE operation",
        "args": {
            "name": "managed source",
            "count": "1..200 copies, excludes source",
            "offset": "[x,y,z] step",
            "prefix": "unique copy prefix",
        },
    },
    "light": {
        "summary": "Create managed light",
        "args": {
            "name": "unique name",
            "kind": "AREA|POINT|SUN default AREA",
            "energy": "0..10000",
            "location": "[x,y,z]",
            "target": "optional aim point [x,y,z]",
            "size": "0.01..100",
        },
    },
    "camera": {
        "summary": "Create managed camera and make it active",
        "args": {
            "name": "unique name",
            "location": "[x,y,z]",
            "target": "aim point [x,y,z]",
            "lens": "10..200 mm default 50",
        },
    },
    "delete": {
        "summary": "Delete managed objects and their unused data; requires delete permission AND confirm",
        "args": {
            "names": "list of up to 200 managed names",
            "confirm": "true; may wait for approval in Blender",
        },
    },
    "sprite_sheet": {
        "summary": "Render evenly spaced animation frames into one PNG grid for 2D sprites; blocking",
        "args": {
            "filename": "basename ending .png",
            "frames": "1..64, default 8, spread over the frame range",
            "columns": "1..64, default 8",
            "size": "32..512 longest edge per frame, default 128",
        },
    },
    "export": {
        "summary": "Export objects for godot, unity or unreal with the right axes and scale, and report game-ready checks",
        "args": {
            "engine": "godot (.glb) | unity (.fbx, Y-up) | unreal (.fbx, Z-up)",
            "filename": "basename ending .glb for godot, .fbx for unity and unreal; existing files refused",
            "names": "optional list of object names, default all managed objects",
        },
    },
    "save": {
        "summary": "Save a copy under the configured output directory; existing files refused",
        "args": {"filename": "basename ending .blend; may wait for approval in Blender"},
    },
}

CATALOG.update(OPERATIONS)
for entry in CATALOG.values():
    entry["summary"] = entry["summary"].replace("managed ", "")
    entry["args"] = {k: v.replace("managed ", "") for k, v in entry["args"].items()}

PERMISSIONS = {
    name: ("delete" if name == "delete" else "save" if name == "save" else "write") for name in CATALOG
}
PERMISSIONS.update(python="python", render="render", rna=None, export="save", sprite_sheet="render")


def discover(operation=None):
    if operation is None:
        return {name: entry["summary"] for name, entry in CATALOG.items()}
    if operation not in CATALOG:
        raise ValueError(f"Unknown operation: {operation}")
    return CATALOG[operation]

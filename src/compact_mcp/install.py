"""Install the bundled Blender add-on, or write it as a ZIP for Install from Disk."""

import os
import re
import shutil
import sys
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ADDON = "compact_blender"


def addon_source():
    """The add-on ships inside the wheel; a source checkout keeps it at the repository root."""
    here = Path(__file__).resolve().parent
    for candidate in (here / "addon" / ADDON, here.parents[1] / "addon" / ADDON):
        if (candidate / "__init__.py").is_file():
            return candidate
    raise FileNotFoundError("Bundled Blender add-on not found; reinstall frugal-blender-mcp")


def config_root():
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming")) / "Blender Foundation/Blender"
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Blender"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "blender"


def blender_versions(root=None):
    """Blender user config folders such as 5.1, newest first."""
    root = Path(root) if root else config_root()
    if not root.is_dir():
        return []
    found = [p for p in root.iterdir() if p.is_dir() and re.fullmatch(r"\d+\.\d+", p.name)]
    return sorted(found, key=lambda p: tuple(int(part) for part in p.name.split(".")), reverse=True)


def write_zip(target):
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(target, "w", ZIP_DEFLATED) as archive:
        for path in sorted(addon_source().glob("*.py")):
            archive.write(path, f"{ADDON}/{path.name}")
    return target


def write_extension_zip(target):
    """The same add-on as a Blender 4.2+ extension: the manifest sits at the root of the ZIP."""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    source = addon_source()
    with ZipFile(target, "w", ZIP_DEFLATED) as archive:
        for path in [*sorted(source.glob("*.py")), source / "blender_manifest.toml"]:
            archive.write(path, path.name)
    return target


def install(version=None, root=None):
    """Copy the add-on into a Blender user scripts folder. Returns the destination."""
    versions = blender_versions(root)
    if version:
        versions = [v for v in versions if v.name == version]
    if not versions:
        wanted = f"Blender {version}" if version else "a Blender user config folder"
        raise FileNotFoundError(
            f"Could not find {wanted} under {Path(root) if root else config_root()}. "
            "Open Blender once, or use --zip and install it from Preferences > Add-ons."
        )
    destination = versions[0] / "scripts" / "addons" / ADDON
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    for path in addon_source().glob("*.py"):
        shutil.copy2(path, destination / path.name)
    return destination

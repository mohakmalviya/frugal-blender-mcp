"""Build an installable Blender add-on ZIP, excluding caches and runtime state."""

import hashlib
from pathlib import Path

from compact_mcp import __version__
from compact_mcp.install import write_extension_zip, write_zip

dist = Path(__file__).resolve().parents[1] / "dist"
target = write_zip(dist / f"frugal-blender-mcp-addon-{__version__}.zip")
extension = write_extension_zip(dist / f"frugal-blender-mcp-extension-{__version__}.zip")
(dist / "SHA256SUMS").write_text(
    "".join(f"{hashlib.sha256(z.read_bytes()).hexdigest()}  {z.name}\n" for z in (target, extension))
)
print(target)
print(extension)

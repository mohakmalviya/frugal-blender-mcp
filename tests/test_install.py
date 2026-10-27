from zipfile import ZipFile

import pytest

from compact_mcp import install


def test_versions_sort_numerically_and_ignore_other_folders(tmp_path):
    for name in ("4.2", "5.9", "5.10", "config", "5.1-backup"):
        (tmp_path / name).mkdir()
    assert [p.name for p in install.blender_versions(tmp_path)] == ["5.10", "5.9", "4.2"]


def test_install_targets_newest_blender_and_replaces_old_copy(tmp_path):
    (tmp_path / "4.2").mkdir()
    stale = tmp_path / "5.1/scripts/addons/compact_blender"
    stale.mkdir(parents=True)
    (stale / "old.py").write_text("")
    destination = install.install(root=tmp_path)
    assert destination == stale
    assert (destination / "__init__.py").is_file()
    assert not (destination / "old.py").exists()


def test_install_specific_version(tmp_path):
    (tmp_path / "4.2").mkdir()
    (tmp_path / "5.1").mkdir()
    assert install.install("4.2", root=tmp_path).parts[-4] == "4.2"


def test_install_without_blender_explains_zip_fallback(tmp_path):
    with pytest.raises(FileNotFoundError, match="--zip"):
        install.install(root=tmp_path / "missing")


def test_zip_contains_only_addon_sources(tmp_path):
    with ZipFile(install.write_zip(tmp_path / "addon.zip")) as archive:
        names = archive.namelist()
    assert "compact_blender/__init__.py" in names
    assert all(name.startswith("compact_blender/") and name.endswith(".py") for name in names)


def test_extension_zip_has_the_manifest_at_its_root_and_matches_the_package_version(tmp_path):
    import tomllib

    from compact_mcp import __version__

    with ZipFile(install.write_extension_zip(tmp_path / "ext.zip")) as archive:
        names = archive.namelist()
        manifest = tomllib.loads(archive.read("blender_manifest.toml").decode())
    assert "__init__.py" in names and all("/" not in name for name in names)
    assert manifest["version"] == __version__ and manifest["id"] == "compact_blender"

import json, tempfile
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
import modpacker

def test_launcher_integration_injects_and_backup():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        mc = td / ".minecraft"
        mc.mkdir()
        # existing profiles
        orig = {"profiles": {"existing": {"name":"Old","gameDir":"/old","lastVersionId":"1.19","type":"custom","created":"2020-01-01T00:00:00.000Z","lastUsed":"2020-01-01T00:00:00.000Z","icon":"Grass"}}, "settings":{}}
        (mc/"launcher_profiles.json").write_text(json.dumps(orig))
        profile = modpacker.LauncherProfile(name="Test Pack", gameDir=str(mc/"profiles"/"test-pack"), lastVersionId="fabric-loader-0.15-1.21.1")
        key = modpacker.inject_launcher_profile(mc, "test-pack", profile, dry_run=False)
        data = json.loads((mc/"launcher_profiles.json").read_text())
        assert key in data["profiles"]
        assert data["profiles"][key]["name"] == "Test Pack"
        assert data["profiles"]["existing"]["name"] == "Old"  # preserved
        assert (mc/"launcher_profiles.json.bak").exists()
        # second inject should reuse same key (deterministic uuid5) and preserve created
        created_first = data["profiles"][key]["created"]
        profile2 = modpacker.LauncherProfile(name="Test Pack", gameDir=str(mc/"profiles"/"test-pack"), lastVersionId="fabric-loader-0.15-1.21.1")
        key2 = modpacker.inject_launcher_profile(mc, "test-pack", profile2, dry_run=False)
        assert key == key2
        data2 = json.loads((mc/"launcher_profiles.json").read_text())
        assert data2["profiles"][key]["created"] == created_first

def test_launcher_dry_run_no_write():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        mc = td / ".minecraft"
        mc.mkdir()
        orig = {"profiles": {}}
        (mc/"launcher_profiles.json").write_text(json.dumps(orig))
        profile = modpacker.LauncherProfile(name="Dry", gameDir=str(mc/"profiles"/"dry"), lastVersionId="1.21.1")
        modpacker.inject_launcher_profile(mc, "dry", profile, dry_run=True)
        data = json.loads((mc/"launcher_profiles.json").read_text())
        assert "dry" not in str(data)  # not injected
        assert len(data["profiles"]) == 0

def test_launcher_creates_if_missing():
    with tempfile.TemporaryDirectory() as td:
        td=Path(td)/"mc"
        td.mkdir(parents=True)
        profile = modpacker.LauncherProfile(name="New", gameDir=str(td/"profiles"/"new"), lastVersionId="1.21.1")
        key = modpacker.inject_launcher_profile(td, "new", profile, dry_run=False)
        assert (td/"launcher_profiles.json").exists()
        data=json.loads((td/"launcher_profiles.json").read_text())
        assert key in data["profiles"]

def test_minecraft_dir_detection():
    assert modpacker.get_minecraft_dir("/tmp/custom") == Path("/tmp/custom")
    # auto detect returns Path
    auto = modpacker.get_minecraft_dir()
    assert isinstance(auto, Path)

def test_url_parsing():
    slug, ver, typ = modpacker.parse_modrinth_url("https://modrinth.com/modpack/fabulously-optimized")
    assert slug == "fabulously-optimized"
    assert ver is None
    slug, ver, _ = modpacker.parse_modrinth_url("https://modrinth.com/modpack/ovo/version/1.2.3")
    assert slug == "ovo"
    assert ver == "1.2.3"
    try:
        modpacker.parse_modrinth_url("https://example.com/not-modrinth")
        assert False
    except modpacker.ModrinthError:
        pass
    # direct mrpack
    slug, ver, typ = modpacker.parse_modrinth_url("https://cdn.modrinth.com/data/abc/versions/xyz/pack.mrpack")
    assert typ == "direct"

def test_installer_dry_run_offline():
    # Use synthetic mrpack with no files, test dry-run does not hit network if we mock vanilla? Actually installer will try to ensure vanilla but dry-run skips network?
    # We'll test with dry_run_preview path by calling install_from_version directly with a fake index requiring network? Instead test the _install_from_index directly dry_run path
    import tempfile, json
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        mc = td / "mc"
        mc.mkdir()
        # Create minimal mrpack
        idx = {
            "formatVersion":1,"game":"minecraft","versionId":"v1","name":"Dry Pack",
            "files":[], "dependencies":{"minecraft":"1.21.1","fabric-loader":"0.15.7"}
        }
        # Need to simulate mrpack file
        mrpack_path = td / "pack.mrpack"
        import zipfile
        with zipfile.ZipFile(mrpack_path, "w") as z:
            z.writestr("modrinth.index.json", json.dumps(idx))
            z.writestr("overrides/config/test.toml", b"a=1")
        parser = modpacker.MrpackParser(mrpack_path)
        index, overrides_tmp = parser.parse()
        proj = modpacker.ModrinthProject(id="testid", slug="dry-pack", title="Dry Pack", description="", categories=[], author="", icon_url=None, versions=[], game_versions=["1.21.1"], loaders=["fabric"], raw={})
        vers = modpacker.ModrinthVersion(id="v1", project_id="testid", name="Dry Pack", version_number="1.0", changelog=None, date_published="2026-01-01T00:00:00Z", game_versions=["1.21.1"], loaders=["fabric"], featured=False, files=[], dependencies={"minecraft":"1.21.1","fabric-loader":"0.15.7"}, raw={})
        installer = modpacker.ModpackInstaller(minecraft_dir=mc, dry_run=True)
        pack = installer._install_from_index(index, overrides_tmp, proj, vers, mrpack_path, game_dir_custom=None)
        assert pack.name == "Dry Pack"
        assert pack.minecraft_version == "1.21.1"
        # dry_run should not create launcher_profiles.json modification
        assert not (mc / "launcher_profiles.json").exists() or "Dry Pack" not in (mc / "launcher_profiles.json").read_text() if (mc/"launcher_profiles.json").exists() else True
        # gameDir should not be created? Actually dry_run still logs but not mkdir; but _install_from_index with dry_run should not mkdir
        # It does mkdir only if not dry_run; so not created
        assert not (mc / "profiles" / "dry-pack" / "config" / "test.toml").exists()

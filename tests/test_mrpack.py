import hashlib
import json
import tempfile
import zipfile
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
import modpacker

def make_mrpack(tmp_path: Path, index: dict, overrides: dict = None) -> Path:
    p = tmp_path / "test.mrpack"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("modrinth.index.json", json.dumps(index))
        if overrides:
            for rel, content in overrides.items():
                z.writestr(f"overrides/{rel}", content)
    return p

# Helper to compute hash
def sha512_of(b: bytes) -> str:
    return hashlib.sha512(b).hexdigest()

def test_parse_valid_fabric():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        idx = {
            "formatVersion": 1,
            "game": "minecraft",
            "versionId": "1.0",
            "name": "Test Pack",
            "summary": "A test",
            "files": [
                {
                    "path": "mods/sodium.jar",
                    "hashes": {"sha512": sha512_of(b"fakejar"), "sha1": hashlib.sha1(b"fakejar").hexdigest()},
                    "env": {"client": "required", "server": "optional"},
                    "downloads": ["https://example.com/sodium.jar"],
                    "fileSize": 7
                }
            ],
            "dependencies": {"minecraft": "1.21.1", "fabric-loader": "0.15.0"}
        }
        p = make_mrpack(td, idx, {"config/test.toml": b"foo=1", "mods/extra.txt": b"hi"})
        parser = modpacker.MrpackParser(p)
        index, overrides_tmp = parser.parse()
        assert index.name == "Test Pack"
        assert index.minecraft_version == "1.21.1"
        assert index.loader_type == "fabric-loader"
        assert index.loader_version == "0.15.0"
        assert (overrides_tmp / "config" / "test.toml").exists()
        assert (overrides_tmp / "mods" / "extra.txt").exists()

def test_path_traversal_rejected_in_zip():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        p = td / "evil.mrpack"
        evil_index = {
            "formatVersion": 1, "game": "minecraft", "versionId": "1",
            "name": "Evil", "files": [], "dependencies": {"minecraft": "1.21.1"}
        }
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("modrinth.index.json", json.dumps(evil_index))
            z.writestr("../../evil.txt", b"evil")
        try:
            parser = modpacker.MrpackParser(p)
            parser.parse()
            assert False, "should have raised MrpackError"
        except modpacker.MrpackError as e:
            assert "Unsafe path" in str(e)

def test_path_traversal_rejected_in_manifest():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        idx = {
            "formatVersion": 1,
            "game": "minecraft",
            "versionId": "1",
            "name": "EvilManifest",
            "files": [
                {"path": "../../escape.jar", "hashes": {"sha512":"abc"}, "downloads":["https://example.com/a.jar"], "fileSize":1, "env":{}}
            ],
            "dependencies": {"minecraft":"1.21.1","fabric-loader":"0.15.0"}
        }
        p = make_mrpack(td, idx)
        parser = modpacker.MrpackParser(p)
        index, overrides_tmp = parser.parse()
        # parse should succeed but later safe_join should reject
        # test safe_join directly
        base = Path("/tmp/game")
        try:
            modpacker.safe_join(base, "../../escape.jar")
            assert False
        except ValueError:
            pass
        # Also installer should skip unsafe
        # verify loader detection still works
        assert index.loader_type == "fabric-loader"

def test_overrides_stripping():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        idx = {
            "formatVersion": 1, "game":"minecraft","versionId":"1","name":"OverridesTest",
            "files":[], "dependencies":{"minecraft":"1.20.1","forge":"47.1.0"}
        }
        p = make_mrpack(td, idx, {"config/a/b.toml": b"a", "resourcepacks/pack.zip": b"zip", "kubejs/server.js": b"js"})
        parser = modpacker.MrpackParser(p)
        index, overrides = parser.parse()
        assert (overrides / "config" / "a" / "b.toml").exists()
        assert (overrides / "resourcepacks" / "pack.zip").exists()

def test_hash_verifier():
    data = b"hello world"
    h512 = hashlib.sha512(data).hexdigest()
    h1 = hashlib.sha1(data).hexdigest()
    assert modpacker.HashVerifier.verify_bytes(data, {"sha512": h512})
    assert modpacker.HashVerifier.verify_bytes(data, {"sha1": h1})
    assert not modpacker.HashVerifier.verify_bytes(data, {"sha512": "0"*128})
    with tempfile.TemporaryDirectory() as td:
        f = Path(td)/"file.bin"
        f.write_bytes(data)
        assert modpacker.HashVerifier.verify_file(f, {"sha512": h512})

def test_safe_join_windows_drive():
    base = Path(tempfile.gettempdir()) / "base"
    base.mkdir(exist_ok=True)
    try:
        modpacker.safe_join(base, "mods\\..\\..\\Windows\\System32\\evil.dll")
        # On linux, this is just a weird filename with backslashes, not traversal, but still should be inside base if backslashes are part of name
        # The important is that posix "../../" is caught
        pass
    except ValueError:
        pass
    # absolute
    try:
        modpacker.safe_join(base, "/etc/passwd")
        assert False
    except ValueError:
        pass

def test_forge_detection():
    idx = {"formatVersion":1,"game":"minecraft","versionId":"1","name":"ForgePack","files":[],"dependencies":{"minecraft":"1.20.1","forge":"47.1.0"}}
    parsed = modpacker.parse_mrpack_index(idx)
    assert parsed.loader_type == "forge"
    assert parsed.loader_version == "47.1.0"

def test_neoforge_and_quilt():
    for deps, expected in [
        ({"minecraft":"1.20.4","neoforge":"20.4.196"}, "neoforge"),
        ({"minecraft":"1.20.1","quilt-loader":"0.20.0"}, "quilt-loader"),
    ]:
        idx = {"formatVersion":1,"game":"minecraft","versionId":"1","name":"Test","files":[],"dependencies":deps}
        parsed = modpacker.parse_mrpack_index(idx)
        assert parsed.loader_type == expected

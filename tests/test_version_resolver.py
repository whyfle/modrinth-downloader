from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
import modpacker

def test_find_best_version_prefers_featured():
    v1 = modpacker.ModrinthVersion(id="1", project_id="p", name="a", version_number="1.0", changelog=None, date_published="2026-01-01T00:00:00Z", game_versions=["1.21.1"], loaders=["fabric"], featured=False, files=[], dependencies={}, raw={})
    v2 = modpacker.ModrinthVersion(id="2", project_id="p", name="b", version_number="1.1", changelog=None, date_published="2026-02-01T00:00:00Z", game_versions=["1.21.1"], loaders=["fabric"], featured=True, files=[], dependencies={}, raw={})
    v3 = modpacker.ModrinthVersion(id="3", project_id="p", name="c", version_number="1.2", changelog=None, date_published="2026-03-01T00:00:00Z", game_versions=["1.21.1"], loaders=["fabric"], featured=False, files=[], dependencies={}, raw={})
    client = modpacker.ModrinthClient()
    best = client.find_best_version([v1,v2,v3])
    assert best.id == "2"  # featured
    # if no featured, pick latest
    best2 = client.find_best_version([v1,v3])
    assert best2.id == "3"

def test_minecraft_version_and_loader_extraction():
    idx = {"formatVersion":1,"game":"minecraft","versionId":"1","name":"X","files":[],"dependencies":{"minecraft":"1.21.1","fabric-loader":"0.15.0"}}
    parsed = modpacker.parse_mrpack_index(idx)
    assert parsed.minecraft_version == "1.21.1"
    assert parsed.loader_type == "fabric-loader"
    assert parsed.loader_version == "0.15.0"
    # unknown
    idx2 = {"formatVersion":1,"game":"minecraft","versionId":"1","name":"X","files":[],"dependencies":{"minecraft":"1.20.1"}}
    parsed2 = modpacker.parse_mrpack_index(idx2)
    assert parsed2.loader_type == "unknown"

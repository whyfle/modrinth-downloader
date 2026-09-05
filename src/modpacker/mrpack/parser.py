from pathlib import Path
import importlib.util
_spec = importlib.util.spec_from_file_location("modpacker_standalone", str(Path(__file__).parent.parent.parent.parent / "modpacker.py"))
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
MrpackParser = _mod.MrpackParser
MrpackIndex = _mod.MrpackIndex
MrpackFile = _mod.MrpackFile
parse_mrpack_index = _mod.parse_mrpack_index

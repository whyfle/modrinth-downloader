from pathlib import Path
import importlib.util
_spec = importlib.util.spec_from_file_location("modpacker_standalone", str(Path(__file__).parent.parent.parent.parent / "modpacker.py"))
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
InstalledPack = _mod.InstalledPack
load_installation_state = _mod.load_installation_state
save_installation_state = _mod.save_installation_state

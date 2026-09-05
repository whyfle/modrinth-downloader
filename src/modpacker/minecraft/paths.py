from pathlib import Path
import importlib.util
_spec = importlib.util.spec_from_file_location("modpacker_standalone", str(Path(__file__).parent.parent.parent.parent / "modpacker.py"))
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
get_minecraft_dir = _mod.get_minecraft_dir
get_profiles_dir = _mod.get_profiles_dir
is_launcher_running = _mod.is_launcher_running

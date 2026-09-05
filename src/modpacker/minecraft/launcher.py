from pathlib import Path
import importlib.util
_spec = importlib.util.spec_from_file_location("modpacker_standalone", str(Path(__file__).parent.parent.parent.parent / "modpacker.py"))
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
LauncherProfile = _mod.LauncherProfile
load_launcher_profiles = _mod.load_launcher_profiles
inject_launcher_profile = _mod.inject_launcher_profile
backup_launcher_profiles = _mod.backup_launcher_profiles

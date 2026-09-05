"""Modpacker modular package — thin re-exports over standalone modpacker.py."""
from pathlib import Path as _P
import importlib.util as _ilu

_spec = _ilu.spec_from_file_location("modpacker_standalone", str(_P(__file__).parent.parent.parent / "modpacker.py"))
_mod = _ilu.module_from_spec(_spec)
assert _spec and _spec.loader
_spec.loader.exec_module(_mod)  # type: ignore

__version__ = _mod.APP_VERSION
__all__ = ["__version__"]
# Re-export public symbols for modular imports
ModpackInstaller = _mod.ModpackInstaller
ModrinthClient = _mod.ModrinthClient
MrpackParser = _mod.MrpackParser
DownloadManager = _mod.DownloadManager
safe_join = _mod.safe_join

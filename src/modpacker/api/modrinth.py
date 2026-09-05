"""Modular shim — delegates to standalone modpacker.py"""
from pathlib import Path
import importlib.util
_spec = importlib.util.spec_from_file_location("modpacker_standalone", str(Path(__file__).parent.parent.parent.parent / "modpacker.py"))
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
ModrinthClient = _mod.ModrinthClient
ModrinthProject = _mod.ModrinthProject
ModrinthVersion = _mod.ModrinthVersion
parse_modrinth_url = _mod.parse_modrinth_url

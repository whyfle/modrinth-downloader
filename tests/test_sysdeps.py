"""Tests for system-dependency handling (vista / native package managers)."""
import subprocess
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
import modpacker


def _Completed(args, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=args, returncode=returncode, stdout=stdout, stderr=stderr)


def test_detect_prefers_vista(monkeypatch):
    monkeypatch.setattr(modpacker.shutil, "which", lambda c: f"/usr/bin/{c}" if c in ("vista", "dnf", "flatpak") else None)
    assert modpacker.detect_package_managers() == ["vista", "dnf", "flatpak"]


def test_detect_no_managers(monkeypatch):
    monkeypatch.setattr(modpacker.shutil, "which", lambda c: None)
    assert modpacker.detect_package_managers() == []


def test_check_java_parses_version(monkeypatch):
    monkeypatch.setattr(modpacker.shutil, "which", lambda c: "/usr/bin/java" if c == "java" else None)
    fake = lambda cmd, **kw: _Completed(cmd, stderr='openjdk version "21.0.5" 2024-10-15\nOpenJDK Runtime Environment\n')
    monkeypatch.setattr(modpacker.subprocess, "run", fake)
    ok, detail = modpacker.check_java()
    assert ok and "21.0.5" in detail


def test_check_java_old_rejected(monkeypatch):
    monkeypatch.setattr(modpacker.shutil, "which", lambda c: "/usr/bin/java" if c == "java" else None)
    fake = lambda cmd, **kw: _Completed(cmd, stderr='openjdk version "11.0.22" 2024-01-16\n')
    monkeypatch.setattr(modpacker.subprocess, "run", fake)
    ok, detail = modpacker.check_java()
    assert not ok and "too old" in detail


def test_check_java_missing(monkeypatch):
    monkeypatch.setattr(modpacker.shutil, "which", lambda c: None)
    ok, _ = modpacker.check_java()
    assert not ok


def test_vista_java_command():
    cmd = modpacker.build_dep_install_command("java", "vista", yes=True)
    assert cmd == ["vista", "install", "adoptium@temurin21-binaries", "-y"]


def test_vista_launcher_uses_flathub_fallback():
    cmd = modpacker.build_dep_install_command("launcher", "vista", yes=False)
    assert cmd == ["vista", "install", "org.prismlauncher.PrismLauncher", "--default", "flathub"]


def test_vista_tkinter_unsupported():
    assert modpacker.build_dep_install_command("tkinter", "vista") is None


def test_native_commands():
    assert modpacker.build_dep_install_command("java", "apt")[-1] == "openjdk-21-jre"
    assert modpacker.build_dep_install_command("tkinter", "dnf")[-1] == "python3-tkinter"
    assert modpacker.build_dep_install_command("launcher", "flatpak") == [
        "flatpak", "install", "-y", "flathub", "org.prismlauncher.PrismLauncher"]


def test_resolve_pm_auto_prefers_vista(monkeypatch):
    monkeypatch.setattr(modpacker, "detect_package_managers", lambda: ["apt", "vista"])
    assert modpacker.resolve_pm("auto") == "vista"


def test_resolve_pm_explicit_missing(monkeypatch):
    monkeypatch.setattr(modpacker, "detect_package_managers", lambda: ["apt"])
    try:
        modpacker.resolve_pm("vista")
        assert False, "should raise"
    except modpacker.ModpackerError:
        pass


def test_install_deps_dry_run_no_subprocess(monkeypatch):
    # java + launcher missing, tkinter ok; vista available
    monkeypatch.setattr(modpacker, "detect_package_managers", lambda: ["vista"])
    monkeypatch.setattr(modpacker, "check_system_deps", lambda: {
        "java": {"installed": False, "detail": "no java", "label": "Java"},
        "tkinter": {"installed": True, "detail": "ok", "label": "Tk"},
        "launcher": {"installed": False, "detail": "none", "label": "Launcher"},
    })
    calls = []
    monkeypatch.setattr(modpacker.subprocess, "run", lambda *a, **k: calls.append(a) or _Completed(a[0]))
    n = modpacker.install_system_deps(pm="vista", dry_run=True, yes=True)
    assert n == 2
    assert calls == []


def test_install_deps_runs_vista_for_missing(monkeypatch):
    monkeypatch.setattr(modpacker, "detect_package_managers", lambda: ["vista"])
    monkeypatch.setattr(modpacker, "check_system_deps", lambda: {
        "java": {"installed": False, "detail": "no java", "label": "Java"},
        "tkinter": {"installed": True, "detail": "ok", "label": "Tk"},
        "launcher": {"installed": True, "detail": "ok", "label": "Launcher"},
    })
    calls = []
    monkeypatch.setattr(modpacker.subprocess, "run", lambda *a, **k: calls.append(list(a[0])) or _Completed(a[0]))
    n = modpacker.install_system_deps(pm="vista", only=["java"], dry_run=False, yes=True)
    assert n == 1
    assert calls == [["vista", "install", "adoptium@temurin21-binaries", "-y"]]

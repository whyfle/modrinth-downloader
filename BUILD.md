# Build & Development Instructions

## Standalone Script (Zero Dependencies)

No build required. The installer is a single Python file using only the stdlib.

```bash
# Check syntax
python -m py_compile modpacker.py

# Run directly
python modpacker.py --help
python modpacker.py https://modrinth.com/modpack/fabulously-optimized --dry-run
python modpacker.py --gui
```

## Development Setup (Modular)

```bash
# Clone / enter repo
cd modpacker

# (Optional) create venv
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate

# Install dev deps
pip install -r requirements.txt
pip install -e .

# Run tests
pytest -v
# or
python -m pytest tests -v

# Lint (if ruff installed)
ruff check modpacker.py
```

## Packaging

```bash
# sdist + wheel
pip install build
python -m build

# Install locally
pip install dist/modpacker-1.0.0-py3-none-any.whl

# Verify entry point
modpacker --help
```

## Windows Build (Single EXE)

Use PyInstaller to create a standalone exe (no Python required on target):

```bash
pip install pyinstaller
pyinstaller --onefile --windowed --name Modpacker --icon=icon.ico modpacker.py
# Output: dist/Modpacker.exe
# Run: dist/Modpacker.exe --gui
```

For CLI-only exe without window:
```bash
pyinstaller --onefile --name modpacker-cli modpacker.py
```

## macOS Build

```bash
pip install py2app
# or pyinstaller
pyinstaller --onefile --windowed --name Modpacker modpacker.py
# Or: python setup.py py2app
```

Sign for Gatekeeper if distributing:
```bash
codesign --deep --force --verify --verbose --sign "Developer ID" dist/Modpacker.app
```

## Linux Build

```bash
# AppImage via pyinstaller
pyinstaller --onefile --name modpacker modpacker.py

# Or deb/rpm via fpm
fpm -s python -t deb -n modpacker -v 1.0.0 --python-package-name-prefix python3 .
```

## GUI Requirements

- Tkinter is stdlib. On Debian/Ubuntu if missing:
  ```bash
  sudo apt install python3-tk
  ```
- On Windows/macOS, Tk is bundled with official Python installers.

## Testing the Full Pipeline (Dry-Run)

```bash
# No disk mutation — downloads .mrpack and previews
python modpacker.py https://modrinth.com/modpack/fabulously-optimized \
  --minecraft-dir /tmp/test-mc --dry-run --verbose

# Check isolated profile not written to real .minecraft
ls /tmp/test-mc/profiles/  # should be empty after dry-run

# Real install to isolated dir for manual verification
python modpacker.py https://modrinth.com/modpack/fabulously-optimized \
  --minecraft-dir /tmp/test-mc

# Verify launcher_profiles.json
cat /tmp/test-mc/launcher_profiles.json | python -m json.tool

# Verify gameDir
ls /tmp/test-mc/profiles/fabulously-optimized/mods | head
ls /tmp/test-mc/versions/
```

## CI

GitHub Actions example:

```yaml
- uses: actions/setup-python@v5
  with: {python-version: '3.11'}
- run: pip install -r requirements.txt
- run: pytest --cov
- run: python modpacker.py --help
```

## Release Checklist

1. Update `APP_VERSION` in `modpacker.py` and `pyproject.toml`
2. `pytest`
3. `python modpacker.py --dry-run` on Fabric + Forge packs
4. `pyinstaller` builds for Windows/macOS/Linux
5. Tag and push

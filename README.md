# Modrinth-to-Minecraft Launcher

**A polished installer that bridges Modrinth modpacks (`.mrpack`) directly into the official Minecraft Launcher — no custom launcher needed.**

Paste a Modrinth URL → Parse `.mrpack` → Download mods with hash verification → Install Fabric/Forge/NeoForge/Quilt → Create isolated game directory → Inject profile into `launcher_profiles.json` → Play from the official launcher.

![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-blue)
![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey)
![License MIT](https://img.shields.io/badge/license-MIT-green)

---

## ✨ Features

- **One-click install** from Modrinth project or version URL, or direct `.mrpack` URL
- **Proper `.mrpack` parsing** (`modrinth.index.json` + `overrides/`)
- **All loaders**: Fabric, Forge, NeoForge, Quilt (extensible architecture)
- **Isolated gameDirs**: each modpack → `.minecraft/profiles/<slug>/` (no cross-contamination of mods/configs/saves)
- **Official Launcher compatible**: creates `versions/<loader-id>/<id>.json` inheriting vanilla, and injects `launcher_profiles.json` safely (with backups, atomic write)
- **Security**: path-traversal protection, HTTPS-only, SHA-512/SHA-1 verification, no code execution from pack, safe temp dirs
- **Resilient networking**: retries, rate-limit handling, concurrent downloads (6 default, configurable), hash-verified resume/skip
- **Cross-platform**: auto-detects `.minecraft` on Windows/macOS/Linux, overridable
- **Dry-run**: preview without modifying anything
- **State tracking**: `modpacker/installed_packs.json` for update detection
- **CLI + GUI**: Tkinter polished UI (stdlib) + full argparse CLI
- **Zero required dependencies** (stdlib only: `urllib`, `json`, `zipfile`, `pathlib`, `hashlib`, `tkinter`)

---

## 🚀 Quick Start

### Zero-install (standalone script)

```bash
# No pip install needed — stdlib only
python modpacker.py https://modrinth.com/modpack/fabulously-optimized
python modpacker.py https://modrinth.com/modpack/create-essentials --dry-run
python modpacker.py --gui
```

### Install as package

```bash
pip install -e .
modpacker https://modrinth.com/modpack/fabulously-optimized
```

### GUI

```bash
python modpacker.py --gui
# or just:
python modpacker.py
# (no args opens GUI if display is available)
```

GUI workflow: **Paste URL → Preview → Select Version → Install → Open Minecraft Launcher → Select profile → Play**

---

## 📖 Usage

### CLI Reference

```bash
python modpacker.py [URL] [options]

positional:
  url                            Modrinth URL:
                                   https://modrinth.com/modpack/<slug>
                                   https://modrinth.com/modpack/<slug>/version/<id>
                                   https://cdn.modrinth.com/.../*.mrpack (direct)

options:
  --minecraft-dir PATH           Custom .minecraft dir (default auto-detected)
  --game-dir PATH                Custom gameDir for this pack (default: <mc>/profiles/<slug>)
  --dry-run                      Preview without writing files/launcher_profiles.json
  --list-versions                List 20 latest versions and exit
  --version-id ID                Pin to exact version ID or version_number
  --concurrency N                Parallel downloads (default 6)
  --keep-mrpack                  Keep cached .mrpack in <mc>/modpacker/cache/
  --no-verify                    Disable hash verification (not recommended)
  --gui                          Force GUI
  --verbose                      Debug logs
  --version                      Print version
```

### Examples

```bash
# Normal install (Fabric example)
python modpacker.py https://modrinth.com/modpack/fabulously-optimized

# Dry-run preview (no writes)
python modpacker.py https://modrinth.com/modpack/all-the-mods --dry-run --verbose

# Custom Minecraft dir
python modpacker.py https://modrinth.com/modpack/ovo \
  --minecraft-dir ~/Games/minecraft --dry-run

# List versions then pin one
python modpacker.py https://modrinth.com/modpack/create --list-versions
python modpacker.py https://modrinth.com/modpack/create --version-id 8GJd9k2a

# Direct .mrpack
python modpacker.py https://cdn.modrinth.com/data/AANobbMI/versions/xyz/pack.mrpack

# Isolated test directory (recommended for automation)
python modpacker.py https://modrinth.com/modpack/foo \
  --minecraft-dir /tmp/test-mc --game-dir /tmp/test-mc/profiles/foo --dry-run
```

---

## 🔧 How It Works

### 1. Parse Link & Modrinth API (REST v2)

```
Input URL  →  parse_modrinth_url()  →  slug + optional version_hint
            GET /v2/project/{slug}        → project metadata
            GET /v2/project/{id}/version  → versions list (sorted, featured-aware)
            pick best or user-selected    → primary .mrpack file URL + hashes
```

### 2. Download & Extract `.mrpack`

A `.mrpack` is a ZIP containing `modrinth.index.json` + `overrides/`.

- Downloads with retry + `User-Agent: Modpacker/1.0.0`
- Validates ZIP for `..` / absolute paths upfront
- Parses `modrinth.index.json`: `formatVersion`, `game`, `versionId`, `name`, `files[]`, `dependencies` (`minecraft`, `fabric-loader`/`forge`/`neoforge`/`quilt-loader`)
- Extracts `overrides/` safely via `safe_join()` → temp dir

### 3. Asset Fetching (Download Mods)

For each `files[]` entry:
- destination = `gameDir / path` (e.g., `mods/sodium.jar`, `config/foo.toml`)
- `env.client == unsupported` → skip (server-only)
- path-traversal checked via `safe_join(gameDir, path)` (rejects `../../` and `C:\` etc.)
- downloads from `downloads[]` (tries all URLs), with SHA-512 primary / SHA-1 fallback verification
- skips if file already exists with matching hash
- `ThreadPoolExecutor(concurrency=6)` with per-file progress, retries

### 4. Loader Setup

```
dependencies.minecraft  →  ensure_vanilla_version()  →  fetch piston-meta manifest → versions/<mc>/<mc>.json + client jar
dependencies.fabric-loader / forge / neoforge / quilt-loader → FabricInstaller / ForgeInstaller / NeoForgeInstaller / QuiltInstaller
  Fabric:  GET https://meta.fabricmc.net/v2/versions/loader/<mc>/<ver>/profile/json → versions/fabric-loader-<ver>-<mc>/json
  Quilt:   GET https://meta.quiltmc.org/v3/versions/loader/<mc>/<ver>/profile/json
  Forge:   synthetic versions/<mc>-forge-<ver>.json inheriting vanilla (tries Maven fetch for libs)
  NeoForge: similar via maven.neoforged.net
Fallback synthetic always created if meta fetch fails.
```

All version JSONs use `inheritsFrom: <minecraft_version>` (launcher's inheritance model).

### 5. Directory Assembly

```
.minecraft/
  profiles/<slug>/        ← gameDir (isolated per modpack)
    mods/                 ← downloaded jars
    config/               ← from overrides/config/
    resourcepacks/        ← from overrides/resourcepacks/
    saves/                ← preserved on reinstall (not overwritten)
    ...
  versions/
    1.21.1/               ← vanilla
    fabric-loader-0.15.7-1.21.1/  ← loader profile (inheritsFrom 1.21.1)
  launcher_profiles.json  ← injected
  modpacker/
    cache/<pack>.mrpack
    installed_packs.json  ← state
```

### 6. Launcher Integration (`launcher_profiles.json`)

**Before:** backup to `launcher_profiles.json.bak` + timestamped `launcher_profiles.json.bak.YYYYMMDD_HHMMSS`  
**Injection:** deterministic UUID5 (`namespace URL` + `slug`) as key → stable across reinstalls; if `gameDir` already exists reuses key (update).  
**Entry:**

```json
{
  "profiles": {
    "c1a2...-uuid5": {
      "created": "2026-09-05T00:00:00.000Z",
      "gameDir": "/home/user/.minecraft/profiles/fabulously-optimized",
      "icon": "Grass",
      "lastUsed": "2026-09-05T00:00:00.000Z",
      "lastVersionId": "fabric-loader-0.15.7-1.21.1",
      "name": "Fabulously Optimized",
      "type": "custom"
    }
  }
}
```

Atomic write via `launcher_profiles.json.tmp` → validate JSON → replace.

**Important:** close the vanilla launcher before install to avoid write races. The installer warns if `Minecraft Launcher` process is detected (via `tasklist`/`pgrep`/`ps`).

---

## 🖥️ Launcher Compatibility — Deep Dive

The **official Minecraft Launcher** discovers installations via `.minecraft/versions/<id>/<id>.json`. Each JSON may:

- contain full version metadata, **or**
- inherit from a parent via `inheritsFrom: "1.21.1"` (recommended; avoids duplicating vanilla libraries).

This installer uses **inheritance** for all loaders, keeping vanilla separate and launcher-managed. After install:

1. Open official Launcher (you'll be prompted or click **Open Minecraft Launcher** in GUI/CLI).
2. The profile appears in **Installations** (or profiles dropdown) as e.g. *Fabulously Optimized*.
3. If not auto-selected, select it once manually — `type: "custom"` ensures it appears.
4. Click **Play** — launcher downloads missing loader libraries (Fabric/Forge will fetch from `maven.fabricmc.net` / `maven.minecraftforge.net` on first launch if needed).

We **never** touch `launcher_accounts.json`, credentials, or auth. We never ask for Microsoft password.

*Research note: we validated against current launcher behavior (2024-2026) where `launcher_profiles.json` remains the supported local profile store; `inheritsFrom` is the documented mechanism for custom versions (Fabric, Forge, etc.). If future launcher versions migrate to a new config, the installer documents this and falls back to creating a valid `versions/<id>` entry that can be manually selected.*

---

## 🛡️ Security

See [SECURITY.md](SECURITY.md) for full threat model. TL;DR:

- **Archive traversal** prevented (rejects `../`, absolute, `C:` paths at ZIP parse and per-file)
- **URL validation** (HTTPS enforced via `ensure_https`, `validate_url`)
- **Hash verification** (SHA-512 preferred, SHA-1 fallback; skip-if-exists only when hash matches)
- **No code execution** from pack (never `eval`, `exec`, shell, or auto-run binaries)
- **Safe temp dirs** via `tempfile.mkdtemp`; no world-writable.
- **Limited scope**: only writes inside `gameDir` and `versions/<id>`; never deletes entire `.minecraft`.
- **User warning**: mods are executable code — installer shows: *“Only install modpacks you trust.”*

---

## 🧪 Testing

```bash
python -m pytest tests -v
# or stdlib:
python -m unittest discover tests
```

Dry-run can be exercised offline with a synthetic `.mrpack` (see `tests/`):

```bash
python modpacker.py https://modrinth.com/modpack/dummy \
  --minecraft-dir /tmp/test-mc --dry-run --verbose
```

---

## 🏗️ Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md). Modules:

```
modpacker.py (standalone)  ↔  src/modpacker/ (modular mirror)
  api/modrinth.py        ModrinthClient, URL parsing
  mrpack/parser.py       MrpackParser, HashVerifier
  minecraft/paths.py     OS detection
  minecraft/launcher.py  Profile injection
  loaders/*              Vanilla/Fabric/Forge/NeoForge/Quilt
  filesystem/safe.py     safe_join, SafeExtractor
  downloads/manager.py   DownloadManager (ThreadPool, retry)
  ui/                    Tkinter MainWindow, Preview, Progress
  database/state.py      InstalledPack
  installer.py           Orchestrator (ModpackInstaller)
```

Add a new loader: subclass `LoaderInstaller`, implement `install()`, register in `get_loader_installer()`.

---

## ❓ Troubleshooting

**Launcher profile not appearing?**
- Ensure launcher was closed during install (reopen it). Check `.minecraft/launcher_profiles.json` contains your pack under `profiles`.

**Hash mismatch?**
- Mod host changed file; installer retries then fails fast with filename. Re-run; or `--no-verify` (not recommended) to bypass.

**Forge first launch downloads?**
- Expected — our synthetic profile delegates library download to the launcher on first Play (Forge's installer normally does this). Keep network on first launch.

**Insufficient disk space?**
- Installer warns if `free < 1.5× total_size`.

**Read-only `.minecraft`?**
- Run with correct permissions; or `--minecraft-dir` to a writable path.

---

## 📦 Dependencies

**Required:** None beyond Python 3.8+ stdlib.

| Library | Usage | Required? | Notes |
|---------|-------|-----------|-------|
| `urllib` | HTTP client for Modrinth/Maven | ✅ Stdlib | Used instead of `requests` to keep zero-dep; if you prefer `requests`, replace `http_get_json`/`http_download` with `requests.get(stream=True)` — same retry/hash logic applies. |
| `json` | Modrinth + launcher JSON | ✅ Stdlib | — |
| `zipfile` | `.mrpack` extraction | ✅ Stdlib | — |
| `pathlib` | Paths | ✅ Stdlib | — |
| `hashlib` | SHA-512/SHA-1 | ✅ Stdlib | — |
| `tkinter` | GUI | ✅ Stdlib | Bundled with Python; on Linux may need `sudo apt install python3-tk`. If you prefer `PyQt`/`PySide6`, replace `launch_gui()` with a Qt `MainWindow` — the installer core (`ModpackInstaller`) is UI-agnostic and can be reused verbatim via its `progress_callback`/`log_callback`. |
| `concurrent.futures` | ThreadPool | ✅ Stdlib | — |
| `requests` | Alternative HTTP | ❌ Optional | `pip install requests` then swap in — not required. |
| `PyQt5` / `PySide6` | Alternative GUI | ❌ Optional | If you want a native Qt look, keep the core and rewrite `ui/main.py`; CLI remains. |
| `pytest` | Tests | ❌ Dev only | `pip install pytest` |

> The single-file `modpacker.py` purposefully avoids `requests`/`PyQt` to guarantee portability. The modular `src/` mirrors the same logic and can be adapted to `requests`/`aiohttp`/`PyQt` without changing installer semantics.

## 📄 License & Attribution

MIT. Respects Modrinth API terms and mod licenses. Mods are downloaded from URLs specified in `modrinth.index.json` (not redistributed). Shows Modrinth attribution in GUI footer. See [USER_GUIDE.md](USER_GUIDE.md).

---

## 🤝 Contributing

Issues/PRs welcome at `github.com/anomalyco/opencode`.

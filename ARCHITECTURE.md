# Architecture

## Overview

The installer is designed as a **translator**, not a launcher. Single responsibility: transform a Modrinth `.mrpack` into a vanilla-launcher-compatible installation.

```
Modrinth URL
  → ModrinthClient (REST v2)
  → .mrpack download
  → MrpackParser (ZIP + modrinth.index.json + overrides)
  → HashVerifier + DownloadManager (concurrent, verified)
  → MinecraftInstaller (vanilla fetch) + LoaderInstaller (Fabric/Forge/NeoForge/Quilt)
  → FileManager (isolated gameDir) + LauncherIntegration (launcher_profiles.json)
  → StateDB (installed_packs.json)
```

## Modules

### Standalone vs Modular

- **`modpacker.py`** — single-file, stdlib-only, can be `curl | python` deployed. Contains all logic, CLI, and Tkinter GUI. Meets deliverable “standalone script using urllib/json/zipfile/pathlib/hashlib”.
- **`src/modpacker/`** — clean modular mirror for maintainability and tests; each file re-exports or thinly wraps the standalone logic. Preferred for development.

```
src/modpacker/
  __init__.py
  api/
    modrinth.py          ModrinthClient, parse_modrinth_url, data classes Project/Version
  mrpack/
    parser.py            MrpackParser, parse_mrpack_index, MrpackIndex/File
    validator.py         (future) pack validation rules
  filesystem/
    safe.py              safe_join, SafeExtractor, compute_hash, ensure_https
    manager.py           FileManager helpers
  downloads/
    manager.py           DownloadManager, DownloadTask, RetryPolicy
  minecraft/
    paths.py             get_minecraft_dir, is_launcher_running
    vanilla.py           ensure_vanilla_version, fetch manifest
    launcher.py          load/inject launcher_profiles.json, backups, atomic write
  loaders/
    base.py              LoaderInstaller (ABC)
    fabric.py            FabricInstaller (meta.fabricmc.net)
    forge.py             ForgeInstaller (maven.minecraftforge.net, synthetic fallback)
    neoforge.py          NeoForgeInstaller
    quilt.py             QuiltInstaller
  database/
    state.py             InstalledPack, load/save installed_packs.json
  installer.py           ModpackInstaller orchestrator
  ui/
    main.py              Tk MainWindow, Preview, Progress, Settings, Completion
  cli.py                 argparse entrypoint
```

Dependency rule: `installer.py` depends on all; no circular deps; UI depends on installer but not vice versa.

## Key Interfaces

```python
class LoaderInstaller(ABC):
    def loader_id(self) -> str: ...
    def install(self, minecraft_version: str, loader_version: str, dry_run: bool, progress_cb) -> str:
        """return versionId for launcher_profiles.json"""

class ModrinthClient:
    def get_project(slug) -> Project
    def list_versions(project_id) -> List[Version]
    def get_version(project_id, version_id) -> Version

class MrpackParser:
    def parse(self) -> (MrpackIndex, overrides_tmp: Path)

class DownloadManager:
    def download_all(tasks, progress_cb, file_progress_cb, verify_hashes) -> (completed, total, failed)

class LauncherIntegration:
    def inject_launcher_profile(mc_dir, slug, LauncherProfile, dry_run) -> uuid_key
```

## Loader Extensibility

Add `MyLoaderInstaller(LoaderInstaller)`:

1. Implement `loader_id()` e.g. `"my-loader"`.
2. Implement `install()` — fetch profile JSON or synthesize `{id, inheritsFrom, libraries, mainClass}` → write `versions/<id>/<id>.json`.
3. Register in `get_loader_installer()` dispatch.

All loaders use `inheritsFrom` so they stay launcher-managed.

## Concurrency Model

- **I/O-bound** → `ThreadPoolExecutor(max_workers=concurrency)` (default 6) with safe URL retry across `downloads[]`.
- **Progress** via callbacks → GUI thread marshals via `root.after`.
- **Cancellation** via `DownloadManager._cancel` Event + `Future.cancel()`.

## Persistence

- **GameDir**: `profiles/<slug>/` (default) user-overridable. Each pack isolated.
- **Versions**: `versions/<versionId>/` JSON (+ jar for vanilla).
- **Launcher profiles**: `launcher_profiles.json` (atomic tmp → JSON validate → replace; backups retained).
- **State**: `modpacker/installed_packs.json` (hashes, dates, dirs) for update detection; `modpacker/cache/*.mrpack` optional.

## Error Handling

Hierarchy: `ModpackerError → ModrinthError | MrpackError | HashMismatchError | DownloadError`.  
CLI maps to user-friendly messages + `Show technical details (--verbose)` trace. GUI surfaces per-file failures with source URL.

## Testing Strategy

Fixtures include synthetic `.mrpack` zips with edge cases: nested dirs, unsafe paths, missing files, hash mismatches. Tests cover parser, safe_join, hash verifier, version resolution, launcher injection (on temp dir), installer dry-run.

## Future

- Async (`asyncio`+`aiohttp`) for higher concurrency if requests dependency allowed.
- Signature verification for mods.
- Incremental update (hash-compare → only download changed).

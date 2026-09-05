# User Guide — Modrinth-to-Minecraft Launcher

## Install a Pack in 30 Seconds

1. Copy Modrinth URL, e.g. `https://modrinth.com/modpack/fabulously-optimized`
2. Run:
   ```bash
   python modpacker.py --gui
   ```
   Or CLI:
   ```bash
   python modpacker.py https://modrinth.com/modpack/fabulously-optimized
   ```
3. Wait for progress: *Fetching → Downloading modpack → Resolving → Installing loader → Installing mods → Overrides → Launcher profile*.
4. Click **Open Minecraft Launcher** (or open manually).
5. In the launcher, select installation named after the pack (e.g. *Fabulously Optimized*). First launch may download additional loader libs — keep internet on.
6. **Play**.

## GUI Tour

- **URL field** — paste any `modrinth.com/modpack/...` URL (project or `/version/<id>`). Direct `.mrpack` URLs also work.
- **Preview** — fetches title/author/description, Minecraft versions, loaders, file count. Populates version dropdown (newest first, featured pinned).
- **Version dropdown** — pick a specific pack version if you don't want latest.
- **Install Modpack** — starts the full pipeline with live progress bar, current file, speed, counters.
- **Dry Run** — same as Preview but also simulates directory layout, loader resolution, and launcher injection without writing.
- **⚙ Settings**:
  - Minecraft directory (auto-detected; browse to override)
  - Download concurrency (1–16)
  - Verify hashes (on by default)
  - Keep `.mrpack` (off by default; cached under `.minecraft/modpacker/cache/`)
  - Separate gameDir (on: `profiles/<slug>/`; off: install directly into `.minecraft` — not recommended)

## CLI Tour

See `--help`. Notable workflows:

```bash
# Preview without touching disk
python modpacker.py https://modrinth.com/modpack/ovo --dry-run --verbose

# List and pin a version
python modpacker.py https://modrinth.com/modpack/create --list-versions
python modpacker.py https://modrinth.com/modpack/create --version-id 3.2.1

# Isolated test (good for CI)
python modpacker.py https://modrinth.com/modpack/foo \
  --minecraft-dir /tmp/test-mc --dry-run

# Custom gameDir
python modpacker.py https://modrinth.com/modpack/foo \
  --game-dir ~/MyPacks/foo
```

## Where Are My Files?

Default after installing `fabulously-optimized`:

```
.minecraft/
  profiles/fabulously-optimized/
    mods/            ← 120 jars
    config/          ← from overrides/
    resourcepacks/
    saves/           ← your worlds (preserved)
  versions/
    1.21.1/                     ← vanilla (launcher-managed)
    fabric-loader-0.15.7-1.21.1/← fabric profile (inheritsFrom 1.21.1)
  launcher_profiles.json       ← now contains profile
  modpacker/
    installed_packs.json       ← registry for updates
    cache/                     ← .mrpack zips
```

Change base `.minecraft` via `--minecraft-dir` or Settings → Browse. Change per-pack dir via `--game-dir`.

## Updating a Pack

Re-run the same install:

```bash
python modpacker.py https://modrinth.com/modpack/foo
```

The installer detects existing `profiles/<slug>` and updates. It:

- Compares file hashes → only downloads changed.
- Leaves `saves/` alone.
- Offers *clean update* later (manual delete of obsolete files if you tick “clean” — future GUI feature; currently re-download ensures obsolete files under `mods/` that are no longer in manifest remain until you delete them manually).

If you chose **Dry Run** first, re-run without it to apply.

## Troubleshooting

- **Mods missing?** Check `profiles/<slug>/mods/` count matches pack file count. Re-run; failed downloads are retried 3× and reported.
- **Profile not in launcher?** Close launcher, re-install, reopen. Validate `launcher_profiles.json` is valid JSON. Restore from `launcher_profiles.json.bak` if needed.
- **Forge “downloading libraries” long first launch?** Normal — synthetic Forge profile defers Maven fetches to launcher’s first launch.
- **Launcher running warning:** Always close launcher before install.
- **Permission error:** Ensure `.minecraft` writable; or pick a writable `--minecraft-dir`.

## Uninstall a Pack

1. In launcher, delete the profile (Installations → delete).
2. Delete its gameDir: `rm -rf .minecraft/profiles/<slug>`.
3. Optionally delete its version under `.minecraft/versions/<versionId>` if no other pack uses it.
4. Entry stays in `modpacker/installed_packs.json` until you remove it there (safe to edit JSON).

## Legal

Mods are licensed separately. Installer downloads from pack-specified URLs and does not redistribute them. Comply with Modrinth Terms.

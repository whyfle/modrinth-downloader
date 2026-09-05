# Security

Modpacks are **untrusted input**. The installer treats them as such.

## Threat Model

| Threat | Mitigation |
|--------|------------|
| **Zip path traversal** (`../../etc/passwd`, `/absolute`, `C:\Windows\…`) | Rejected at two layers: (1) `MrpackParser.parse()` iterates `ZipInfo.filename` and rejects `startswith("/")` or ` ".." in Path(parts)` before extraction; (2) per-file `safe_join(base, relative)` verifies `resolved.relative_to(base.resolve())` succeeds. Both file `path` in `modrinth.index.json` and ZIP entries under `overrides/` are checked. |
| **Unsafe URLs** | `validate_url()` enforces `https`/`http` + netloc; `ensure_https()` upgrades `http→https`; no `file://`, `ftp://`, etc. |
| **Hash tampering / MITM** | Every download verified against `hashes.sha512` (preferred) or `sha1` from `modrinth.index.json` (which itself is from Modrinth API over TLS). Existing files skipped only if hash matches. Mismatches raise `HashMismatchError` → fail/ retry. Opt-out via `--no-verify` is explicit and warned. |
| **Code execution from pack** | No `eval`/`exec`/shell. No extraction of executables to `PATH`. No shell commands derived from pack metadata. Mod JARs are placed under `gameDir/mods/` but **never executed** by the installer. Launcher launches them under Minecraft's own JVM. |
| **Malicious launcher_profiles.json edits** | Existing data preserved (`json.load` → merge `profiles[key]`). Backups: `launcher_profiles.json.bak` + timestamped `bak.YYYYMMDD_HHMMSS` before write. Atomic write: temp file → JSON validate → `replace()`. Original `created` timestamp retained on update. |
| **Credential handling** | No interaction with `launcher_accounts.json`, `launcher_accounts_microsoft_store.json`, or Microsoft OAuth. No password prompt. `launcher_profiles.json` injection is limited to `profiles.*` with `type: custom`. |
| **HTTPS & TLS** | `ssl.create_default_context()` for all `urllib` requests; `User-Agent: Modpacker/1.0.0`. |
| **Rate limiting / DoS against Modrinth** | Respects `429 Retry-After`, exponential backoff on 5xx, concurrency cap (default 6), `DEFAULT_TIMEOUT 30s`. |
| **Disk exhaustion / overwrites** | Checks `shutil.disk_usage().free` vs `1.5× total_size`; warns. Uses isolated `profiles/<slug>` per pack; never blindly deletes `.minecraft`. On reinstall, only `saves/` is preserved (skips overwriting). |
| **Vanilla Launcher race** | Warns if launcher process detected (`tasklist`/`pgrep`/`ps` heuristics) before touching `launcher_profiles.json`. |
| **Temp dir safety** | `tempfile.mkdtemp(prefix="mrpack_parse_")` with OS-secure perms; cleaned via `shutil.rmtree` after install (even on error where possible). |

## User Warning

Since mods are JVM bytecode, installing a modpack is equivalent to running third-party code inside Minecraft. The GUI and CLI emit:

> ⚠ Mods are executable code. Only install modpacks you trust.

and show attribution/links to Modrinth.

## Disclosure

Report vulnerabilities via GitHub issues (or email maintainer). Do not publish exploit packs before a fix.

## Hardening Roadmap

- Optional Sigstore/Cosign verification if Modrinth signs.
- Allowlist for `overrides/` extensions (deny `.exe`, `.sh`, `.bat` if desired — currently permitted because legit packs use `.sh` for server start but never auto-executed).
- Certificate pinning for Modrinth hosts.

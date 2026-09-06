#!/usr/bin/env python3
"""
Modrinth-to-Minecraft Launcher - Standalone Installer
=====================================================
A polished Python installer that bridges Modrinth modpacks (.mrpack)
directly into the official Minecraft Launcher.

Features:
 - Parses Modrinth project/version URLs via Modrinth REST API v2
 - Downloads & validates .mrpack (modrinth.index.json + overrides/)
 - Verifies SHA-512 / SHA-1 hashes, retries, safe path handling
 - Concurrent downloads with progress & resume support
 - Installs Fabric / Forge / NeoForge / Quilt + Vanilla metadata
 - Creates isolated gameDir per modpack: .minecraft/profiles/<slug>/
 - Safely injects profile into launcher_profiles.json (with backup)
 - CLI + Tkinter GUI, dry-run, cross-platform

Stdlib only: urllib, json, zipfile, pathlib, hashlib, concurrent.futures,
             tkinter (GUI), argparse, etc.
Optional: requests (if installed, will be used for slightly better ergonomics)

Usage (CLI):
    python modpacker.py https://modrinth.com/modpack/fabulously-optimized
    python modpacker.py https://modrinth.com/modpack/adventure --dry-run
    python modpacker.py --gui
    python modpacker.py https://modrinth.com/modpack/foo --minecraft-dir /custom/.minecraft --list-versions

GUI:
    python modpacker.py --gui   (or no args on desktop)

Author: OpenCode / Muse Spark
License: MIT
"""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime
import hashlib
import io
import json
import os
import platform
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
import queue

# ──────────────────────────────────────────────────────────────────────────────
# Constants & Globals
# ──────────────────────────────────────────────────────────────────────────────

APP_NAME = "Modrinth Downloader"
APP_VERSION = "1.1.0"
MODRINTH_API = "https://api.modrinth.com/v2"
MODRINTH_API_STAGING = "https://staging-api.modrinth.com/v2"
USER_AGENT = f"Modpacker/{APP_VERSION} (github.com/anomalyco/opencode)"
VANILLA_MANIFEST_URL = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
FABRIC_META_URL = "https://meta.fabricmc.net/v2"
QUILT_META_URL = "https://meta.quiltmc.org/v3"
FORGE_MAVEN_URL = "https://maven.minecraftforge.net/net/minecraftforge/forge"
NEOFORGE_MAVEN_URL = "https://maven.neoforged.net/net/neoforged/neoforge"

# concurrency defaults
DEFAULT_CONCURRENCY = 6
DEFAULT_TIMEOUT = 30
DEFAULT_RETRIES = 3

# logging
import logging
log = logging.getLogger("modpacker")
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
log.addHandler(handler)
log.setLevel(logging.INFO)

# ──────────────────────────────────────────────────────────────────────────────
# Utilities
# ──────────────────────────────────────────────────────────────────────────────

def iso_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")

def slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")[:64] or "modpack"

def get_minecraft_dir(custom: Optional[str] = None) -> Path:
    if custom:
        return Path(custom).expanduser().resolve()
    system = platform.system()
    if system == "Windows":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / ".minecraft"
        return Path.home() / "AppData" / "Roaming" / ".minecraft"
    elif system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "minecraft"
    else:
        return Path.home() / ".minecraft"

def get_profiles_dir(mc_dir: Path) -> Path:
    return mc_dir / "profiles"

def get_modpacks_dir(mc_dir: Path) -> Path:
    # alternative location, we default to profiles/<slug> per spec
    return mc_dir / "profiles"

def is_launcher_running() -> bool:
    """Best-effort check if official launcher is running; warn to avoid JSON race."""
    system = platform.system()
    try:
        if system == "Windows":
            import subprocess
            out = subprocess.run(["tasklist"], capture_output=True, text=True, timeout=5)
            txt = out.stdout.lower()
            return "minecraft launcher" in txt or "minecraftlauncher" in txt or "javaw.exe" in txt and "launcher" in txt
        else:
            import subprocess
            # pgrep may not exist everywhere; fallback to ps
            try:
                out = subprocess.run(["pgrep", "-fl", "minecraft"], capture_output=True, text=True, timeout=3)
                return bool(out.stdout.strip())
            except FileNotFoundError:
                out = subprocess.run(["ps", "aux"], capture_output=True, text=True, timeout=3)
                txt = out.stdout.lower()
                return "minecraft launcher" in txt or "launcher_profiles" in txt
    except Exception:
        return False
    return False

def warn_if_launcher_running():
    if is_launcher_running():
        log.warning("⚠  Minecraft Launcher appears to be running. Close it before installing to avoid launcher_profiles.json conflicts.")
        return True
    return False

def safe_join(base: Path, relative: str) -> Path:
    """Prevent path traversal. Only allow relative POSIX paths under base."""
    # reject absolute, drive letters, ..
    if os.path.isabs(relative):
        raise ValueError(f"Absolute path not allowed: {relative}")
    # zip paths are posix
    p = Path(relative)
    # check for .. components
    for part in p.parts:
        if part in ("..", "../", "..\\"):
            raise ValueError(f"Path traversal not allowed: {relative}")
        if part.startswith("/") or part.startswith("\\"):
            raise ValueError(f"Absolute path component: {relative}")
        if ":" in part and platform.system() == "Windows":
            # drive letter like C:
            if re.match(r"^[a-zA-Z]:", part):
                raise ValueError(f"Drive path not allowed: {relative}")
    # also use posix normalization
    # Prevent encoded traversal like a/b/../../c
    normalized = (base / relative).resolve()
    try:
        normalized.relative_to(base.resolve())
    except ValueError:
        raise ValueError(f"Path escapes base directory: {relative} -> {normalized}")
    return base / relative

def compute_hash(path: Path, algo: str = "sha512") -> str:
    h = hashlib.new(algo)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024*1024), b""):
            h.update(chunk)
    return h.hexdigest()

def compute_hash_bytes(data: bytes, algo: str = "sha512") -> str:
    h = hashlib.new(algo)
    h.update(data)
    return h.hexdigest()

def validate_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(url)
        return parsed.scheme in ("https", "http") and bool(parsed.netloc)
    except Exception:
        return False

def ensure_https(url: str) -> str:
    if url.startswith("http://"):
        try:
            parsed = urllib.parse.urlparse(url)
            host = parsed.hostname or ""
            # Don't upgrade localhost/private for tests
            if host in ("127.0.0.1", "localhost", "::1") or host.startswith("192.168.") or host.startswith("10."):
                return url
        except Exception:
            pass
        return "https://" + url[len("http://"):]
    return url

def human_size(n: int) -> str:
    for unit in ["B","KB","MB","GB"]:
        if abs(n) < 1024.0:
            return f"{n:3.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} TB"

# ──────────────────────────────────────────────────────────────────────────────
# Exceptions
# ──────────────────────────────────────────────────────────────────────────────

class ModpackerError(Exception):
    pass

class ModrinthError(ModpackerError):
    pass

class MrpackError(ModpackerError):
    pass

class HashMismatchError(ModpackerError):
    pass

class DownloadError(ModpackerError):
    pass

# ──────────────────────────────────────────────────────────────────────────────
# HTTP Client (stdlib with retry)
# ──────────────────────────────────────────────────────────────────────────────

def http_get_json(url: str, timeout: int = DEFAULT_TIMEOUT, retries: int = DEFAULT_RETRIES, headers_extra: Optional[Dict[str,str]] = None) -> Any:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }
    if headers_extra:
        headers.update(headers_extra)
    last_exc = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers, method="GET")
            ctx = ssl.create_default_context()
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                if resp.status == 429:
                    wait = int(resp.headers.get("Retry-After", "5"))
                    log.warning(f"Rate limited (429), waiting {wait}s attempt {attempt+1}/{retries}")
                    time.sleep(wait)
                    continue
                if resp.status >= 400:
                    raise ModrinthError(f"HTTP {resp.status} for {url}")
                data = resp.read()
                return json.loads(data.decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = int(e.headers.get("Retry-After", "5"))
                log.warning(f"Rate limited 429, waiting {wait}s")
                time.sleep(wait)
                last_exc = e
                continue
            if 500 <= e.code < 600 and attempt < retries-1:
                log.warning(f"Server error {e.code}, retry {attempt+1}/{retries}")
                time.sleep(1 + attempt*2)
                last_exc = e
                continue
            raise ModrinthError(f"HTTP {e.code} {e.reason} for {url}: {e.read().decode(errors='ignore')[:500]}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_exc = e
            if attempt < retries-1:
                time.sleep(1 + attempt*2)
                continue
            raise ModrinthError(f"Network error for {url}: {e}") from e
    raise ModrinthError(f"Failed after {retries} retries: {last_exc}")

def http_download(url: str, dest: Path, expected_hashes: Optional[Dict[str,str]] = None, timeout: int = DEFAULT_TIMEOUT, retries: int = DEFAULT_RETRIES, progress_cb: Optional[Callable[[int,int],None]] = None) -> Path:
    """Stream download to dest, verify hashes, with retries."""
    url = ensure_https(url)
    if not validate_url(url):
        raise DownloadError(f"Invalid URL: {url}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": USER_AGENT}
    last_exc = None
    for attempt in range(retries):
        try:
            # Check if file exists and hash matches -> skip
            if dest.exists() and expected_hashes:
                try:
                    skip = True
                    for algo, expected in expected_hashes.items():
                        if algo not in ("sha512","sha1","sha256","md5"):
                            continue
                        actual = compute_hash(dest, algo)
                        if actual.lower() != expected.lower():
                            skip = False
                            break
                    if skip:
                        log.debug(f"Skipping existing file with valid hash: {dest.name}")
                        if progress_cb:
                            progress_cb(dest.stat().st_size, dest.stat().st_size)
                        return dest
                except Exception:
                    pass  # re-download
            req = urllib.request.Request(url, headers=headers, method="GET")
            ctx = ssl.create_default_context()
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                if resp.status == 429:
                    wait = int(resp.headers.get("Retry-After", "5"))
                    time.sleep(wait)
                    continue
                if resp.status >= 400:
                    raise DownloadError(f"HTTP {resp.status} for {url}")
                total = int(resp.headers.get("Content-Length", "0"))
                tmp = dest.with_suffix(dest.suffix + ".tmp")
                hasher512 = hashlib.sha512()
                hasher1 = hashlib.sha1()
                downloaded = 0
                with open(tmp, "wb") as f:
                    while True:
                        chunk = resp.read(1024*64)
                        if not chunk:
                            break
                        f.write(chunk)
                        hasher512.update(chunk)
                        hasher1.update(chunk)
                        downloaded += len(chunk)
                        if progress_cb:
                            progress_cb(downloaded, total)
                # verify hashes
                if expected_hashes:
                    if "sha512" in expected_hashes:
                        actual = hasher512.hexdigest()
                        if actual.lower() != expected_hashes["sha512"].lower():
                            tmp.unlink(missing_ok=True)
                            raise HashMismatchError(f"SHA512 mismatch for {url}\n expected {expected_hashes['sha512']}\n actual   {actual}")
                    elif "sha1" in expected_hashes:
                        actual = hasher1.hexdigest()
                        if actual.lower() != expected_hashes["sha1"].lower():
                            tmp.unlink(missing_ok=True)
                            raise HashMismatchError(f"SHA1 mismatch for {url}")
                tmp.replace(dest)
                return dest
        except HashMismatchError:
            raise
        except Exception as e:
            last_exc = e
            if attempt < retries-1:
                log.warning(f"Download failed for {url} attempt {attempt+1}/{retries}: {e} – retrying")
                time.sleep(1 + attempt*2)
                continue
            raise DownloadError(f"Failed to download {url} after {retries} attempts: {e}") from e
    raise DownloadError(f"Download failed {url}: {last_exc}")

# ──────────────────────────────────────────────────────────────────────────────
# Modrinth API
# ──────────────────────────────────────────────────────────────────────────────

MODRINTH_URL_RE = re.compile(
    r"https?://(?:www\.)?modrinth\.com/(?P<type>modpack|mod|plugin|resourcepack|shader|datapack)/(?P<slug>[a-zA-Z0-9_-]+)(?:/version/(?P<version>[a-zA-Z0-9._-]+))?",
    re.IGNORECASE
)
# Also handle api direct?
MRPACK_URL_RE = re.compile(r"https?://.*\.mrpack(\?.*)?$", re.IGNORECASE)

@dataclass
class ModrinthProject:
    id: str
    slug: str
    title: str
    description: str
    categories: List[str]
    author: str
    icon_url: Optional[str]
    versions: List[str]
    game_versions: List[str]
    loaders: List[str]
    raw: Dict[str,Any]

@dataclass
class ModrinthVersion:
    id: str
    project_id: str
    name: str
    version_number: str
    changelog: Optional[str]
    date_published: str
    game_versions: List[str]
    loaders: List[str]
    featured: bool
    files: List[Dict[str,Any]]  # primary file is mrpack
    dependencies: Dict[str,str]
    raw: Dict[str,Any]

    @property
    def primary_file(self) -> Optional[Dict[str,Any]]:
        for f in self.files:
            if f.get("primary"):
                return f
        return self.files[0] if self.files else None

    @property
    def mrpack_url(self) -> Optional[str]:
        pf = self.primary_file
        if pf:
            return pf.get("url")
        return None

    @property
    def mrpack_filename(self) -> Optional[str]:
        pf = self.primary_file
        if pf:
            return pf.get("filename")
        return None

def parse_modrinth_url(url: str) -> Tuple[str, Optional[str], Optional[str]]:
    """Returns (slug, version_id_or_slug, url_type). Raises if invalid."""
    url = url.strip()
    if MRPACK_URL_RE.match(url):
        return ("__direct__", None, "direct")
    m = MODRINTH_URL_RE.search(url)
    if not m:
        raise ModrinthError(
            f"Invalid Modrinth URL: {url}\n"
            "Expected format: https://modrinth.com/modpack/<pack-name> or https://modrinth.com/modpack/<pack-name>/version/<version-id>"
        )
    slug = m.group("slug")
    version = m.group("version")
    typ = m.group("type").lower()
    if typ != "modpack":
        log.warning(f"URL type is '{typ}' not 'modpack'; attempting to treat as modpack anyway")
    return (slug, version, "project")

class ModrinthClient:
    def __init__(self, base: str = MODRINTH_API):
        self.base = base.rstrip("/")

    def get_project(self, slug_or_id: str) -> ModrinthProject:
        url = f"{self.base}/project/{urllib.parse.quote(slug_or_id)}"
        data = http_get_json(url)
        return ModrinthProject(
            id=data["id"],
            slug=data.get("slug", slug_or_id),
            title=data.get("title", slug_or_id),
            description=data.get("description", ""),
            categories=data.get("categories", []),
            author=data.get("author", data.get("team", "")),
            icon_url=data.get("icon_url"),
            versions=data.get("versions", []),
            game_versions=data.get("game_versions", []),
            loaders=data.get("loaders", []),
            raw=data
        )

    def list_versions(self, project_id: str, loaders: Optional[List[str]]=None, game_versions: Optional[List[str]]=None) -> List[ModrinthVersion]:
        url = f"{self.base}/project/{urllib.parse.quote(project_id)}/version"
        # Modrinth supports query filtering but we fetch all and filter locally for simplicity
        data = http_get_json(url)
        versions = []
        for v in data:
            versions.append(self._parse_version(v))
        return versions

    def get_version(self, project_id: str, version_id: str) -> ModrinthVersion:
        url = f"{self.base}/project/{urllib.parse.quote(project_id)}/version/{urllib.parse.quote(version_id)}"
        data = http_get_json(url)
        return self._parse_version(data)

    def resolve_versions(self, slug_or_id: str) -> Tuple[ModrinthProject, List[ModrinthVersion]]:
        proj = self.get_project(slug_or_id)
        vers = self.list_versions(proj.id)
        # sort by date_published desc
        vers.sort(key=lambda v: v.date_published, reverse=True)
        return proj, vers

    def _parse_version(self, data: Dict[str,Any]) -> ModrinthVersion:
        return ModrinthVersion(
            id=data["id"],
            project_id=data["project_id"],
            name=data.get("name", data.get("version_number","")),
            version_number=data.get("version_number",""),
            changelog=data.get("changelog"),
            date_published=data.get("date_published",""),
            game_versions=data.get("game_versions",[]),
            loaders=data.get("loaders",[]),
            featured=data.get("featured", False),
            files=data.get("files",[]),
            dependencies=data.get("dependencies",{}),
            raw=data
        )

    def find_best_version(self, versions: List[ModrinthVersion]) -> Optional[ModrinthVersion]:
        if not versions:
            return None
        # prefer featured, else latest by date
        featured = [v for v in versions if v.featured]
        if featured:
            featured.sort(key=lambda v: v.date_published, reverse=True)
            return featured[0]
        # sort by date_published descending if not already
        sorted_versions = sorted(versions, key=lambda v: v.date_published, reverse=True)
        return sorted_versions[0]

# ──────────────────────────────────────────────────────────────────────────────
# Mrpack Parser
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class MrpackFile:
    path: str
    hashes: Dict[str,str]
    env: Dict[str,str]
    downloads: List[str]
    fileSize: int

@dataclass
class MrpackIndex:
    formatVersion: int
    game: str
    versionId: str
    name: str
    summary: Optional[str]
    files: List[MrpackFile]
    dependencies: Dict[str,str]
    raw: Dict[str,Any]

    @property
    def minecraft_version(self) -> str:
        return self.dependencies.get("minecraft", "unknown")

    @property
    def loader_type(self) -> str:
        # detect loader
        deps = self.dependencies
        for key in ["fabric-loader","forge","neoforge","quilt-loader","quilt","neoForge"]:
            if key in deps:
                return key
        # also check case variants
        for k,v in deps.items():
            lk = k.lower()
            if "fabric" in lk:
                return "fabric-loader"
            if lk == "forge":
                return "forge"
            if "neoforge" in lk:
                return "neoforge"
            if "quilt" in lk:
                return "quilt-loader"
        return "unknown"

    @property
    def loader_version(self) -> Optional[str]:
        lt = self.loader_type
        return self.dependencies.get(lt)

def parse_mrpack_index(data: Dict[str,Any]) -> MrpackIndex:
    required = ["formatVersion","game","versionId","name","files","dependencies"]
    for r in required:
        if r not in data:
            raise MrpackError(f"modrinth.index.json missing required key: {r}")
    files = []
    for f in data["files"]:
        if "path" not in f or "hashes" not in f or "downloads" not in f:
            raise MrpackError(f"Invalid file entry in modrinth.index.json: {f}")
        path = f["path"]
        # security: reject unsafe paths early
        if path.startswith("/") or ".." in Path(path).parts or ":" in path:
            # allow but will be rejected later via safe_join ; we log
            log.warning(f"Suspicious path in manifest (will be validated on extraction): {path}")
        files.append(MrpackFile(
            path=path,
            hashes=f.get("hashes",{}),
            env=f.get("env",{}),
            downloads=f.get("downloads",[]),
            fileSize=f.get("fileSize",0)
        ))
    return MrpackIndex(
        formatVersion=data["formatVersion"],
        game=data["game"],
        versionId=data["versionId"],
        name=data["name"],
        summary=data.get("summary"),
        files=files,
        dependencies=data["dependencies"],
        raw=data
    )

class MrpackParser:
    def __init__(self, mrpack_path: Path):
        self.mrpack_path = Path(mrpack_path)
        if not self.mrpack_path.exists():
            raise MrpackError(f"Mrpack not found: {mrpack_path}")

    def parse(self) -> Tuple[MrpackIndex, Path]:
        """Extract modrinth.index.json to temp and return index + temp dir containing overrides"""
        tmpdir = Path(tempfile.mkdtemp(prefix="mrpack_parse_"))
        try:
            with zipfile.ZipFile(self.mrpack_path, 'r') as z:
                # Validate zip for path traversal before extraction
                for info in z.infolist():
                    name = info.filename
                    # zip may contain absolute or traversal
                    if name.startswith("/") or ".." in Path(name).parts:
                        raise MrpackError(f"Unsafe path in .mrpack archive: {name}")
                # Must contain modrinth.index.json
                if "modrinth.index.json" not in z.namelist():
                    raise MrpackError("Invalid .mrpack: missing modrinth.index.json")
                index_data = json.loads(z.read("modrinth.index.json").decode("utf-8"))
                index = parse_mrpack_index(index_data)
                # Extract overrides safely
                for info in z.infolist():
                    if info.is_dir():
                        continue
                    if info.filename.startswith("overrides/"):
                        rel = info.filename[len("overrides/"):]
                        if not rel or rel.endswith("/"):
                            continue
                        dest = safe_join(tmpdir, rel)
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        with z.open(info) as src, open(dest, "wb") as dst:
                            shutil.copyfileobj(src, dst)
                    elif info.filename == "modrinth.index.json":
                        continue
                    else:
                        # spec allows only modrinth.index.json + overrides/, but be lenient
                        log.debug(f"Ignoring unexpected file in mrpack: {info.filename}")
                return index, tmpdir
        except zipfile.BadZipFile as e:
            shutil.rmtree(tmpdir, ignore_errors=True)
            raise MrpackError(f"Invalid or corrupted .mrpack zip: {e}") from e

    @staticmethod
    def validate_index(index: MrpackIndex):
        if index.game != "minecraft":
            raise MrpackError(f"Unsupported game: {index.game}, expected 'minecraft'")
        if not index.minecraft_version or index.minecraft_version == "unknown":
            log.warning("modrinth.index.json does not specify minecraft version")
        # warm: check file hashes present
        for f in index.files:
            if not f.hashes:
                log.warning(f"File without hash: {f.path}")
            if not f.downloads:
                log.warning(f"File without downloads: {f.path}")

# ──────────────────────────────────────────────────────────────────────────────
# Hash Verifier
# ──────────────────────────────────────────────────────────────────────────────

class HashVerifier:
    @staticmethod
    def verify_file(path: Path, expected: Dict[str,str]) -> bool:
        for algo, exp in expected.items():
            if algo not in ("sha512","sha1","sha256"):
                continue
            try:
                actual = compute_hash(path, algo)
            except Exception:
                return False
            if actual.lower() != exp.lower():
                return False
        return True

    @staticmethod
    def verify_bytes(data: bytes, expected: Dict[str,str]) -> bool:
        for algo, exp in expected.items():
            if algo not in ("sha512","sha1","sha256"):
                continue
            actual = compute_hash_bytes(data, algo)
            if actual.lower() != exp.lower():
                return False
        return True

# ──────────────────────────────────────────────────────────────────────────────
# Download Manager (concurrent)
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class DownloadTask:
    url: str
    dest: Path
    hashes: Dict[str,str]
    size: int
    path: str  # relative in instance

class DownloadManager:
    def __init__(self, concurrency: int = DEFAULT_CONCURRENCY, retries: int = DEFAULT_RETRIES, timeout: int = DEFAULT_TIMEOUT):
        self.concurrency = concurrency
        self.retries = retries
        self.timeout = timeout
        self._cancel = threading.Event()

    def cancel(self):
        self._cancel.set()

    def download_all(
        self,
        tasks: List[DownloadTask],
        progress_cb: Optional[Callable[[int,int,str],None]] = None,  # completed, total, current_file
        file_progress_cb: Optional[Callable[[str,int,int],None]] = None,
        verify_hashes: bool = True
    ) -> Tuple[int,int,List[str]]:
        total = len(tasks)
        completed = 0
        failed: List[str] = []
        total_bytes = sum(t.size for t in tasks)
        downloaded_bytes = 0
        lock = threading.Lock()
        start = time.time()

        def _download_one(task: DownloadTask) -> Optional[str]:
            nonlocal completed, downloaded_bytes
            if self._cancel.is_set():
                return f"Cancelled: {task.path}"
            try:
                def _prog(dl, tot):
                    if file_progress_cb:
                        file_progress_cb(task.path, dl, tot)
                # choose primary URL, retry across all URLs if needed
                last_err = None
                for url in task.downloads if hasattr(task, 'downloads') else [task.url]:
                    # task.url fallback
                    urls = task.downloads if task.downloads else [task.url]
                    for u in urls:
                        try:
                            # Check existing file hash skip logic inside http_download
                            http_download(u, task.dest, expected_hashes=task.hashes if verify_hashes else None, timeout=self.timeout, retries=self.retries, progress_cb=_prog)
                            return None
                        except HashMismatchError as e:
                            return str(e)
                        except Exception as e:
                            last_err = e
                            continue
                    break
                return f"{task.path}: {last_err}"
            except Exception as e:
                return f"{task.path}: {e}"
            finally:
                pass

        # Use ThreadPool
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.concurrency) as executor:
            # Map tasks
            future_to_task = {}
            for t in tasks:
                if self._cancel.is_set():
                    break
                # Expand DownloadTask to have downloads list if not present
                if not hasattr(t, 'downloads') or not t.downloads:
                    t.downloads = [t.url]
                fut = executor.submit(_download_one, t)
                future_to_task[fut] = t

            for fut in concurrent.futures.as_completed(future_to_task):
                if self._cancel.is_set():
                    # cancel remaining
                    for f in future_to_task:
                        f.cancel()
                    break
                task = future_to_task[fut]
                err = fut.result()
                with lock:
                    completed += 1
                    if err:
                        failed.append(err)
                        log.error(f"Failed: {err}")
                    else:
                        # approximate bytes
                        try:
                            if task.dest.exists():
                                downloaded_bytes += task.dest.stat().st_size
                        except Exception:
                            pass
                if progress_cb:
                    try:
                        progress_cb(completed, total, task.path)
                    except Exception:
                        pass
                # overall speed?
        return completed, total, failed

# ──────────────────────────────────────────────────────────────────────────────
# Minecraft Version & Loader Installers
# ──────────────────────────────────────────────────────────────────────────────

def fetch_vanilla_manifest() -> Dict[str,Any]:
    return http_get_json(VANILLA_MANIFEST_URL)

def fetch_vanilla_version_json(version_id: str) -> Dict[str,Any]:
    manifest = fetch_vanilla_manifest()
    for v in manifest.get("versions",[]):
        if v.get("id") == version_id:
            url = v.get("url")
            if url:
                return http_get_json(url)
    raise ModpackerError(f"Vanilla version not found in manifest: {version_id}")

def ensure_vanilla_version(mc_dir: Path, version_id: str, dry_run: bool = False, progress_cb: Optional[Callable[[str],None]]=None) -> Path:
    """Ensure .minecraft/versions/<id>/<id>.json and .jar exist, downloading if needed."""
    ver_dir = mc_dir / "versions" / version_id
    ver_json = ver_dir / f"{version_id}.json"
    ver_jar = ver_dir / f"{version_id}.jar"
    if ver_json.exists() and ver_jar.exists():
        log.info(f"Vanilla {version_id} already present")
        return ver_dir
    if dry_run:
        log.info(f"[dry-run] Would download vanilla {version_id} to {ver_dir}")
        return ver_dir
    log.info(f"Fetching vanilla version {version_id} metadata...")
    if progress_cb:
        progress_cb(f"Fetching vanilla {version_id}")
    data = fetch_vanilla_version_json(version_id)
    ver_dir.mkdir(parents=True, exist_ok=True)
    # save json
    with open(ver_json, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    # download client jar
    client = data.get("downloads",{}).get("client",{})
    url = client.get("url")
    sha1 = client.get("sha1")
    if url:
        log.info(f"Downloading vanilla client jar for {version_id}...")
        if progress_cb:
            progress_cb(f"Downloading vanilla {version_id} client")
        expected = {"sha1": sha1} if sha1 else {}
        try:
            http_download(url, ver_jar, expected_hashes=expected)
        except Exception as e:
            log.warning(f"Could not download vanilla jar (will still create version json): {e}")
    else:
        log.warning(f"No client download URL for {version_id}")
    return ver_dir

class LoaderInstaller:
    """Base loader installer."""
    def __init__(self, mc_dir: Path):
        self.mc_dir = Path(mc_dir)
    def loader_id(self) -> str:
        raise NotImplementedError
    def install(self, minecraft_version: str, loader_version: str, dry_run: bool=False, progress_cb: Optional[Callable[[str],None]]=None) -> str:
        """Install loader, return versionId string for launcher_profiles.json lastVersionId"""
        raise NotImplementedError
    def get_version_dir(self, version_id: str) -> Path:
        return self.mc_dir / "versions" / version_id

class FabricInstaller(LoaderInstaller):
    def loader_id(self) -> str:
        return "fabric-loader"
    def install(self, minecraft_version: str, loader_version: str, dry_run: bool=False, progress_cb: Optional[Callable[[str],None]]=None) -> str:
        version_id = f"fabric-loader-{loader_version}-{minecraft_version}"
        # Fabric version profile is typically like "fabric-loader-0.15.7-1.21.1"
        alt_id = f"{minecraft_version}-fabric-{loader_version}"
        # choose one style; vanilla fabric uses fabric-loader-xxx
        version_id = f"fabric-loader-{loader_version}-{minecraft_version}"
        ver_dir = self.get_version_dir(version_id)
        ver_json = ver_dir / f"{version_id}.json"
        if ver_json.exists():
            log.info(f"Fabric {loader_version} for {minecraft_version} already installed: {version_id}")
            return version_id
        if dry_run:
            log.info(f"[dry-run] Would install Fabric {loader_version} for {minecraft_version} -> {version_id}")
            return version_id
        log.info(f"Installing Fabric Loader {loader_version} for Minecraft {minecraft_version}...")
        if progress_cb:
            progress_cb(f"Installing Fabric {loader_version}")
        # Try to fetch from Fabric meta
        meta_url = f"{FABRIC_META_URL}/versions/loader/{urllib.parse.quote(minecraft_version)}/{urllib.parse.quote(loader_version)}/profile/json"
        try:
            profile = http_get_json(meta_url)
            # Fabric profile is a version json that inherits from vanilla
            ver_dir.mkdir(parents=True, exist_ok=True)
            with open(ver_json, "w", encoding="utf-8") as f:
                json.dump(profile, f, indent=2)
            log.info(f"Fabric profile installed: {version_id}")
            return version_id
        except Exception as e:
            log.warning(f"Failed to fetch Fabric profile from meta ({e}), creating synthetic profile")
            # Fallback: create synthetic json inheriting from vanilla
            ensure_vanilla_version(self.mc_dir, minecraft_version, dry_run=False, progress_cb=progress_cb)
            ver_dir.mkdir(parents=True, exist_ok=True)
            # Need to know Fabric loader maven coordinate: net.fabricmc:fabric-loader:xxx
            synthetic = {
                "id": version_id,
                "inheritsFrom": minecraft_version,
                "releaseTime": iso_now(),
                "time": iso_now(),
                "type": "release",
                "mainClass": "net.fabricmc.loader.impl.launch.knot.KnotClient",
                "arguments": {
                    "game": ["--clientId", "${clientid}", "--xuid", "${auth_xuid}", "--username", "${auth_player_name}", "--version", "${version_name}", "--gameDir", "${game_directory}", "--assetsDir", "${assets_root}", "--assetIndex", "${assets_index_name}", "--uuid", "${auth_uuid}", "--accessToken", "${auth_access_token}", "--userType", "${user_type}", "--versionType", "${version_type}"]
                },
                "libraries": [
                    {
                        "name": f"net.fabricmc:fabric-loader:{loader_version}",
                        "url": "https://maven.fabricmc.net/"
                    },
                    {
                        "name": "net.fabricmc:intermediary:{}".format(minecraft_version),
                        "url": "https://maven.fabricmc.net/"
                    }
                ]
            }
            with open(ver_json, "w", encoding="utf-8") as f:
                json.dump(synthetic, f, indent=2)
            return version_id

class QuiltInstaller(LoaderInstaller):
    def loader_id(self) -> str:
        return "quilt-loader"
    def install(self, minecraft_version: str, loader_version: str, dry_run=False, progress_cb=None) -> str:
        version_id = f"quilt-loader-{loader_version}-{minecraft_version}"
        ver_dir = self.get_version_dir(version_id)
        ver_json = ver_dir / f"{version_id}.json"
        if ver_json.exists():
            log.info(f"Quilt {loader_version} already installed")
            return version_id
        if dry_run:
            log.info(f"[dry-run] Would install Quilt {loader_version} -> {version_id}")
            return version_id
        log.info(f"Installing Quilt Loader {loader_version} for {minecraft_version}")
        if progress_cb:
            progress_cb(f"Installing Quilt {loader_version}")
        meta_url = f"{QUILT_META_URL}/versions/loader/{urllib.parse.quote(minecraft_version)}/{urllib.parse.quote(loader_version)}/profile/json"
        try:
            profile = http_get_json(meta_url)
            ver_dir.mkdir(parents=True, exist_ok=True)
            with open(ver_json, "w", encoding="utf-8") as f:
                json.dump(profile, f, indent=2)
            return version_id
        except Exception as e:
            log.warning(f"Quilt meta fetch failed ({e}), creating synthetic")
            ensure_vanilla_version(self.mc_dir, minecraft_version, dry_run=False, progress_cb=progress_cb)
            ver_dir.mkdir(parents=True, exist_ok=True)
            synthetic = {
                "id": version_id,
                "inheritsFrom": minecraft_version,
                "releaseTime": iso_now(),
                "time": iso_now(),
                "type": "release",
                "mainClass": "org.quiltmc.loader.impl.launch.knot.KnotClient",
                "libraries": [
                    {"name": f"org.quiltmc:quilt-loader:{loader_version}", "url": "https://maven.quiltmc.org/repository/release"}
                ]
            }
            with open(ver_json, "w", encoding="utf-8") as f:
                json.dump(synthetic, f, indent=2)
            return version_id

class ForgeInstaller(LoaderInstaller):
    def loader_id(self) -> str:
        return "forge"
    def install(self, minecraft_version: str, loader_version: str, dry_run=False, progress_cb=None) -> str:
        # Forge version string is often "<mc>-<forge>" e.g. "1.20.1-47.1.0" or just "47.1.0" where mc is separate
        # Normalize: if loader_version already contains "-", assume full
        if "-" in loader_version and loader_version.split("-")[0].count(".")>=2:
            full = loader_version
            forge_ver = loader_version.split("-",1)[1]
            mc_ver = loader_version.split("-",1)[0]
            # Use mc_ver if different? But dependencies.minecraft is authoritative
            version_id = f"{minecraft_version}-forge-{forge_ver}"
        else:
            # loader_version is like "47.1.0"
            forge_ver = loader_version
            version_id = f"{minecraft_version}-forge-{forge_ver}"
        ver_dir = self.get_version_dir(version_id)
        ver_json = ver_dir / f"{version_id}.json"
        if ver_json.exists():
            log.info(f"Forge {forge_ver} already installed: {version_id}")
            return version_id
        if dry_run:
            log.info(f"[dry-run] Would install Forge {forge_ver} for {minecraft_version} -> {version_id}")
            return version_id
        log.info(f"Installing Forge {forge_ver} for {minecraft_version}...")
        if progress_cb:
            progress_cb(f"Installing Forge {forge_ver}")
        ensure_vanilla_version(self.mc_dir, minecraft_version, dry_run=False, progress_cb=progress_cb)
        # Try to fetch Forge version json if available via maven? Forge doesn't have simple json meta like Fabric.
        # Attempt to download installer info? We will create synthetic but try to fetch libraries list from files.minecraftforge.net
        # For robustness, create synthetic that inherits and adds forge libraries
        ver_dir.mkdir(parents=True, exist_ok=True)
        # Forge mainClass historically: cpw.mods.bootstraplauncher.BootstrapLauncher or net.minecraft.launchwrapper.Launch
        # For modern Forge (1.17+), it's cpw.mods.bootstraplauncher.BootstrapLauncher
        # We'll attempt to detect forge json via https://maven.minecraftforge.net/net/minecraftforge/forge/<minecraft_version>-<forge_ver>/forge-<minecraft_version>-<forge_ver>.json
        # Actually Forge publishes installer json at .../forge-<ver>-installer.jar contains version.json
        synthetic_libs = []
        forge_maven_coord = f"net.minecraftforge:forge:{minecraft_version}-{forge_ver}"
        # Some versions use net.minecraftforge:forge
        synthetic = {
            "id": version_id,
            "inheritsFrom": minecraft_version,
            "releaseTime": iso_now(),
            "time": iso_now(),
            "type": "release",
            "mainClass": "cpw.mods.bootstraplauncher.BootstrapLauncher",
            "arguments": {
                "game": ["--launchTarget", "forgeclient", "--fml.forgeVersion", forge_ver, "--fml.mcVersion", minecraft_version, "--fml.forgeGroup", "net.minecraftforge", "--fml.mcpVersion", "unknown"],
                "jvm": ["-Dforgewrapper.main", "net.minecraft.launchwrapper.Launch"]
            },
            "libraries": [
                {"name": forge_maven_coord, "url": "https://maven.minecraftforge.net/"},
                {"name": "cpw.mods:securejarhandler:2.1.4"},
                {"name": "org.ow2.asm:asm:9.6"},
                {"name": "net.minecraftforge:accesstransformers:8.0.4"}
            ]
        }
        # Try remote forge json fetch for more accurate libs
        remote_urls = [
            f"https://maven.minecraftforge.net/net/minecraftforge/forge/{minecraft_version}-{forge_ver}/forge-{minecraft_version}-{forge_ver}.json",
            f"https://files.minecraftforge.net/maven/net/minecraftforge/forge/{minecraft_version}-{forge_ver}/forge-{minecraft_version}-{forge_ver}.json",
        ]
        fetched = False
        for u in remote_urls:
            try:
                data = http_get_json(u)
                # This file is usually installer profile, not version json, but try
                if isinstance(data, dict) and "libraries" in data:
                    synthetic["libraries"] = data["libraries"]
                    fetched = True
                    break
            except Exception:
                continue
        if fetched:
            log.info("Fetched Forge libraries from Maven")
        with open(ver_json, "w", encoding="utf-8") as f:
            json.dump(synthetic, f, indent=2)
        log.info(f"Forge profile created: {version_id} (note: first launch may download additional libraries via Minecraft Launcher)")
        return version_id

class NeoForgeInstaller(LoaderInstaller):
    def loader_id(self) -> str:
        return "neoforge"
    def install(self, minecraft_version: str, loader_version: str, dry_run=False, progress_cb=None) -> str:
        # NeoForge versioning: neoforge-<version> where version like "20.4.196" corresponds to mc 1.20.4?
        # But dependencies may just be "20.4.196" or "1.20.4-20.4.196"? We'll normalize similar to Forge
        neo_ver = loader_version
        # If loader_version contains "-", strip mc prefix
        if "-" in neo_ver:
            parts = neo_ver.split("-")
            # If first part looks like mc version
            if parts[0].count(".")>=2:
                neo_ver = parts[-1]
        version_id = f"{minecraft_version}-neoforge-{neo_ver}"
        # Alternate id style used by NeoForge installer: "neoforge-20.4.196"
        alt_id = f"neoforge-{neo_ver}"
        ver_dir = self.get_version_dir(version_id)
        ver_json = ver_dir / f"{version_id}.json"
        if ver_json.exists():
            log.info(f"NeoForge {neo_ver} already installed")
            return version_id
        if dry_run:
            log.info(f"[dry-run] Would install NeoForge {neo_ver} -> {version_id}")
            return version_id
        log.info(f"Installing NeoForge {neo_ver} for {minecraft_version}")
        if progress_cb:
            progress_cb(f"Installing NeoForge {neo_ver}")
        ensure_vanilla_version(self.mc_dir, minecraft_version, dry_run=False, progress_cb=progress_cb)
        ver_dir.mkdir(parents=True, exist_ok=True)
        synthetic = {
            "id": version_id,
            "inheritsFrom": minecraft_version,
            "releaseTime": iso_now(),
            "time": iso_now(),
            "type": "release",
            "mainClass": "cpw.mods.bootstraplauncher.BootstrapLauncher",
            "arguments": {
                "game": ["--launchTarget", "forgeclient", "--fml.neoForgeVersion", neo_ver, "--fml.mcVersion", minecraft_version],
            },
            "libraries": [
                {"name": f"net.neoforged:neoforge:{neo_ver}", "url": "https://maven.neoforged.net/releases"}
            ]
        }
        # Try fetch from neoforge maven
        try:
            # NeoForge also publishes version json? Attempt
            url = f"https://maven.neoforged.net/releases/net/neoforged/neoforge/{neo_ver}/neoforge-{neo_ver}.json"
            data = http_get_json(url)
            if isinstance(data, dict) and "libraries" in data:
                synthetic["libraries"] = data["libraries"]
        except Exception:
            pass
        with open(ver_json, "w", encoding="utf-8") as f:
            json.dump(synthetic, f, indent=2)
        return version_id

def get_loader_installer(loader_type: str, mc_dir: Path) -> LoaderInstaller:
    lt = loader_type.lower()
    if "fabric" in lt:
        return FabricInstaller(mc_dir)
    if lt == "forge":
        return ForgeInstaller(mc_dir)
    if "neoforge" in lt or lt == "neoForge":
        return NeoForgeInstaller(mc_dir)
    if "quilt" in lt:
        return QuiltInstaller(mc_dir)
    # unknown loader -> treat as vanilla?
    raise ModpackerError(f"Unsupported loader: {loader_type}. Supported: fabric-loader, forge, neoforge, quilt-loader")

# ──────────────────────────────────────────────────────────────────────────────
# System Dependencies (Vista / native package managers)
# ──────────────────────────────────────────────────────────────────────────────
#
# `--check-deps` reports whether Java, Tkinter and a Minecraft launcher exist.
# `--install-deps [--pm vista|dnf|apt|pacman|zypper|apk|flatpak|auto]` installs
# whatever is missing. `--pm vista` shells out to the Vista universal package
# manager (https://github.com/whyfle/vista), e.g.:
#   vista install adoptium@temurin21-binaries -y
#   vista install org.prismlauncher.PrismLauncher --default flathub -y

SYSTEM_PACKAGE_MANAGERS = ("vista", "dnf", "apt", "pacman", "zypper", "apk", "flatpak")

# Binaries probed per manager (first hit wins for detection).
_PM_BINARIES = {
    "vista": ("vista",),
    "dnf": ("dnf", "dnf5"),
    "apt": ("apt", "apt-get"),
    "pacman": ("pacman",),
    "zypper": ("zypper",),
    "apk": ("apk",),
    "flatpak": ("flatpak",),
}

# dep -> per-manager package/spec. `vista` holds a vista install spec
# (None = not installable via vista, use native PM instead).
SYSTEM_DEPS: Dict[str, Dict[str, Any]] = {
    "java": {
        "label": "Java 21 Runtime (required for Minecraft 1.20.5+)",
        "vista": "adoptium@temurin21-binaries",
        "dnf": "java-21-openjdk",
        "apt": "openjdk-21-jre",
        "pacman": "jre21-openjdk",
        "zypper": "java-21-openjdk",
        "apk": "openjdk21-jre",
        "flatpak": None,
    },
    "tkinter": {
        "label": "Tkinter (required for --gui)",
        "vista": None,  # distro python binding; always via native PM
        "dnf": "python3-tkinter",
        "apt": "python3-tk",
        "pacman": "tk",
        "zypper": "python3-tk",
        "apk": "py3-tkinter",
        "flatpak": None,
    },
    "launcher": {
        "label": "Minecraft launcher (official or PrismLauncher)",
        "vista": "org.prismlauncher.PrismLauncher --default flathub",
        "dnf": None,  # official launcher: download from minecraft.net
        "apt": None,
        "pacman": "prismlauncher",
        "zypper": None,
        "apk": None,
        "flatpak": "org.prismlauncher.PrismLauncher",
    },
}

def detect_package_managers() -> List[str]:
    """Return installed system package managers, vista first."""
    found = []
    for pm in SYSTEM_PACKAGE_MANAGERS:
        for binary in _PM_BINARIES[pm]:
            if shutil.which(binary):
                found.append(pm)
                break
    return found

def check_java() -> Tuple[bool, str]:
    """Check for a usable Java runtime (17+, ideally 21)."""
    java = shutil.which("java")
    if not java:
        return False, "no `java` on PATH"
    try:
        out = subprocess.run([java, "-version"], capture_output=True, text=True, timeout=10)
        text = (out.stderr or "") + (out.stdout or "")
        m = re.search(r'version "(\d+)(?:\.(\d+))?', text)
        if not m:
            return True, f"found at {java} (version unparseable)"
        major = int(m.group(1))
        if major == 1 and m.group(2):
            major = int(m.group(2))  # old-style "1.8.0" -> 8
        detail = f"{text.strip().splitlines()[0] if text.strip() else java}"
        if major >= 21:
            return True, detail
        if major >= 17:
            return True, detail + " (works; 21 recommended for 1.20.5+)"
        return False, detail + " (too old; need 17+, ideally 21)"
    except Exception as e:
        return False, f"found at {java} but `-version` failed: {e}"

def check_tkinter() -> Tuple[bool, str]:
    """Check whether this Python has tkinter (needed for --gui)."""
    try:
        out = subprocess.run(
            [sys.executable, "-c", "import tkinter"],
            capture_output=True, text=True, timeout=15,
        )
        if out.returncode == 0:
            return True, f"available ({sys.executable})"
        err = (out.stderr or "").strip().splitlines()
        return False, err[0] if err else "import tkinter failed"
    except Exception as e:
        return False, f"check failed: {e}"

def check_launcher() -> Tuple[bool, str]:
    """Check for an installed Minecraft launcher (official or Prism)."""
    if shutil.which("prismlauncher"):
        return True, "PrismLauncher on PATH"
    if shutil.which("minecraft-launcher"):
        return True, "official Minecraft Launcher on PATH"
    if shutil.which("flatpak"):
        try:
            out = subprocess.run(
                ["flatpak", "list", "--app", "--columns=application"],
                capture_output=True, text=True, timeout=15,
            )
            apps = (out.stdout or "").split()
            if "org.prismlauncher.PrismLauncher" in apps:
                return True, "PrismLauncher (Flatpak)"
            if "com.mojang.Minecraft" in apps:
                return True, "official launcher (Flatpak)"
        except Exception:
            pass
    return False, "no launcher found (official minecraft-launcher or PrismLauncher)"

_DEP_CHECKERS = {
    "java": check_java,
    "tkinter": check_tkinter,
    "launcher": check_launcher,
}

def check_system_deps() -> Dict[str, Dict[str, Any]]:
    """Check all system deps. Returns {name: {installed, detail, label}}."""
    result = {}
    for name, spec in SYSTEM_DEPS.items():
        try:
            installed, detail = _DEP_CHECKERS[name]()
        except Exception as e:
            installed, detail = False, f"check failed: {e}"
        result[name] = {"installed": installed, "detail": detail, "label": spec["label"]}
    return result

def _needs_root(pm: str) -> bool:
    return pm in ("dnf", "apt", "pacman", "zypper", "apk")

def build_dep_install_command(dep: str, pm: str, yes: bool = False) -> Optional[List[str]]:
    """Build the install command for `dep` via `pm`. None if unsupported."""
    spec = SYSTEM_DEPS.get(dep)
    if not spec:
        raise ModpackerError(f"Unknown dependency: {dep}. Known: {sorted(SYSTEM_DEPS)}")
    if pm == "vista":
        vista_spec = spec.get("vista")
        if not vista_spec:
            return None
        cmd = ["vista", "install"] + vista_spec.split()
        if yes:
            cmd.append("-y")
        return cmd
    pkg = spec.get(pm)
    if not pkg:
        return None
    # Native managers need root; prepend sudo when available and not root.
    prefix: List[str] = []
    try:
        is_root = (os.geteuid() == 0)
    except AttributeError:
        is_root = False  # Windows: no sudo concept here
    if _needs_root(pm) and not is_root and shutil.which("sudo"):
        prefix = ["sudo"]
    if pm == "dnf":
        binary = "dnf" if shutil.which("dnf") else "dnf5"
        return prefix + [binary, "install", "-y", pkg]
    if pm == "apt":
        return prefix + ["apt-get", "install", "-y", pkg]
    if pm == "pacman":
        return prefix + ["pacman", "-S", "--noconfirm", "--needed", pkg]
    if pm == "zypper":
        return prefix + ["zypper", "install", "-y", pkg]
    if pm == "apk":
        return prefix + ["apk", "add", pkg]
    if pm == "flatpak":
        return ["flatpak", "install", "-y", "flathub", pkg]
    raise ModpackerError(f"Unsupported package manager: {pm}")

def resolve_pm(pm: str) -> str:
    """Resolve `auto` to the best available manager (vista preferred)."""
    available = detect_package_managers()
    if pm != "auto":
        if pm not in available:
            raise ModpackerError(
                f"Package manager '{pm}' not found on PATH. Available: {available or 'none'}"
            )
        return pm
    for preferred in ("vista", "dnf", "apt", "pacman", "zypper", "apk"):
        if preferred in available:
            return preferred
    if "flatpak" in available:
        return "flatpak"
    raise ModpackerError("No supported package manager found (vista, dnf, apt, pacman, zypper, apk, flatpak)")

def install_system_deps(pm: str = "auto", only: Optional[List[str]] = None,
                        dry_run: bool = False, yes: bool = False) -> int:
    """Install missing system deps via `pm`. Returns number installed (dry-run counts planned)."""
    manager = resolve_pm(pm)
    wanted = list(only) if only else list(SYSTEM_DEPS)
    unknown = [d for d in wanted if d not in SYSTEM_DEPS]
    if unknown:
        raise ModpackerError(f"Unknown dependencies: {unknown}. Known: {sorted(SYSTEM_DEPS)}")
    status = check_system_deps()
    installed = 0
    for dep in wanted:
        if status[dep]["installed"]:
            print(f"  ✓ {dep}: already installed ({status[dep]['detail']})")
            continue
        cmd = build_dep_install_command(dep, manager, yes=yes)
        if cmd is None:
            print(f"  ✗ {dep}: '{manager}' cannot provide this ({status[dep]['detail']}). "
                  f"Install manually: {SYSTEM_DEPS[dep]['label']}")
            continue
        print(f"  → {dep}: {' '.join(cmd)}")
        if dry_run:
            installed += 1
            continue
        try:
            subprocess.run(cmd, check=True)
            installed += 1
            print(f"  ✓ {dep}: installed")
        except subprocess.CalledProcessError as e:
            print(f"  ✗ {dep}: command failed (exit {e.returncode}). Re-run with --verbose or install manually.")
        except FileNotFoundError:
            print(f"  ✗ {dep}: `{cmd[0]}` not found despite detection. Re-run --check-deps.")
    return installed

# ──────────────────────────────────────────────────────────────────────────────
# Launcher Integration (launcher_profiles.json)
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class LauncherProfile:
    name: str
    gameDir: str
    lastVersionId: str
    icon: str = "Grass"
    type: str = "custom"
    created: str = field(default_factory=iso_now)
    lastUsed: str = field(default_factory=iso_now)

def load_launcher_profiles(mc_dir: Path) -> Tuple[Dict[str,Any], Path]:
    p = mc_dir / "launcher_profiles.json"
    if not p.exists():
        # Create minimal structure
        data = {"profiles": {}, "settings": {}, "version": 3}
        return data, p
    with open(p, "r", encoding="utf-8") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as e:
            raise ModpackerError(f"Corrupted launcher_profiles.json: {e}. Please restore from backup at {p}.bak")
    if "profiles" not in data:
        data["profiles"] = {}
    return data, p

def backup_launcher_profiles(mc_dir: Path):
    src = mc_dir / "launcher_profiles.json"
    if src.exists():
        backup = src.with_suffix(".json.bak")
        # keep timestamped backups too
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_ts = src.with_name(f"launcher_profiles.json.bak.{ts}")
        try:
            shutil.copy2(src, backup)
            shutil.copy2(src, backup_ts)
            log.info(f"Backed up launcher_profiles.json to {backup} and {backup_ts}")
        except Exception as e:
            log.warning(f"Could not backup launcher_profiles.json: {e}")

def inject_launcher_profile(
    mc_dir: Path,
    slug: str,
    profile: LauncherProfile,
    dry_run: bool = False
) -> str:
    """Inject profile, return uuid key used."""
    data, path = load_launcher_profiles(mc_dir)
    # Use slug as key if not exists, else uuid, but spec says "<unique-uuid-or-slug>"
    # We'll generate deterministic uuid5 from slug to keep id stable across reinstalls
    # Use uuid5 with NAMESPACE_URL
    stable_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"modpacker:{slug}"))
    key = stable_uuid
    # If slug already exists as key, migrate?
    # Check if any existing profile has same gameDir or name, reuse key
    existing_key = None
    for k, v in data["profiles"].items():
        if isinstance(v, dict) and v.get("gameDir") == profile.gameDir:
            existing_key = k
            break
        if isinstance(v, dict) and v.get("name") == profile.name and v.get("gameDir") == profile.gameDir:
            existing_key = k
            break
    if existing_key:
        key = existing_key
        log.info(f"Updating existing launcher profile: {key}")
    else:
        log.info(f"Creating new launcher profile: {key} -> {profile.name}")

    entry = {
        "created": profile.created,
        "gameDir": profile.gameDir,
        "icon": profile.icon,
        "lastUsed": profile.lastUsed,
        "lastVersionId": profile.lastVersionId,
        "name": profile.name,
        "type": profile.type
    }
    # Preserve created date if updating
    if key in data["profiles"] and "created" in data["profiles"][key]:
        entry["created"] = data["profiles"][key]["created"]

    data["profiles"][key] = entry

    if dry_run:
        log.info(f"[dry-run] Would write launcher_profiles.json with profile {key}: {entry}")
        return key

    backup_launcher_profiles(mc_dir)
    # Atomic write
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    # Validate JSON before replacing
    with open(tmp, "r", encoding="utf-8") as f:
        json.load(f)
    tmp.replace(path)
    log.info(f"Launcher profile injected: {key} ({profile.name}) -> {profile.lastVersionId}")
    return key

# ──────────────────────────────────────────────────────────────────────────────
# Installation State Tracking
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class InstalledPack:
    slug: str
    name: str
    project_id: str
    version_id: str
    minecraft_version: str
    loader: str
    loader_version: str
    version_dir_id: str  # lastVersionId
    install_dir: str
    installed_at: str
    updated_at: str
    files_count: int
    total_size: int

def load_installation_state(mc_dir: Path) -> Dict[str,Any]:
    p = mc_dir / "modpacker" / "installed_packs.json"
    if not p.exists():
        return {"packs": {}}
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"packs": {}}

def save_installation_state(mc_dir: Path, pack: InstalledPack):
    state_path = mc_dir / "modpacker" / "installed_packs.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state = load_installation_state(mc_dir)
    state["packs"][pack.slug] = asdict(pack)
    tmp = state_path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    tmp.replace(state_path)

def get_installed_pack(mc_dir: Path, slug: str) -> Optional[InstalledPack]:
    state = load_installation_state(mc_dir)
    data = state.get("packs",{}).get(slug)
    if data:
        return InstalledPack(**data)
    return None

# ──────────────────────────────────────────────────────────────────────────────
# Core Installer Orchestrator
# ──────────────────────────────────────────────────────────────────────────────

class ModpackInstaller:
    def __init__(
        self,
        minecraft_dir: Optional[Path] = None,
        concurrency: int = DEFAULT_CONCURRENCY,
        keep_mrpack: bool = False,
        verify_hashes: bool = True,
        dry_run: bool = False,
        progress_callback: Optional[Callable[[str,int,int,str],None]] = None,  # stage, current, total, detail
        log_callback: Optional[Callable[[str],None]] = None,
    ):
        self.mc_dir = get_minecraft_dir(str(minecraft_dir) if minecraft_dir else None)
        self.concurrency = concurrency
        self.keep_mrpack = keep_mrpack
        self.verify_hashes = verify_hashes
        self.dry_run = dry_run
        self.progress_cb = progress_callback
        self.log_cb = log_callback
        self.client = ModrinthClient()
        self.download_mgr = DownloadManager(concurrency=concurrency)
        self._overrides_tmp: Optional[Path] = None

    def log(self, msg: str):
        log.info(msg)
        if self.log_cb:
            try:
                self.log_cb(msg)
            except Exception:
                pass

    def progress(self, stage: str, current: int, total: int, detail: str = ""):
        if self.progress_cb:
            try:
                self.progress_cb(stage, current, total, detail)
            except Exception:
                pass

    def install_from_url(self, url: str, selected_version_id: Optional[str] = None, game_dir_custom: Optional[Path]=None) -> InstalledPack:
        url = url.strip()
        # Direct .mrpack URL
        if MRPACK_URL_RE.match(url):
            return self.install_from_mrpack_url(url, game_dir_custom=game_dir_custom)
        slug, version_hint, typ = parse_modrinth_url(url)
        self.log(f"Fetching Modrinth project: {slug}")
        self.progress("Fetching Modrinth project", 0, 100, slug)
        warn_if_launcher_running()

        proj = self.client.get_project(slug)
        self.log(f"Found project: {proj.title} ({proj.id}) - {proj.description[:120]}")
        versions = self.client.list_versions(proj.id)
        if not versions:
            raise ModrinthError(f"No versions found for project {slug}")

        target_version: Optional[ModrinthVersion] = None
        if selected_version_id:
            for v in versions:
                if v.id == selected_version_id or v.version_number == selected_version_id:
                    target_version = v
                    break
            if not target_version:
                raise ModrinthError(f"Version {selected_version_id} not found for {proj.title}")
        elif version_hint:
            # URL contained /version/<hint>
            for v in versions:
                if v.id == version_hint or v.version_number == version_hint or v.id.startswith(version_hint):
                    target_version = v
                    break
            if not target_version:
                # fallback to best
                log.warning(f"Version hint {version_hint} not found, using latest")
                target_version = self.client.find_best_version(versions)
        else:
            target_version = self.client.find_best_version(versions)

        assert target_version is not None
        self.log(f"Selected version: {target_version.name} ({target_version.version_number}) - {target_version.id}")
        self.log(f"Game versions: {target_version.game_versions} Loaders: {target_version.loaders}")

        # Find primary .mrpack file
        mrpack_file = target_version.primary_file
        if not mrpack_file or not mrpack_file.get("url", "").endswith(".mrpack"):
            # try any file ending with .mrpack
            for f in target_version.files:
                if f.get("filename","").endswith(".mrpack") or f.get("url","").endswith(".mrpack"):
                    mrpack_file = f
                    break
        if not mrpack_file:
            raise ModrinthError(f"No .mrpack file found for version {target_version.version_number}. Files: {target_version.files}")

        # Download .mrpack
        return self.install_from_version(proj, target_version, mrpack_file, game_dir_custom=game_dir_custom)

    def install_from_mrpack_url(self, url: str, game_dir_custom: Optional[Path]=None) -> InstalledPack:
        self.log(f"Downloading .mrpack directly from {url}")
        warn_if_launcher_running()
        tmp = Path(tempfile.gettempdir()) / f"modpacker_direct_{uuid.uuid4().hex}.mrpack"
        http_download(url, tmp)
        # Try to parse to get name
        parser = MrpackParser(tmp)
        index, overrides_tmp = parser.parse()
        self._overrides_tmp = overrides_tmp
        # Need to fabricate project/version for state
        proj_fake = ModrinthProject(id="direct", slug=slugify(index.name), title=index.name, description=index.summary or "", categories=[], author="", icon_url=None, versions=[], game_versions=[index.minecraft_version], loaders=[index.loader_type], raw={})
        ver_fake = ModrinthVersion(id=index.versionId, project_id="direct", name=index.name, version_number=index.versionId, changelog=None, date_published=iso_now(), game_versions=[index.minecraft_version], loaders=[index.loader_type], featured=False, files=[], dependencies=index.dependencies, raw={})
        pack = self._install_from_index(index, overrides_tmp, proj_fake, ver_fake, tmp, game_dir_custom=game_dir_custom)
        if not self.keep_mrpack and not self.dry_run:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
        return pack

    def install_from_version(self, proj: ModrinthProject, version: ModrinthVersion, mrpack_file: Dict[str,Any], game_dir_custom: Optional[Path]=None) -> InstalledPack:
        hashes = mrpack_file.get("hashes",{})
        url = mrpack_file.get("url")
        filename = mrpack_file.get("filename", f"{proj.slug}.mrpack")
        if not url:
            raise ModrinthError("Version file has no URL")
        # Use cache dir for mrpacks
        cache_dir = self.mc_dir / "modpacker" / "cache"
        if not self.dry_run:
            cache_dir.mkdir(parents=True, exist_ok=True)
        mrpack_path = cache_dir / filename
        # If hashes provided, use them
        expected = {}
        if "sha512" in hashes:
            expected["sha512"] = hashes["sha512"]
        elif "sha1" in hashes:
            expected["sha1"] = hashes["sha1"]
        self.log(f"Downloading .mrpack: {filename} ({human_size(mrpack_file.get('size',0))})")
        self.progress("Downloading modpack", 0, 100, filename)
        # Always download .mrpack (even in dry-run) so we can parse real modrinth.index.json; dry-run only avoids mutating gameDir/launcher
        if self.dry_run:
            self.log(f"[dry-run] Downloading for preview (no launcher mutation): {url}")
        # Ensure cache dir exists even for dry-run preview (use temp if needed)
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            # fallback to temp
            cache_dir = Path(tempfile.gettempdir()) / "modpacker_cache"
            cache_dir.mkdir(parents=True, exist_ok=True)
            mrpack_path = cache_dir / filename
        try:
            http_download(url, mrpack_path, expected_hashes=expected if self.verify_hashes and expected else None)
        except Exception as e:
            if self.dry_run and not mrpack_path.exists():
                self.log(f"[dry-run] Could not download .mrpack for preview ({e}); using synthetic index")
                deps = {}
                if version.game_versions:
                    deps["minecraft"] = version.game_versions[0]
                if version.loaders:
                    loader = version.loaders[0]
                    if loader == "fabric":
                        deps["fabric-loader"] = "0.15.0"
                    elif loader in ("forge","neoforge","quilt"):
                        deps[loader] = "unknown"
                    else:
                        deps[loader] = "unknown"
                fake_index = MrpackIndex(
                    formatVersion=1,
                    game="minecraft",
                    versionId=version.id,
                    name=proj.title,
                    summary=proj.description,
                    files=[],
                    dependencies=deps,
                    raw={}
                )
                return self._install_from_index(fake_index, Path(tempfile.mkdtemp()), proj, version, mrpack_path, dry_run_preview=True, game_dir_custom=game_dir_custom)
            else:
                raise

        parser = MrpackParser(mrpack_path)
        index, overrides_tmp = parser.parse()
        self._overrides_tmp = overrides_tmp
        self.log(f"Parsed modrinth.index.json: {index.name} - {index.minecraft_version} / {index.loader_type} {index.loader_version}")
        pack = self._install_from_index(index, overrides_tmp, proj, version, mrpack_path, game_dir_custom=game_dir_custom)
        # cleanup
        if overrides_tmp and overrides_tmp.exists():
            shutil.rmtree(overrides_tmp, ignore_errors=True)
        return pack

    def _install_from_index(
        self,
        index: MrpackIndex,
        overrides_dir: Path,
        proj: ModrinthProject,
        version: ModrinthVersion,
        mrpack_path: Path,
        dry_run_preview: bool = False,
        game_dir_custom: Optional[Path] = None
    ) -> InstalledPack:
        slug = slugify(proj.slug if hasattr(proj,'slug') else index.name)
        # Use project slug for dir name if available
        if hasattr(proj, 'slug') and proj.slug and proj.slug != "direct":
            slug = slugify(proj.slug)
        else:
            slug = slugify(index.name)

        # Check existing installation
        existing = get_installed_pack(self.mc_dir, slug)
        if existing and not self.dry_run and not dry_run_preview:
            self.log(f"Existing installation found for {slug} ({existing.name}). Will update/reinstall.")
            # We proceed to reinstall; could offer clean update logic

        # Determine gameDir
        if game_dir_custom:
            game_dir = Path(game_dir_custom).expanduser().resolve()
        else:
            # Per spec: %APPDATA%/.minecraft/profiles/<slug>
            # Also spec says .minecraft/modpacks/<slug> is alternative; we support both but default to profiles
            game_dir = get_profiles_dir(self.mc_dir) / slug

        self.log(f"Game directory: {game_dir}")
        if not self.dry_run and not dry_run_preview:
            game_dir.mkdir(parents=True, exist_ok=True)
        else:
            self.log(f"[dry-run] Would create gameDir {game_dir}")

        # Preview info
        total_files = len(index.files)
        total_size = sum(f.fileSize for f in index.files)
        self.log(f"Modpack has {total_files} files, approx {human_size(total_size)}")
        self.progress("Resolving dependencies", 0, total_files, f"{total_files} files")

        # Loader setup
        mc_version = index.minecraft_version
        loader_type = index.loader_type
        loader_version = index.loader_version
        version_id_for_launcher = mc_version  # fallback vanilla

        if mc_version == "unknown":
            raise MrpackError("Cannot determine Minecraft version from modrinth.index.json dependencies")
        if loader_type == "unknown" or not loader_version:
            log.warning(f"Could not detect modloader from dependencies {index.dependencies}; assuming vanilla")
            # Still ensure vanilla
            if not self.dry_run and not dry_run_preview:
                ensure_vanilla_version(self.mc_dir, mc_version, dry_run=self.dry_run, progress_cb=self.log)
            version_id_for_launcher = mc_version
        else:
            if not self.dry_run and not dry_run_preview:
                ensure_vanilla_version(self.mc_dir, mc_version, dry_run=self.dry_run, progress_cb=self.log)
            installer = get_loader_installer(loader_type, self.mc_dir)
            version_id_for_launcher = installer.install(mc_version, loader_version, dry_run=self.dry_run or dry_run_preview, progress_cb=self.log)

        self.log(f"Launcher versionId: {version_id_for_launcher}")

        # Prepare file downloads
        # Only download files where env client != unsupported?
        # Spec: env client required/optional
        tasks: List[DownloadTask] = []
        skipped_env = 0
        for f in index.files:
            env_client = f.env.get("client", "required")
            if env_client == "unsupported":
                skipped_env += 1
                continue
            # Also support server-only files skip
            # Destination is game_dir / f.path
            dest = game_dir / f.path
            # Normalize path: ensure safe
            # safe_join already checks traversal, but we want to ensure dest is under game_dir
            try:
                safe_join(game_dir, f.path)
            except ValueError as e:
                log.warning(f"Skipping file with unsafe path {f.path}: {e}")
                continue
            if not f.downloads:
                log.warning(f"Skipping file with no downloads: {f.path}")
                continue
            # Use first URL as primary, but keep all for fallback
            primary_url = f.downloads[0]
            task = DownloadTask(
                url=primary_url,
                dest=dest,
                hashes=f.hashes,
                size=f.fileSize,
                path=f.path
            )
            # Keep all downloads for fallback
            task.downloads = f.downloads  # type: ignore
            tasks.append(task)

        self.log(f"Prepared {len(tasks)} download tasks ({skipped_env} skipped due to env=unsupported)")
        if self.dry_run or dry_run_preview:
            # Dry-run: show directory structure
            self.log("=== DRY RUN: Directory structure preview ===")
            self.log(f"Minecraft dir: {self.mc_dir}")
            self.log(f"GameDir: {game_dir}")
            self.log(f"  mods/ <- {len([t for t in tasks if t.path.startswith('mods/')])} files")
            self.log(f"  config/ etc from overrides: {list(overrides_dir.rglob('*'))[:10] if overrides_dir.exists() else 'none'}")
            self.log(f"Version {version_id_for_launcher} would be created in {self.mc_dir / 'versions' / version_id_for_launcher}")
            self.log(f"launcher_profiles.json would be injected with profile '{index.name}' -> {version_id_for_launcher}")
            # Simulate downloading? no
            pack = InstalledPack(
                slug=slug,
                name=index.name,
                project_id=proj.id,
                version_id=version.id,
                minecraft_version=mc_version,
                loader=loader_type,
                loader_version=loader_version or "",
                version_dir_id=version_id_for_launcher,
                install_dir=str(game_dir),
                installed_at=iso_now(),
                updated_at=iso_now(),
                files_count=len(tasks),
                total_size=total_size
            )
            return pack

        # Check disk space (rough)
        try:
            free = shutil.disk_usage(game_dir).free if game_dir.exists() else shutil.disk_usage(self.mc_dir).free
            if total_size > 0 and free < total_size * 1.5:
                log.warning(f"Low disk space: free {human_size(free)}, needed approx {human_size(total_size)}")
        except Exception:
            pass

        # Download mods
        self.log(f"Downloading {len(tasks)} files with {self.concurrency} concurrency...")
        self.progress("Installing mods", 0, len(tasks), "")

        def prog_cb(completed, total, cur_file):
            pct = int(completed/total*100) if total else 0
            self.progress("Installing mods", completed, total, cur_file)
            self.log(f"[{completed}/{total} {pct}%] {cur_file}")

        completed, total_tasks, failed = self.download_mgr.download_all(
            tasks,
            progress_cb=prog_cb,
            verify_hashes=self.verify_hashes
        )
        if failed:
            # Don't silently continue
            failed_msg = "\n".join(failed[:10])
            raise DownloadError(f"Failed to download {len(failed)}/{total_tasks} files:\n{failed_msg}\n\nPlease try again. Technical details in log.")

        self.log(f"All mods downloaded: {completed}/{total_tasks}")

        # Extract overrides into gameDir
        self.log("Installing configuration files (overrides/)...")
        self.progress("Installing configuration files", 0, 100, "overrides")
        if overrides_dir.exists():
            for item in overrides_dir.rglob("*"):
                if item.is_file():
                    rel = item.relative_to(overrides_dir)
                    dest = safe_join(game_dir, str(rel.as_posix()))
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    # Don't overwrite saves? Preserve user files unless clean update
                    # For now, overwrite but preserve if existing and same?
                    if dest.exists():
                        # Compare hash? simple: if same size and mtime close, skip?
                        # We'll overwrite configs but preserve saves if user has saves
                        if "saves" in rel.parts:
                            log.info(f"Preserving existing save: {rel}")
                            continue
                    shutil.copy2(item, dest)
            self.log(f"Overrides extracted to {game_dir}")

        # Also ensure mods directory exists even if no mods? Already
        # Create installation state
        pack = InstalledPack(
            slug=slug,
            name=index.name,
            project_id=proj.id,
            version_id=version.id,
            minecraft_version=mc_version,
            loader=loader_type,
            loader_version=loader_version or "",
            version_dir_id=version_id_for_launcher,
            install_dir=str(game_dir),
            installed_at=existing.installed_at if existing else iso_now(),
            updated_at=iso_now(),
            files_count=len(tasks),
            total_size=total_size
        )
        if not self.dry_run:
            save_installation_state(self.mc_dir, pack)

        # Launcher integration
        self.log("Creating Minecraft launcher profile...")
        self.progress("Creating Minecraft installation", 0, 100, index.name)
        # Use absolute gameDir string with native separators? Launcher expects system path
        # Use resolved path
        game_dir_str = str(game_dir.resolve())
        # On Windows, need backslashes? json handles both but we provide as python path string (with backslashes escaped by json)
        profile = LauncherProfile(
            name=index.name,
            gameDir=game_dir_str,
            lastVersionId=version_id_for_launcher,
            icon="Grass"
        )
        inject_launcher_profile(self.mc_dir, slug, profile, dry_run=self.dry_run)
        self.progress("Finalizing launcher profile", 100, 100, "Done")

        self.log(f"Installation complete: {index.name} ({mc_version} {loader_type} {loader_version})")
        self.log(f"GameDir: {game_dir_str}")
        self.log(f"Launcher profile: {profile.name} -> {version_id_for_launcher}")
        self.log("Open the official Minecraft Launcher and select the profile: '{}'".format(index.name))

        # Optionally keep or delete mrpack
        if not self.keep_mrpack:
            try:
                if mrpack_path.exists() and mrpack_path.parent == self.mc_dir / "modpacker" / "cache":
                    # keep? spec says option keep
                    pass
            except Exception:
                pass

        return pack

    def list_versions(self, url: str) -> List[ModrinthVersion]:
        slug, _, _ = parse_modrinth_url(url)
        proj = self.client.get_project(slug)
        versions = self.client.list_versions(proj.id)
        versions.sort(key=lambda v: v.date_published, reverse=True)
        return versions

# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

def build_cli_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="modrinth-downloader",
        description="Modrinth Downloader - Install Modrinth modpacks (.mrpack) into the official Minecraft Launcher",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""
        Examples:
          %(prog)s https://modrinth.com/modpack/fabulously-optimized
          %(prog)s https://modrinth.com/modpack/ovo --dry-run
          %(prog)s https://modrinth.com/modpack/adventure --minecraft-dir /custom/.minecraft
          %(prog)s --gui
          %(prog)s https://modrinth.com/modpack/foo --list-versions
          %(prog)s https://modrinth.com/modpack/foo --version-id abc123
          %(prog)s --check-deps
          %(prog)s --install-deps --pm vista --dry-run
          %(prog)s --install-deps --pm vista --yes

        Minecraft directory auto-detection:
          Windows: %%APPDATA%%/.minecraft
          macOS: ~/Library/Application Support/minecraft
          Linux: ~/.minecraft

        Launcher integration:
          Creates isolated gameDir per modpack: <mc>/profiles/<slug>/
          Injects profile into launcher_profiles.json safely (with backup).
          Loads as "custom" type with lastVersionId = loader version string.
        """)
    )
    p.add_argument("url", nargs="?", help="Modrinth modpack URL (https://modrinth.com/modpack/<name> or version URL or direct .mrpack URL)")
    p.add_argument("--minecraft-dir", dest="minecraft_dir", help="Custom .minecraft directory (default auto-detected)")
    p.add_argument("--game-dir", dest="game_dir", help="Custom gameDir for this modpack (default: <mc>/profiles/<slug>)")
    p.add_argument("--dry-run", action="store_true", help="Verify directory structures without modifying live launcher settings; shows preview")
    p.add_argument("--list-versions", action="store_true", help="List available versions for the modpack URL and exit")
    p.add_argument("--version-id", dest="version_id", help="Select specific version ID or version_number to install")
    p.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY, help=f"Download concurrency (default {DEFAULT_CONCURRENCY})")
    p.add_argument("--keep-mrpack", action="store_true", help="Keep downloaded .mrpack files")
    p.add_argument("--no-verify", action="store_true", help="Disable SHA-512 hash verification (not recommended)")
    p.add_argument("--gui", action="store_true", help="Launch graphical UI")
    p.add_argument("--verbose", action="store_true", help="Verbose logging")
    p.add_argument("--version", action="store_true", help="Show version and exit")
    p.add_argument("--check-deps", action="store_true", help="Check system dependencies (Java, Tkinter, launcher) and exit")
    p.add_argument("--install-deps", action="store_true", help="Install missing system dependencies and exit (or continue to modpack install if a URL is also given)")
    p.add_argument("--pm", default="auto", choices=["auto", "vista", "dnf", "apt", "pacman", "zypper", "apk", "flatpak"],
                   help="Package manager for --install-deps (default auto; vista preferred when available)")
    p.add_argument("--deps", default=None, help="Comma-separated subset of deps for --install-deps (choices: java,tkinter,launcher)")
    p.add_argument("--yes", action="store_true", help="Assume yes for package-manager prompts during --install-deps")
    return p

def cli_main(argv=None):
    parser = build_cli_parser()
    args = parser.parse_args(argv)
    if args.version:
        print(f"{APP_NAME} v{APP_VERSION}")
        return 0
    if args.verbose:
        log.setLevel(logging.DEBUG)
    # System dependency checks / installation (standalone; may precede a modpack install)
    if args.check_deps:
        print(f"=== {APP_NAME} v{APP_VERSION} — system dependencies ===")
        print(f"Package managers: {detect_package_managers() or 'none found'}")
        status = check_system_deps()
        missing = 0
        for name, info in status.items():
            mark = "✓" if info["installed"] else "✗"
            print(f"  {mark} {name}: {info['detail']}")
            if not info["installed"]:
                missing += 1
        if missing and not args.url and not args.install_deps:
            return 1
        if not args.url and not args.install_deps:
            return 0
    if args.install_deps:
        only = [d.strip().lower() for d in args.deps.split(",") if d.strip()] if args.deps else None
        print(f"=== {APP_NAME} v{APP_VERSION} — installing system dependencies ===")
        try:
            n = install_system_deps(pm=args.pm, only=only, dry_run=args.dry_run, yes=args.yes)
            print(f"  Done: {n} package(s) {'would be ' if args.dry_run else ''}installed")
        except ModpackerError as e:
            log.error(f"Dependency installation failed: {e}")
            return 1
        if not args.url:
            return 0
        print()
    # GUI mode if requested or no URL and display available
    if args.gui or (not args.url and not args.list_versions):
        # Check if tkinter available and display
        try:
            launch_gui(args)
            return 0
        except Exception as e:
            log.error(f"Failed to launch GUI: {e}")
            traceback.print_exc()
            if not args.url:
                parser.print_help()
                return 1
    if not args.url:
        parser.print_help()
        print("\nError: Modrinth URL required (or use --gui)")
        return 2

    # Validate URL early
    url = args.url.strip()
    # Handle dry-run with verbose preview
    installer = ModpackInstaller(
        minecraft_dir=args.minecraft_dir,
        concurrency=args.concurrency,
        keep_mrpack=args.keep_mrpack,
        verify_hashes=not args.no_verify,
        dry_run=args.dry_run,
    )
    print(f"=== {APP_NAME} v{APP_VERSION} ===")
    print(f"Minecraft directory: {installer.mc_dir}")
    if args.dry_run:
        print(">>> DRY RUN MODE: No files will be modified <<<")

    # List versions mode
    if args.list_versions:
        try:
            versions = installer.list_versions(url)
            print(f"\nAvailable versions for {url}:")
            for v in versions[:20]:
                feat = " [featured]" if v.featured else ""
                print(f"  {v.id} | {v.version_number} | {v.name} | {v.date_published[:10]} | {v.game_versions} {v.loaders}{feat}")
                if v.primary_file:
                    print(f"      file: {v.primary_file.get('filename')} size={human_size(v.primary_file.get('size',0))}")
            if len(versions) > 20:
                print(f"  ... and {len(versions)-20} more")
            return 0
        except Exception as e:
            log.error(f"Could not list versions: {e}")
            traceback.print_exc()
            return 1

    # Normal install
    try:
        pack = installer.install_from_url(url, selected_version_id=args.version_id, game_dir_custom=Path(args.game_dir) if args.game_dir else None)
        print("\n✓ Installation complete" if not args.dry_run else "\n✓ Dry-run complete (no changes made)")
        print(f"  Modpack: {pack.name}")
        print(f"  Minecraft: {pack.minecraft_version}")
        print(f"  Loader: {pack.loader} {pack.loader_version}")
        print(f"  GameDir: {pack.install_dir}")
        print(f"  VersionId: {pack.version_dir_id}")
        if not args.dry_run:
            print(f"  Launcher profile injected into {installer.mc_dir / 'launcher_profiles.json'}")
            print(f"\nNext steps:")
            print(f"  1. Open the official Minecraft Launcher")
            print(f"  2. Select installation: \"{pack.name}\" (or {pack.version_dir_id})")
            print(f"  3. Click Play")
            print(f"\n⚠  WARNING: Mods are executable code. Only install modpacks you trust.")
        else:
            print(f"  (dry-run: launcher_profiles.json not modified, files not downloaded)")
        return 0
    except ModpackerError as e:
        log.error(f"Installation failed: {e}")
        if args.verbose:
            traceback.print_exc()
        else:
            print("\nShow technical details with --verbose")
        return 1
    except KeyboardInterrupt:
        log.error("Cancelled by user")
        return 130
    except Exception as e:
        log.error(f"Unexpected error: {e}")
        traceback.print_exc()
        return 1

# ──────────────────────────────────────────────────────────────────────────────
# GUI (Tkinter) - Polished Desktop UI
# ──────────────────────────────────────────────────────────────────────────────

def launch_gui(cli_args=None):
    """Simplified GUI: URL + Install + progress. No settings clutter."""
    try:
        import tkinter as tk
        from tkinter import ttk, messagebox
    except ImportError as e:
        raise ModpackerError(f"Tkinter not available: {e}. Use CLI: python modpacker.py <url>")

    BG = "#111418"
    CARD = "#1e232b"
    ACCENT = "#1bd96a"
    ACCENT_HOVER = "#19c15f"
    TEXT = "#e6e8eb"
    MUTED = "#9aa0a6"
    BORDER = "#2a2f3a"

    root = tk.Tk()
    root.title(f"{APP_NAME}")
    root.geometry("640x460")
    root.minsize(580, 400)
    root.configure(bg=BG)

    style = ttk.Style()
    try:
        style.theme_use("clam")
    except Exception:
        pass
    style.configure("TProgressbar", troughcolor="#0f1115", background=ACCENT, bordercolor=BORDER, lightcolor=ACCENT, darkcolor=ACCENT)

    url_var = tk.StringVar(value=cli_args.url if cli_args and cli_args.url else "")
    status_var = tk.StringVar(value="Paste a Modrinth modpack URL to begin")
    detail_var = tk.StringVar(value="")

    # Header
    hdr = tk.Frame(root, bg=BG)
    hdr.pack(fill="x", padx=20, pady=(18, 8))
    tk.Label(hdr, text="Modrinth \u2192 Minecraft", font=("Segoe UI", 18, "bold"), bg=BG, fg=TEXT).pack(anchor="w")
    tk.Label(hdr, text="Install any Modrinth modpack into the official Launcher in one click", font=("Segoe UI", 9), bg=BG, fg=MUTED).pack(anchor="w")

    # URL card
    card = tk.Frame(root, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
    card.pack(fill="x", padx=20, pady=10, ipady=10)
    inner = tk.Frame(card, bg=CARD)
    inner.pack(fill="x", padx=14, pady=8)
    tk.Label(inner, text="Modpack URL", font=("Segoe UI", 8, "bold"), bg=CARD, fg=MUTED).pack(anchor="w", pady=(0,4))
    url_entry = tk.Entry(inner, textvariable=url_var, font=("Segoe UI", 10), bg="#0f1115", fg=TEXT, insertbackground=TEXT, relief="flat", highlightthickness=1, highlightbackground=BORDER, highlightcolor=ACCENT, bd=0)
    url_entry.pack(fill="x", ipady=7)
    url_entry.bind("<Return>", lambda e: on_install())
    tip = tk.Label(inner, text="Example: https://modrinth.com/modpack/fabulously-optimized", font=("Segoe UI", 7), bg=CARD, fg="#5f6368")
    tip.pack(anchor="w", pady=(6,0))

    # Install button (full width)
    install_btn = tk.Button(root, text="Install", font=("Segoe UI", 11, "bold"), bg=ACCENT, fg="#0a0a0a", activebackground=ACCENT_HOVER, relief="flat", cursor="hand2", pady=8)
    install_btn.pack(fill="x", padx=20, pady=(2,10))

    # Progress
    prog = ttk.Progressbar(root, mode="determinate", maximum=100)
    prog.pack(fill="x", padx=20, pady=(0,4))
    status_lbl = tk.Label(root, textvariable=status_var, font=("Segoe UI", 9, "bold"), bg=BG, fg=TEXT, wraplength=600, justify="left")
    status_lbl.pack(fill="x", padx=20)
    detail_lbl = tk.Label(root, textvariable=detail_var, font=("Segoe UI", 8), bg=BG, fg=MUTED, wraplength=600, justify="left")
    detail_lbl.pack(fill="x", padx=20, pady=(2,6))

    # Log (small, collapsed by default but visible for debugging)
    log = tk.Text(root, height=6, bg="#0f1115", fg="#9aa0a6", font=("Consolas", 7), relief="flat", highlightthickness=1, highlightbackground=BORDER, wrap="word", state="disabled")
    log.pack(fill="both", expand=True, padx=20, pady=(4,8))

    def log_msg(m):
        root.after(0, lambda: _log(m))
    def _log(m):
        log.configure(state="normal")
        log.insert("end", m + "\n")
        log.see("end")
        log.configure(state="disabled")

    _log(f"{APP_NAME} v{APP_VERSION} \u2022 {get_minecraft_dir(str(cli_args.minecraft_dir) if cli_args and cli_args.minecraft_dir else None)}")

    def set_progress(stage, cur, total, detail):
        def _upd():
            if total > 0:
                pct = cur/total*100 if total else 0
                prog.configure(value=pct)
                status_var.set(f"{stage}  {cur}/{total}")
                detail_var.set(detail[:120])
            else:
                status_var.set(stage)
                detail_var.set(detail[:120])
                # indeterminate pulse for unknown total
                if "Fetching" in stage or "Downloading modpack" in stage:
                    prog.configure(mode="indeterminate")
                    prog.start(12)
                else:
                    prog.stop()
                    prog.configure(mode="determinate", value=10)
        root.after(0, _upd)

    def on_install():
        url = url_var.get().strip()
        if not url:
            messagebox.showwarning("URL required", "Paste a Modrinth modpack URL first.")
            return
        try:
            if not MRPACK_URL_RE.match(url):
                parse_modrinth_url(url)
        except Exception as e:
            messagebox.showerror("Invalid URL", str(e))
            return
        install_btn.configure(state="disabled", text="Installing\u2026", bg="#2a2f3a", fg=MUTED)
        prog.configure(mode="determinate", value=0)
        status_var.set("Starting\u2026")
        detail_var.set(url)
        log.configure(state="normal"); log.delete("1.0","end"); log.configure(state="disabled")
        _log(f"Installing: {url}")

        def _run():
            try:
                warn_if_launcher_running()
                mc_dir = get_minecraft_dir(str(cli_args.minecraft_dir) if cli_args and cli_args.minecraft_dir else None)
                installer = ModpackInstaller(minecraft_dir=mc_dir, dry_run=False, progress_callback=set_progress, log_callback=log_msg)
                pack = installer.install_from_url(url)
                def _done():
                    prog.configure(mode="determinate", value=100)
                    status_var.set(f"\u2713 {pack.name} installed")
                    detail_var.set(f"{pack.minecraft_version} \u2022 {pack.loader} {pack.loader_version} \u2022 {pack.install_dir}")
                    _log(f"Done: {pack.name} -> {pack.install_dir} [{pack.version_dir_id}]")
                    install_btn.configure(state="normal", text="Open Minecraft Launcher", bg=ACCENT, fg="#0a0a0a", command=lambda: open_launcher(mc_dir))
                    messagebox.showinfo("Done", f"\u2713 Installed {pack.name}\n\nMinecraft {pack.minecraft_version}\nLoader {pack.loader} {pack.loader_version}\n\nOpen the official Minecraft Launcher and select \"{pack.name}\".")
                root.after(0, _done)
            except Exception as e:
                _log(f"Failed: {e}")
                traceback.print_exc()
                def _err():
                    status_var.set(f"\u2717 {e}")
                    detail_var.set(str(e)[:180])
                    prog.configure(value=0)
                    install_btn.configure(state="normal", text="Install", bg=ACCENT, fg="#0a0a0a", command=on_install)
                    messagebox.showerror("Failed", str(e))
                root.after(0, _err)
        threading.Thread(target=_run, daemon=True).start()

    install_btn.configure(command=on_install)

    def open_launcher(mc_dir: Path):
        try:
            import subprocess
            s = platform.system()
            if s == "Windows":
                exe = Path(os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)")) / "Minecraft Launcher" / "MinecraftLauncher.exe"
                if exe.exists():
                    subprocess.Popen([str(exe)], shell=True)
                else:
                    os.startfile(str(mc_dir))  # type: ignore
            elif s == "Darwin":
                subprocess.Popen(["open", "-a", "Minecraft"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                subprocess.Popen(["xdg-open", str(mc_dir)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            _log("Opening launcher\u2026")
        except Exception as e:
            messagebox.showerror("Open failed", str(e))

    root.after(200, lambda: url_entry.focus_set())
    footer = tk.Label(root, text="Mods are executable code \u2014 only install packs you trust.", font=("Segoe UI", 7), bg=BG, fg="#5f6368")
    footer.pack(side="bottom", pady=(0,8))
    root.mainloop()


# Entry point
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    sys.exit(cli_main())


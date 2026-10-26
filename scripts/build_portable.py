"""Build a Windows x64 portable ZIP without changing the active Python environment.

Each attempt uses a new staging directory; existing outputs are never removed.
SPDX-License-Identifier: GPL-3.0-or-later
"""

import argparse
from datetime import datetime, timezone
from importlib import metadata
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app_metadata import APP_NAME, VERSION, REPOSITORY_URL
from ffmpeg_service import discover_engine, inspect_engine
from scripts.setup_ffmpeg import sha256

PACKAGES = ("pillow", "pillow-heif", "resvg-py", "tkinterdnd2", "pyinstaller")


def git(*args):
    return subprocess.check_output(["git", "-C", str(ROOT), *args], text=True).strip()


def verify_engine(directory, manifest, archive):
    # A local receipt alone is not a trust anchor. Verify the upstream ZIP and
    # compare both executable hashes with its exact, pinned members.
    if not archive.is_file() or sha256(archive) != manifest["archive_sha256"]:
        raise ValueError("The pinned FFmpeg ZIP is missing or has an incorrect SHA-256")
    import hashlib
    with zipfile.ZipFile(archive) as bundle:
        for name in ("ffmpeg.exe", "ffprobe.exe"):
            with bundle.open(f"{manifest['archive_root']}/bin/{name}") as stream:
                digest = hashlib.sha256()
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            if sha256(directory / name) != digest.hexdigest():
                raise ValueError(f"Installed {name} differs from the verified archive")
    capabilities = inspect_engine(discover_engine(directory))
    if capabilities.version != manifest["version"] + "-essentials_build-www.gyan.dev":
        raise ValueError("Unexpected FFmpeg build version")
    if (set(manifest["required_encoders"]) - capabilities.encoders or
            set(manifest["required_muxers"]) - capabilities.muxers):
        raise ValueError("The media engine lacks required codecs or muxers")
    return capabilities


def collect_licenses(destination):
    destination.mkdir()
    inventory = []
    for name in PACKAGES:
        distribution = metadata.distribution(name)
        target = destination / name
        target.mkdir()
        found = []
        for member in distribution.files or []:
            if ".dist-info/" in str(member).replace("\\", "/") and any(
                    token in str(member).lower() for token in ("license", "copying", "notice")):
                source = Path(distribution.locate_file(member))
                if source.is_file():
                    shutil.copy2(source, target / source.name)
                    found.append(source.name)
        if not found:
            raise ValueError(f"No license text was found for {name}")
        inventory.append({"name": name, "version": distribution.version, "license_files": found})
    prefix = Path(sys.base_prefix)
    candidates = [prefix / "LICENSE_PYTHON.txt", prefix / "LICENSE.txt"]
    python_license = next((path for path in candidates if path.is_file()), None)
    if python_license is None:
        raise ValueError("Python license text is missing")
    shutil.copy2(python_license, destination / "Python.txt")
    # Tcl/Tk licenses in the installed runtime, including both Conda and python.org layouts.
    runtime_licenses = list(prefix.glob("Library/lib/*8.6/license.terms")) + list(prefix.glob("tcl/*/license.terms"))
    for path in runtime_licenses:
        shutil.copy2(path, destination / f"{path.parent.name}.txt")
    if not runtime_licenses:
        raise ValueError("Tcl/Tk license text is missing from this Python distribution")
    dnd = Path(metadata.distribution("tkinterdnd2").locate_file("tkinterdnd2/tkdnd/win-x64/tkdnd.tcl"))
    text = dnd.read_text(encoding="utf-8").split("package require Tk", 1)[0]
    (destination / "tkdnd.txt").write_text(text, encoding="utf-8")
    return inventory


def package_files(directory):
    return {path.relative_to(directory).as_posix(): {"sha256": sha256(path), "size": path.stat().st_size}
            for path in sorted(directory.rglob("*")) if path.is_file() and path.name != "package-manifest.json"}


def build(output_dir, archive=None):
    if os.name != "nt" or platform.machine().lower() not in ("amd64", "x86_64") or sys.maxsize < 2**32:
        raise RuntimeError("Build with 64-bit Python on Windows x64")
    manifest = json.loads((ROOT / "tools/ffmpeg-manifest.json").read_text(encoding="utf-8"))
    archive = archive or ROOT / ".cache" / Path(manifest["archive_url"]).name
    engine = ROOT / "tools/ffmpeg"
    capabilities = verify_engine(engine, manifest, archive)
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{APP_NAME}-{VERSION}-windows-x64"
    archive_output = output_dir / (stem + ".zip")
    if archive_output.exists():
        raise FileExistsError(f"Choose a new output directory; preserving {archive_output}")
    if git("ls-files", "--others", "--exclude-standard"):
        raise ValueError("Stage new source files before packaging so the source snapshot is complete")
    staging_parent = ROOT / "build/portable"
    staging_parent.mkdir(parents=True, exist_ok=True)
    # Keep staging and logs for diagnosis. No recursive cleanup of user paths.
    staging = Path(tempfile.mkdtemp(prefix="build-", dir=staging_parent))
    log = staging / "pyinstaller.log"
    print(f"Building in {staging}", flush=True)
    with log.open("w", encoding="utf-8") as stream:
        result = subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
                                 "--distpath", str(staging / "dist"), "--workpath", str(staging / "work"),
                                 str(ROOT / "ImadeoCovert.spec")], cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT)
    if result.returncode:
        raise RuntimeError(f"PyInstaller failed; see {log}")
    package = staging / "dist/ImadeoCovert"
    target_engine = package / "tools/ffmpeg"
    target_engine.mkdir(parents=True)
    for name in ("ffmpeg.exe", "ffprobe.exe", "LICENSE", "README.txt"):
        shutil.copy2(engine / name, target_engine / name)
    shutil.copy2(ROOT / "tools/ffmpeg-manifest.json", package / "tools/ffmpeg-manifest.json")
    provenance = {"manifest": manifest, "version": capabilities.version, "configuration": capabilities.configuration,
                  "binary_sha256": {name: sha256(target_engine / name) for name in ("ffmpeg.exe", "ffprobe.exe")}}
    (target_engine / "installation.json").write_text(json.dumps(provenance, indent=2), encoding="utf-8")
    inventory = collect_licenses(package / "licenses")
    for source, target in (("LICENSE", "LICENSE"), ("THIRD_PARTY_NOTICES.md", "THIRD_PARTY_NOTICES.md"),
                           ("docs/portable-readme.zh-CN.md", "使用说明.txt"), ("tools/release-policy.json", "release-policy.json")):
        shutil.copy2(ROOT / source, package / target)
    # Include the exact application source snapshot; only Git-tracked source paths.
    source_zip = package / "source/ImadeoCovert-source.zip"
    source_zip.parent.mkdir()
    with zipfile.ZipFile(source_zip, "x", compression=zipfile.ZIP_DEFLATED) as bundle:
        for name in git("ls-files", "-z").split("\0"):
            path = ROOT / name
            if name and path.is_file():
                bundle.write(path, name)
    policy = json.loads((ROOT / "tools/release-policy.json").read_text(encoding="utf-8"))
    build_info = {"schema_version": 1, "name": APP_NAME, "version": VERSION, "platform": "windows-x64",
                  "source_repository": REPOSITORY_URL, "source_commit": git("rev-parse", "HEAD"),
                  "source_dirty": bool(git("status", "--porcelain")), "python": sys.version,
                  "built_at_utc": datetime.now(timezone.utc).isoformat(), "dependencies": inventory,
                  "engine": provenance, "release_review_status": policy["status"]}
    (package / "build-info.json").write_text(json.dumps(build_info, indent=2), encoding="utf-8")
    (package / "package-manifest.json").write_text(json.dumps(package_files(package), indent=2), encoding="utf-8")
    temporary_zip = staging / archive_output.name
    with zipfile.ZipFile(temporary_zip, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
        for path in sorted(package.rglob("*")):
            if path.is_file():
                bundle.write(path, str(Path(stem) / path.relative_to(package)))
    # Copy on the destination volume first, then publish atomically. Windows
    # rename refuses an existing target, including a competing build's output.
    with tempfile.NamedTemporaryFile(dir=output_dir, suffix=".partial", delete=False) as target:
        partial = Path(target.name)
        try:
            with temporary_zip.open("rb") as source:
                shutil.copyfileobj(source, target)
            target.flush()
            os.fsync(target.fileno())
        except BaseException:
            target.close()
            partial.unlink(missing_ok=True)
            raise
    try:
        partial.rename(archive_output)
    finally:
        partial.unlink(missing_ok=True)
    checksum = sha256(archive_output)
    (output_dir / (archive_output.name + ".sha256")).write_text(f"{checksum}  {archive_output.name}\n", encoding="ascii")
    (output_dir / "build-summary.json").write_text(json.dumps({"archive": str(archive_output), "sha256": checksum,
        "size": archive_output.stat().st_size, "staging": str(staging), "build_info": build_info}, indent=2), encoding="utf-8")
    print(json.dumps({"archive": str(archive_output), "sha256": checksum, "size": archive_output.stat().st_size}), flush=True)
    return archive_output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    parser.add_argument("--engine-archive", type=Path)
    args = parser.parse_args()
    build(args.output_dir, args.engine_archive)


if __name__ == "__main__":
    main()

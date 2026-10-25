"""Install the pinned Windows toolchain locally; never changes PATH or the registry."""

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import sys
import tempfile
import time
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ffmpeg_service import discover_engine, inspect_engine  # noqa: E402


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def install(archive: Path, manifest: dict, destination: Path) -> dict:
    if sha256(archive) != manifest["archive_sha256"]:
        raise ValueError("FFmpeg archive SHA-256 does not match the pinned manifest")
    if destination.exists():
        raise FileExistsError(f"Refusing to replace an existing engine: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".ffmpeg-install-", dir=destination.parent) as temporary:
        staging = Path(temporary)
        with zipfile.ZipFile(archive) as bundle:
            for member in bundle.infolist():
                parts = PurePosixPath(member.filename).parts
                if not parts or parts[0] != manifest["archive_root"] or ".." in parts or "\\" in member.filename:
                    raise ValueError("Unexpected path in FFmpeg archive")
                if member.is_dir():
                    continue
                relative = parts[1:]
                if relative[:1] == ("bin",):
                    if relative not in (("bin", "ffmpeg.exe"), ("bin", "ffprobe.exe")):
                        continue
                    relative = relative[1:]
                target = staging.joinpath(*relative)
                if not target.resolve().is_relative_to(staging.resolve()):
                    raise ValueError("Archive path escapes the installation directory")
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(member) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
        capabilities = inspect_engine(discover_engine(staging), timeout=20)
        if capabilities.version != manifest["version"] + "-essentials_build-www.gyan.dev":
            raise ValueError(f"Unexpected engine version: {capabilities.version}")
        missing = (set(manifest["required_encoders"]) - capabilities.encoders
                   | set(manifest["required_muxers"]) - capabilities.muxers)
        if missing:
            raise ValueError(f"Pinned build is missing required capabilities: {sorted(missing)}")
        receipt = {
            "manifest": manifest,
            "version": capabilities.version,
            "configuration": capabilities.configuration,
            "binary_sha256": {name: sha256(staging / name) for name in ("ffmpeg.exe", "ffprobe.exe")},
        }
        (staging / "installation.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
        # Rename the staged directory only after verification, leaving no partial engine.
        installed = staging / "ready"
        installed.mkdir()
        for item in list(staging.iterdir()):
            if item != installed:
                item.rename(installed / item.name)
        # Windows scanners can briefly hold a newly executed binary open.
        for attempt in range(6):
            try:
                installed.rename(destination)
                break
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.2 * (attempt + 1))
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, help="Use an already downloaded ZIP")
    args = parser.parse_args()
    manifest = json.loads((ROOT / "tools" / "ffmpeg-manifest.json").read_text(encoding="utf-8"))
    destination = ROOT / "tools" / "ffmpeg"
    if destination.exists():
        print(f"Engine directory already exists: {destination}; inspect it with scripts/probe_media.py.")
        return 2
    archive = args.archive or ROOT / ".cache" / Path(manifest["archive_url"]).name
    if not archive.exists() and args.archive is None:
        archive.parent.mkdir(parents=True, exist_ok=True)
        # Use a temporary download, so interrupted downloads never become cached ZIPs.
        with tempfile.NamedTemporaryFile(dir=archive.parent, suffix=".download", delete=False) as output:
            download = Path(output.name)
            try:
                with urllib.request.urlopen(manifest["archive_url"], timeout=30) as source:
                    shutil.copyfileobj(source, output)
            except BaseException:
                output.close()
                download.unlink(missing_ok=True)
                raise
        try:
            if sha256(download) != manifest["archive_sha256"]:
                raise ValueError("Downloaded archive failed SHA-256 verification")
            download.replace(archive)
        finally:
            download.unlink(missing_ok=True)
    receipt = install(archive, manifest, destination)
    print(f"Installed {receipt['version']} in {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

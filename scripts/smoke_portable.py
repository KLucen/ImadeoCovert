"""Verify and run an extracted portable ZIP with no Python/FFmpeg on PATH.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import argparse
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.setup_ffmpeg import sha256


def extract_verified(archive, destination):
    checksum_file = archive.with_name(archive.name + ".sha256")
    if not checksum_file.is_file() or sha256(archive) != checksum_file.read_text().split()[0]:
        raise ValueError("Portable archive checksum failed")
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            # ZipInfo normalizes backslashes on Windows; inspect the raw name too.
            raw_name = member.orig_filename
            path = PurePosixPath(raw_name)
            if path.is_absolute() or ".." in path.parts or "\\" in raw_name or ":" in raw_name or "\0" in raw_name:
                raise ValueError("Unsafe portable archive path")
        bundle.extractall(destination)
    roots = list(destination.iterdir())
    if len(roots) != 1 or not roots[0].is_dir():
        raise ValueError("Expected one portable root directory")
    package = roots[0]
    manifest = json.loads((package / "package-manifest.json").read_text(encoding="utf-8"))
    actual = {path.relative_to(package).as_posix() for path in package.rglob("*") if path.is_file()}
    if actual != set(manifest) | {"package-manifest.json"}:
        raise ValueError("Portable file inventory differs from the manifest")
    for name, entry in manifest.items():
        path = package / name
        if path.stat().st_size != entry["size"] or sha256(path) != entry["sha256"]:
            raise ValueError(f"Portable file checksum failed: {name}")
    return package


def smoke(archive, report_path):
    report_path.parent.mkdir(parents=True, exist_ok=True)
    parent = ROOT / "test-results/portable"
    parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="离线 portable (test)-", dir=parent))
    extracted = work / "app"
    extracted.mkdir()
    package = extract_verified(archive.resolve(), extracted)
    cwd = work / "unrelated-working-directory"
    cwd.mkdir()
    output = work / "self-test"
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith(("CONDA", "PYTHON"))
           and key.upper() not in {"TCL_LIBRARY", "TK_LIBRARY"}}
    windows = Path(env.get("SystemRoot", r"C:\Windows"))
    env["PATH"] = os.pathsep.join(map(str, (windows / "System32", windows)))
    process = subprocess.Popen([str(package / "ImadeoCovert.exe"), "--self-test", str(output)], cwd=cwd, env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    try:
        stdout, stderr = process.communicate(timeout=150)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            subprocess.run([str(windows / "System32/taskkill.exe"), "/PID", str(process.pid), "/T", "/F"], capture_output=True)
        else:
            process.kill()
        process.communicate()
        raise TimeoutError(f"Portable smoke test timed out; files retained in {work}")
    generated = output / "report.json"
    if not generated.exists():
        raise RuntimeError(f"No self-test report; exit={process.returncode}; stderr={stderr.decode(errors='replace')}; {work}")
    report = json.loads(generated.read_text(encoding="utf-8"))
    report.update({"archive_sha256": sha256(archive), "package": str(package), "restricted_path": env["PATH"],
                   "returncode": process.returncode})
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if process.returncode != 0 or not report["success"] or not report["frozen"]:
        raise RuntimeError(f"Portable self-test failed: {report_path}\n{report.get('error', '')}")
    print(f"Portable smoke passed ({len(report['checks'])} checks): {report_path}")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--report", type=Path, default=ROOT / "test-results/portable-smoke.json")
    args = parser.parse_args()
    smoke(args.archive, args.report)


if __name__ == "__main__":
    main()

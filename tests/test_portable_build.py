import hashlib
import json
from pathlib import Path
import zipfile

import pytest

from scripts.build_portable import verify_engine
from scripts.check_release import validate
from scripts.smoke_portable import extract_verified
from scripts.create_sandbox_check import create


def package(tmp_path, members):
    archive = tmp_path / "portable.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        for name, content in members.items():
            member = zipfile.ZipInfo()
            member.filename = name  # Preserve a malicious raw name on Windows too.
            bundle.writestr(member, content)
    archive.with_name(archive.name + ".sha256").write_text(hashlib.sha256(archive.read_bytes()).hexdigest())
    return archive


def test_package_verification_checks_inventory_and_file_content(tmp_path):
    data = b"executable bytes"
    manifest = {"app.exe": {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}}
    archive = package(tmp_path, {"App/app.exe": data, "App/package-manifest.json": json.dumps(manifest)})
    destination = tmp_path / "extracted"
    destination.mkdir()
    assert extract_verified(archive, destination).name == "App"


@pytest.mark.parametrize("member", ["../escape.txt", "/absolute.txt", "C:/escape.txt", "App/../escape.txt", "App\\escape.txt"])
def test_package_extraction_rejects_unsafe_names(tmp_path, member):
    archive = package(tmp_path, {member: "unsafe"})
    with pytest.raises(ValueError, match="Unsafe"):
        extract_verified(archive, tmp_path / "out")
    assert not (tmp_path / "escape.txt").exists()


def test_package_rejects_tampered_file_even_with_valid_zip_checksum(tmp_path):
    manifest = {"app.exe": {"sha256": "0" * 64, "size": 3}}
    archive = package(tmp_path, {"App/app.exe": b"bad", "App/package-manifest.json": json.dumps(manifest)})
    with pytest.raises(ValueError, match="file checksum"):
        extract_verified(archive, tmp_path / "out")


def test_engine_verification_uses_upstream_archive_not_receipt(tmp_path):
    directory = tmp_path / "engine"
    directory.mkdir()
    (directory / "ffmpeg.exe").write_bytes(b"tampered")
    archive = package(tmp_path, {"engine/bin/ffmpeg.exe": b"trusted", "engine/bin/ffprobe.exe": b"probe"})
    manifest = {"archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(), "archive_root": "engine"}
    with pytest.raises(ValueError, match="differs"):
        verify_engine(directory, manifest, archive)


@pytest.mark.parametrize("tag,version", [("v1.18.0-dev", "1.18.0-dev"), ("v1.19.0", "1.18.0")])
def test_release_rejects_development_or_mismatched_versions(tmp_path, tag, version):
    with pytest.raises(ValueError, match="tag"):
        validate(tag, version, {}, tmp_path)


def test_release_requires_review_and_matching_source_assets(tmp_path):
    policy = {"schema_version": 1, "status": "pending"}
    with pytest.raises(ValueError, match="review"):
        validate("v1.18.0", "1.18.0", policy, tmp_path)
    policy.update(status="reviewed", review_notes="Verified", required_components=["ffmpeg"], source_archives=[])
    with pytest.raises(ValueError, match="No corresponding"):
        validate("v1.18.0", "1.18.0", policy, tmp_path)
    source = tmp_path / "sources.zip"
    source.write_bytes(b"source archive fixture")
    policy["source_archives"] = [{"file": source.name, "sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "components": ["ffmpeg"]}]
    validate("v1.18.0", "1.18.0", policy, tmp_path)
    source.write_bytes(b"changed")
    with pytest.raises(ValueError, match="checksum"):
        validate("v1.18.0", "1.18.0", policy, tmp_path)


def test_offline_sandbox_maps_app_readonly_and_keeps_results_separate(tmp_path):
    import xml.etree.ElementTree as ET
    app = tmp_path / "中文 & app"
    app.mkdir()
    (app / "ImadeoCovert.exe").touch()
    output = tmp_path / "sandbox"
    path = create(app, output)
    config = ET.parse(path).getroot()
    assert config.findtext("Networking") == "Disable"
    folders = config.findall("MappedFolders/MappedFolder")
    assert folders[0].findtext("HostFolder") == str(app.resolve())
    assert folders[0].findtext("ReadOnly") == "true"
    assert folders[1].findtext("ReadOnly") == "false"
    with pytest.raises(FileExistsError):
        create(app, output)

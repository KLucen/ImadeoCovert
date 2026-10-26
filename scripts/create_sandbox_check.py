"""Create an offline Windows Sandbox check for an already-extracted portable app."""

import argparse
from pathlib import Path
import xml.etree.ElementTree as ET


def create(package, output):
    package = package.resolve()
    if not (package / "ImadeoCovert.exe").is_file():
        raise ValueError("Select the extracted directory containing ImadeoCovert.exe")
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    results = output / "results"
    results.mkdir()
    config = ET.Element("Configuration")
    ET.SubElement(config, "Networking").text = "Disable"
    ET.SubElement(config, "MemoryInMB").text = "4096"
    folders = ET.SubElement(config, "MappedFolders")
    for host, guest, readonly in ((package, r"C:\ImadeoCovert", True), (results, r"C:\TestResults", False)):
        entry = ET.SubElement(folders, "MappedFolder")
        ET.SubElement(entry, "HostFolder").text = str(host)
        ET.SubElement(entry, "SandboxFolder").text = guest
        ET.SubElement(entry, "ReadOnly").text = str(readonly).lower()
    command = ET.SubElement(config, "LogonCommand")
    ET.SubElement(command, "Command").text = r'C:\ImadeoCovert\ImadeoCovert.exe --self-test C:\TestResults\self-test'
    ET.indent(config, space="  ")
    path = output / "ImadeoCovert-offline.wsb"
    ET.ElementTree(config).write(path, encoding="utf-8", xml_declaration=True)
    return path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path)
    parser.add_argument("output", type=Path, help="A new directory for configuration and reports")
    args = parser.parse_args()
    print(create(args.package, args.output))

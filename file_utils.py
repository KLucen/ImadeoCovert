"""Video output reservations and atomic publication. SPDX-License-Identifier: GPL-3.0-or-later"""

import os
from pathlib import Path
import tempfile

from ffmpeg_service import FFmpegError


def path_key(path: Path) -> str:
    return os.path.normcase(str(path.expanduser().resolve()))


def protect_sources(destination: Path, sources: tuple[Path, ...]) -> None:
    for source in sources:
        identical = path_key(destination) == path_key(source)
        if not identical and destination.exists() and source.exists():
            identical = os.path.samefile(destination, source)
        if identical:
            raise FFmpegError("source_conflict", "An output must not replace any source file.", str(destination))


def allocate_output(directory: Path, stem: str, extension: str, *,
                    reserved: set[str], sources: tuple[Path, ...], overwrite: bool) -> Path:
    """Reserve names in the snapshot order, avoiding all inputs and batch names."""
    directory = directory.expanduser().resolve()
    number = 0
    while True:
        candidate = directory / f"{stem}{'_' + str(number) if number else ''}{extension}"
        conflict = path_key(candidate) in reserved or (not overwrite and os.path.lexists(candidate))
        try:
            protect_sources(candidate, sources)
        except FFmpegError:
            conflict = True
        if not conflict:
            reserved.add(path_key(candidate))
            return candidate
        number += 1


class OutputTransaction:
    """Only publish a verified temp file; never pre-delete an existing output."""

    def __init__(self, destination: Path, sources: tuple[Path, ...], overwrite: bool):
        # Resolve the parent, not the leaf: a symlink destination is never followed.
        destination = destination.expanduser().absolute()
        self.destination = destination.parent.resolve() / destination.name
        self.sources = sources
        self.overwrite = overwrite
        self.temporary: Path | None = None

    def _validate_destination(self) -> None:
        protect_sources(self.destination, self.sources)
        if self.destination.is_symlink() or self.destination.is_dir():
            raise FFmpegError("output_conflict", "The output is a directory or symbolic link.", str(self.destination))
        if not self.overwrite and os.path.lexists(self.destination):
            raise FFmpegError("output_exists", "The output already exists.", str(self.destination))

    def __enter__(self):
        self._validate_destination()
        self.destination.parent.mkdir(parents=True, exist_ok=True)
        handle, name = tempfile.mkstemp(prefix=".imadeo-", suffix=".part" + self.destination.suffix,
                                        dir=self.destination.parent)
        os.close(handle)
        self.temporary = Path(name)
        return self

    def commit(self) -> Path:
        self._validate_destination()
        if self.temporary is None:
            raise RuntimeError("Transaction has not been started")
        with self.temporary.open("r+b") as output:
            os.fsync(output.fileno())
        try:
            if self.overwrite:
                os.replace(self.temporary, self.destination)
            elif os.name == "nt":
                # Windows rename is exclusive: fail if another writer created the target.
                os.rename(self.temporary, self.destination)
            else:
                # POSIX rename would overwrite. A hard link publishes exclusively instead.
                os.link(self.temporary, self.destination)
        except FileExistsError as exc:
            raise FFmpegError("output_exists", "Another writer created the output before completion.") from exc
        return self.destination

    def __exit__(self, *args):
        if self.temporary is not None:
            self.temporary.unlink(missing_ok=True)

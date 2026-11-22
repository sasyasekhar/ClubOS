import os
import re
import stat
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager, suppress
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

from app.services.storage.base import ObjectMissing, StorageError

_KEY_COMPONENT = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}")
_STATIC_ROOT = Path("app/static")


def _key_parts(key: str) -> list[str]:
    parts = key.split("/")
    if len(key) > 500 or not all(_KEY_COMPONENT.fullmatch(part) for part in parts):
        raise StorageError("Invalid attachment object key.")
    return parts


def _is_private(info: os.stat_result) -> bool:
    return info.st_uid == os.geteuid() and not info.st_mode & 0o077


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


class LocalAttachmentStorage:
    """Private, development-only storage for a POSIX filesystem."""

    def __init__(self, root: Path) -> None:
        self._root = root.expanduser().absolute()
        self._static_root = _STATIC_ROOT.resolve()
        if (
            ".." in self._root.parts
            or self._root.is_relative_to(self._static_root)
            or self._static_root.is_relative_to(self._root)
        ):
            raise StorageError("Attachment storage requires a private directory.")

    @contextmanager
    def _directory(self, parts: list[str], *, create: bool = False) -> Iterator[int]:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        root_depth = len(self._root.parts) - 1
        static_id = None
        static_ancestor_ids: set[tuple[int, int]] = set()
        with suppress(FileNotFoundError):
            static_id = _identity(self._static_root.stat())
        if static_id is not None:
            static_ancestor_ids = {_identity(path.stat()) for path in self._static_root.parents}

        # Directory descriptors keep each lookup anchored to its verified parent.
        with ExitStack() as stack:
            directory = os.open(self._root.anchor, flags)
            stack.callback(os.close, directory)
            for index, part in enumerate((*self._root.parts[1:], *parts), start=1):
                try:
                    child = os.open(part, flags, dir_fd=directory)
                except FileNotFoundError:
                    if not create:
                        raise
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=directory)
                    except FileExistsError:
                        pass  # A competing writer may have created this directory.
                    else:
                        os.fsync(directory)
                    child = os.open(part, flags, dir_fd=directory)
                stack.callback(os.close, child)
                directory = child
                info = os.fstat(child)
                identity = _identity(info)
                # Compare directory identities before writing anywhere below an alias.
                if identity == static_id or (index == root_depth and identity in static_ancestor_ids):
                    raise StorageError("Attachment storage requires a private directory.")
                if index >= root_depth and not _is_private(info):
                    raise StorageError("Attachment storage requires a private directory.")
            yield directory

    def put(self, key: str, content: bytes, content_type: str) -> None:
        *parts, name = _key_parts(key)
        temporary = f".upload-{uuid4().hex}.tmp"
        try:
            with self._directory(parts, create=True) as directory:
                fd = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=directory,
                )
                try:
                    try:
                        with os.fdopen(fd, "wb", closefd=False) as stream:
                            stream.write(content)
                            stream.flush()
                            os.fsync(fd)
                    finally:
                        os.close(fd)
                    os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
                except OSError:
                    os.unlink(temporary, dir_fd=directory)
                    raise
                os.fsync(directory)
        except OSError as exc:
            raise StorageError("Could not store attachment object.") from exc

    @contextmanager
    def get(self, key: str) -> Iterator[BinaryIO]:
        *parts, name = _key_parts(key)
        try:
            with self._directory(parts) as directory:
                fd = os.open(
                    name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
                )
                try:
                    info = os.fstat(fd)
                    if (
                        not stat.S_ISREG(info.st_mode)
                        or info.st_nlink != 1
                        or not _is_private(info)
                    ):
                        raise StorageError("Invalid attachment object.")
                    with os.fdopen(fd, "rb", closefd=False) as stream:
                        yield stream
                finally:
                    os.close(fd)
        except FileNotFoundError as exc:
            raise ObjectMissing("Attachment object is missing.") from exc
        except OSError as exc:
            raise StorageError("Could not read attachment object.") from exc

    def delete(self, key: str) -> None:
        *parts, name = _key_parts(key)
        try:
            with self._directory(parts) as directory:
                with suppress(FileNotFoundError):
                    os.unlink(name, dir_fd=directory)
                os.fsync(directory)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise StorageError("Could not delete attachment object.") from exc

    def iter_keys(self, prefix: str) -> Iterator[str]:
        parts = _key_parts(prefix)
        try:
            with self._directory(parts) as directory:
                yield from self._list_directory(directory, prefix)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise StorageError("Could not list attachment objects.") from exc

    def _list_directory(self, directory: int, prefix: str) -> Iterator[str]:
        with os.scandir(directory) as entries:
            for entry in entries:
                if not _KEY_COMPONENT.fullmatch(entry.name):
                    continue
                key = f"{prefix}/{entry.name}"
                if len(key) > 500:
                    continue
                if entry.is_dir(follow_symlinks=False):
                    try:
                        child = os.open(
                            entry.name,
                            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=directory,
                        )
                    except FileNotFoundError:
                        continue
                    try:
                        if _is_private(os.fstat(child)):
                            yield from self._list_directory(child, key)
                    finally:
                        os.close(child)
                elif entry.is_file(follow_symlinks=False):
                    yield key

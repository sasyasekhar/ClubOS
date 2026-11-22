from collections.abc import Iterable
from contextlib import AbstractContextManager
from typing import BinaryIO, Protocol


class StorageError(Exception):
    """A private attachment storage operation failed."""


class ObjectMissing(StorageError):
    """The requested private object does not exist."""


class AttachmentStorage(Protocol):
    def put(self, key: str, content: bytes, content_type: str) -> None:
        """Store a complete object at a private key."""
        ...

    def get(self, key: str) -> AbstractContextManager[BinaryIO]:
        """Open a bounded-read capable stream and close it on context exit."""
        ...

    def delete(self, key: str) -> None:
        """Delete an object, succeeding if it is already absent."""
        ...


class AttachmentInventory(AttachmentStorage, Protocol):
    def iter_keys(self, prefix: str) -> Iterable[str]:
        """List existing object keys beneath a namespace."""
        ...

from io import BytesIO
from threading import Lock

from app.services.storage.base import ObjectMissing


class MemoryAttachmentStorage:
    """Thread-safe attachment storage fake for use in tests."""

    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}
        self._lock = Lock()

    def put(self, key: str, content: bytes, content_type: str) -> None:
        with self._lock:
            self._objects[key] = content

    def put_if_absent(self, key: str, content: bytes, content_type: str) -> bool:
        with self._lock:
            if key in self._objects:
                return False
            self._objects[key] = content
            return True

    def get(self, key: str) -> BytesIO:
        with self._lock:
            try:
                return BytesIO(self._objects[key])
            except KeyError as exc:
                raise ObjectMissing("Attachment object is missing.") from exc

    def delete(self, key: str) -> None:
        with self._lock:
            self._objects.pop(key, None)

    def iter_keys(self, prefix: str, *, after: str | None = None) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(
                key for key in self._objects if key.startswith(prefix + "/") and (after is None or key > after)
            ))

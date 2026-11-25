import socket
import struct
from enum import Enum
from pathlib import Path
from time import monotonic
from typing import Protocol

from app.config.settings import settings


class ScanVerdict(Enum):
    CLEAN = "CLEAN"
    UNSAFE = "UNSAFE"


class ScannerUnavailable(Exception):
    pass


class AttachmentScanner(Protocol):
    def scan(self, content: bytes) -> ScanVerdict: ...


class ClamAVScanner:
    def __init__(self, socket_path: Path, timeout: float = 5.0):
        self.socket_path = socket_path
        self.timeout = timeout

    def scan(self, content: bytes) -> ScanVerdict:
        deadline = monotonic() + self.timeout

        def set_deadline(connection: socket.socket) -> None:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise ScannerUnavailable("Attachment scan timed out.")
            connection.settimeout(remaining)

        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                set_deadline(connection)
                connection.connect(str(self.socket_path))
                set_deadline(connection)
                connection.sendall(b"zINSTREAM\0")
                chunk_size = 65536
                for offset in range(0, len(content), chunk_size):
                    chunk = content[offset:offset + chunk_size]
                    set_deadline(connection)
                    connection.sendall(struct.pack("!I", len(chunk)) + chunk)
                set_deadline(connection)
                connection.sendall(b"\0" * 4)
                response = bytearray()
                while len(response) < 4096:
                    set_deadline(connection)
                    chunk = connection.recv(4096 - len(response))
                    if not chunk:
                        break
                    response.extend(chunk)
                    if b"\0" in response:
                        break
        except OSError as exc:
            raise ScannerUnavailable("Attachment scanner is unavailable.") from exc

        message, terminator, trailing = bytes(response).partition(b"\0")
        if terminator and not trailing:
            if message == b"stream: OK":
                return ScanVerdict.CLEAN
            if message.startswith(b"stream: ") and message.endswith(b" FOUND"):
                return ScanVerdict.UNSAFE
        raise ScannerUnavailable("Attachment scanner did not return a definitive result.")


def get_attachment_scanner() -> AttachmentScanner | None:
    socket_path = settings.ATTACHMENT_SCAN_SOCKET
    return ClamAVScanner(socket_path) if socket_path is not None and socket_path.is_absolute() else None

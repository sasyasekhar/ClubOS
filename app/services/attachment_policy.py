import re
from dataclasses import dataclass
from pathlib import PurePath
from struct import unpack_from
from unicodedata import category
from uuid import UUID
from zlib import crc32

from sqlalchemy.orm import Session

from app.models.account import Account
from app.models.activity_revision import ActivityRevision
from app.models.enums import ActivityRevisionStatus
from app.services.academic_year_write_guard import require_completion_window_open
from app.services.activity_drafts import ActivityError
from app.services.activity_workflow import authorized_revision


MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MiB

ALLOWED_TYPES = {
    ".pdf": ("application/pdf", b"%PDF-"),
    ".png": ("image/png", b"\x89PNG\r\n\x1a\n"),
    ".jpg": ("image/jpeg", b"\xff\xd8\xff"),
    ".jpeg": ("image/jpeg", b"\xff\xd8\xff"),
}

_RESERVED_FILENAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"{prefix}{digit}" for prefix in ("COM", "LPT") for digit in "123456789¹²³"),
}
_PDF_HEADER = re.compile(rb"%PDF-(?:1\.[0-7]|2\.0)[\r\n]")
_PDF_TRAILER = re.compile(rb"startxref\s+([0-9]{1,10})\s+%%EOF\s*")
_PDF_XREF = re.compile(rb"(?:xref|[0-9]+\s+[0-9]+\s+obj)(?=\s)")
_PNG_BIT_DEPTHS = {
    0: {1, 2, 4, 8, 16},
    2: {8, 16},
    3: {1, 2, 4, 8},
    4: {8, 16},
    6: {8, 16},
}
_JPEG_FRAME_MARKERS = {
    0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
    0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
}


class AttachmentValidationError(Exception):
    pass


@dataclass(frozen=True)
class ValidatedAttachment:
    file_name: str
    file_type: str
    size: int


def is_safe_filename(filename: str) -> bool:
    if (
        not filename
        or len(filename) > 255
        or filename != filename.strip()
        or filename.startswith(".")
        or filename.endswith(".")
        or any(char in '/\\<>:"|?*' for char in filename)
        or any(
            category(char) in {"Cc", "Cf", "Cs", "Zl", "Zp"} and char not in "\u200c\u200d"
            for char in filename
        )
    ):
        return False

    # Windows treats device names as reserved even when followed by an extension.
    return filename.split(".", 1)[0].rstrip().upper() not in _RESERVED_FILENAMES


def _has_pdf_structure(content: bytes) -> bool:
    trailer_offset = content.rfind(b"startxref")
    if trailer_offset < 0 or _PDF_HEADER.match(content) is None:
        return False

    trailer = _PDF_TRAILER.fullmatch(content, trailer_offset)
    if trailer is None:
        return False

    # Traditional PDFs point to an xref table. Newer files may use a stream object.
    xref_offset = int(trailer[1])
    if not 8 < xref_offset < trailer_offset:
        return False
    xref = _PDF_XREF.match(content, xref_offset, trailer_offset)
    return xref is not None and (
        xref[0] != b"xref" or content.rfind(b"trailer", xref.end(), trailer_offset) >= 0
    )


def _has_png_structure(content: bytes) -> bool:
    if len(content) < 33 or content[8:16] != b"\x00\x00\x00\rIHDR":
        return False

    width, height, depth, color, compression, filtering, interlace = unpack_from(
        ">IIBBBBB", content, 16
    )
    if (
        not 0 < width < 2**31
        or not 0 < height < 2**31
        or depth not in _PNG_BIT_DEPTHS.get(color, ())
        or compression != 0
        or filtering != 0
        or interlace not in (0, 1)
    ):
        return False

    data = memoryview(content)
    offset = 8
    previous_kind = b""
    has_palette = False
    has_idat = False
    idat_bytes = 0
    while offset + 12 <= len(data):
        length, kind = unpack_from(">I4s", data, offset)
        data_end = offset + 8 + length
        chunk_end = data_end + 4
        if chunk_end > len(data) or not kind.isalpha():
            return False
        if crc32(data[offset + 4:data_end]) != unpack_from(">I", data, data_end)[0]:
            return False
        if kind == b"IHDR" and offset != 8:
            return False
        if kind == b"PLTE":
            if (
                has_palette or has_idat or color in (0, 4)
                or not 0 < length <= 768 or length % 3
                or (color == 3 and length // 3 > 2**depth)
            ):
                return False
            has_palette = True
        if kind == b"IDAT":
            if (has_idat and previous_kind != kind) or (color == 3 and not has_palette):
                return False
            has_idat = True
            idat_bytes += length
        if kind == b"IEND":
            # A zlib stream needs a header, a deflate block, and a checksum.
            return length == 0 and chunk_end == len(data) and idat_bytes >= 8
        previous_kind = kind
        offset = chunk_end

    return False


def _has_jpeg_structure(content: bytes) -> bool:
    offset = 2  # Skip Start of Image.
    scan_start = None
    has_scan = False
    frame_components = 0
    height = 0

    while offset < len(content):
        marker_start = content.find(b"\xff", offset)
        if marker_start < 0 or (scan_start is None and marker_start != offset):
            return False

        marker_offset = marker_start + 1
        while marker_offset < len(content) and content[marker_offset] == 0xFF:
            marker_offset += 1
        if marker_offset == len(content):
            return False

        marker = content[marker_offset]
        offset = marker_offset + 1
        if scan_start is not None:
            # Escaped 0xFF bytes and restart markers remain part of a scan.
            if marker == 0 or 0xD0 <= marker <= 0xD7:
                continue
            if marker_start == scan_start:
                return False
            has_scan = True
            scan_start = None

        if marker == 0xD9:  # End of Image
            return has_scan and height > 0 and offset == len(content)
        if marker < 0xC0 or 0xD0 <= marker <= 0xD8 or offset + 2 > len(content):
            return False

        length = unpack_from(">H", content, offset)[0]
        if length < 2 or offset + length > len(content):
            return False
        if marker in _JPEG_FRAME_MARKERS:
            if length < 11:
                return False
            height, width, frame_components = unpack_from(">HHB", content, offset + 3)
            if width == 0 or frame_components == 0 or length != 8 + 3 * frame_components:
                return False
        if marker == 0xDA:  # Start of Scan
            if length < 8:
                return False
            scan_components = content[offset + 2]
            if (
                not 1 <= scan_components <= min(4, frame_components)
                or length != 6 + 2 * scan_components
            ):
                return False
            scan_start = offset + length
        if marker == 0xDC:  # Define Number of Lines (permitted after a zero frame height).
            if length != 4 or height != 0 or not frame_components:
                return False
            height = unpack_from(">H", content, offset + 2)[0]
        offset += length

    return False


_STRUCTURE_CHECKS = {
    "application/pdf": _has_pdf_structure,
    "image/png": _has_png_structure,
    "image/jpeg": _has_jpeg_structure,
}


def validate_attachment(
    filename: str,
    content_type: str,
    content: bytes,
) -> ValidatedAttachment:
    if not is_safe_filename(filename):
        raise AttachmentValidationError("Invalid filename.")

    extension = PurePath(filename).suffix.lower()

    if extension not in ALLOWED_TYPES:
        raise AttachmentValidationError("File type not allowed.")

    expected_type, signature = ALLOWED_TYPES[extension]

    if content_type.lower() != expected_type:
        raise AttachmentValidationError("File type mismatch.")

    if not content or len(content) > MAX_FILE_SIZE:
        raise AttachmentValidationError("Invalid file size.")

    if not content.startswith(signature):
        raise AttachmentValidationError("Invalid file signature.")

    # These structural checks avoid interpreting PDF objects or decoding pixels.
    if not _STRUCTURE_CHECKS[expected_type](content):
        raise AttachmentValidationError("Incomplete or malformed file.")

    return ValidatedAttachment(
        file_name=filename,
        file_type=expected_type,
        size=len(content),
    )


def ensure_upload_allowed(
    db: Session,
    student: Account,
    club_id: UUID,
    activity_id: UUID,
    revision_id: UUID,
) -> ActivityRevision:
    revision, year = authorized_revision(
        db,
        student,
        club_id,
        activity_id,
        revision_id,
    )

    require_uploadable_revision(revision)
    require_completion_window_open(db, year, ActivityError)
    return revision


def require_uploadable_revision(revision: ActivityRevision) -> None:
    if (
        revision.status != ActivityRevisionStatus.DRAFT
        or revision.content_purged_at is not None
    ):
        raise ActivityError(
            409,
            "Attachments can only be added to draft revisions.",
        )

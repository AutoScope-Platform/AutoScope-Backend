"""Work out what an uploaded file really is from its first bytes.

The `Content-Type` a client sends is only a claim: anyone can label an `.exe` as
`image/png`. Every upload (KYC documents, listing photos, claim evidence, chat
attachments) is checked against the file's signature ("magic bytes") instead, and the
detected type is what gets stored and served back.
"""

from __future__ import annotations

from collections.abc import Collection

from app.core.errors import BadRequest

# File extension used in storage keys for each type we accept anywhere.
EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "application/pdf": ".pdf",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/webm": ".webm",
}


def sniff(data: bytes) -> str | None:
    """The MIME type the bytes actually contain, or None if it is not one we accept."""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"%PDF"):
        return "application/pdf"
    if len(data) >= 12 and data[4:8] == b"ftyp":  # ISO media: MP4 and iPhone .mov
        return "video/quicktime" if data[8:12] == b"qt  " else "video/mp4"
    if data.startswith(b"\x1aE\xdf\xa3"):  # Matroska / WebM
        return "video/webm"
    return None


def checked_type(data: bytes, allowed: Collection[str], *, message: str, code: str) -> str:
    """Return the detected type if it is in `allowed`, otherwise raise `BadRequest`.

    Callers keep their own user-facing message and error code so the API stays stable.
    """
    kind = sniff(data)
    if kind is None or kind not in allowed:
        raise BadRequest(message, code=code)
    return kind

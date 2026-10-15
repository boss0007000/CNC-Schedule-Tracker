"""Upload validation + storage. Files live OUTSIDE the web root (fs) or in the database (db backend,
for hosts with ephemeral disks) and are only served to authenticated staff via signed, expiring links."""
import hashlib
import logging
import re
import secrets
import socket
import struct
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from app import security
from app.config import get_config
from app.errors import UserError
from app.models import FileBlob

log = logging.getLogger("files")

MAX_FILES = 5
MAX_BYTES = 10 * 1024 * 1024
LINK_TTL_SECONDS = 15 * 60

# extension -> acceptable client-declared MIME types (browsers send octet-stream for most CAD formats)
ALLOWED: dict[str, set[str]] = {
    ".pdf": {"application/pdf"},
    ".png": {"image/png"},
    ".jpg": {"image/jpeg"}, ".jpeg": {"image/jpeg"},
    ".webp": {"image/webp"},
    ".dxf": {"application/dxf", "image/vnd.dxf", "application/octet-stream", "text/plain", "application/x-dxf"},
    ".step": {"application/step", "model/step", "application/octet-stream", "text/plain", "application/STEP"},
    ".stp": {"application/step", "model/step", "application/octet-stream", "text/plain"},
    ".stl": {"model/stl", "application/sla", "application/vnd.ms-pki.stl", "application/octet-stream",
             "text/plain", "application/x-stl"},
}
PHOTO_EXT = {".png", ".jpg", ".jpeg", ".webp"}
MAGIC = {".pdf": [b"%PDF"], ".png": [b"\x89PNG\r\n\x1a\n"], ".jpg": [b"\xff\xd8\xff"], ".jpeg": [b"\xff\xd8\xff"],
         ".webp": [b"RIFF"]}
BAD_PREFIXES = (b"MZ", b"\x7fELF", b"#!", b"<?php", b"<script", b"<html", b"<!DOCTYPE", b"PK\x03\x04")
SERVE_MIME = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
              ".webp": "image/webp"}


@dataclass
class Stored:
    key: str
    original_name: str
    mime: str
    size: int
    sha256: str
    scan_status: str


def sanitize_filename(name: str) -> str:
    name = (name or "file").replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r"[^A-Za-z0-9._\- ]", "_", name).strip(" .")
    name = re.sub(r"_{2,}", "_", name)
    return (name or "file")[:120]


def scan_file(data: bytes, name: str) -> str:
    """VIRUS-SCAN HOOK. Returns 'clean' | 'skipped'; raise UserError to reject.

    If CLAMD_HOST is set (e.g. a ClamAV sidecar container) the bytes are streamed to clamd's INSTREAM
    command. Otherwise scanning is skipped and recorded as such. Replace this function to plug in any scanner."""
    import os
    host = os.environ.get("CLAMD_HOST")
    if not host:
        return "skipped"
    port = int(os.environ.get("CLAMD_PORT", "3310"))
    try:
        with socket.create_connection((host, port), timeout=10) as s:
            s.sendall(b"zINSTREAM\0")
            for i in range(0, len(data), 8192):
                chunk = data[i:i + 8192]
                s.sendall(struct.pack("!I", len(chunk)) + chunk)
            s.sendall(struct.pack("!I", 0))
            reply = s.recv(1024)
    except OSError:
        log.exception("clamd unreachable; failing closed")
        raise UserError("We couldn't scan your file right now. Please try again in a few minutes.") from None
    if b"FOUND" in reply:
        raise UserError(f"“{name}” was flagged by the virus scanner and was not accepted.")
    return "clean"


def validate(name: str, declared_mime: str | None, data: bytes, allowed_ext: set[str] | None = None) -> tuple[str, str]:
    """Return (safe_name, ext). Raises UserError with a specific reason."""
    safe = sanitize_filename(name)
    ext = Path(safe).suffix.lower()
    allowed = {e: m for e, m in ALLOWED.items() if allowed_ext is None or e in allowed_ext}
    if ext not in allowed:
        raise UserError(f"“{safe}” isn't an accepted file type. Allowed: {', '.join(sorted(allowed))}.", field="files")
    mime = (declared_mime or "").split(";")[0].strip().lower()
    if mime and mime not in {m.lower() for m in allowed[ext]}:
        raise UserError(f"“{safe}” doesn't look like a {ext} file (type {mime}).", field="files")
    if len(data) == 0:
        raise UserError(f"“{safe}” is empty.", field="files")
    if len(data) > MAX_BYTES:
        raise UserError(f"“{safe}” is larger than 10 MB.", field="files")
    if ext in MAGIC and not any(data.startswith(m) for m in MAGIC[ext]):
        raise UserError(f"“{safe}” isn't a valid {ext[1:].upper()} file.", field="files")
    if ext == ".webp" and data[8:12] != b"WEBP":
        raise UserError(f"“{safe}” isn't a valid WEBP file.", field="files")
    if ext not in MAGIC and data.lstrip()[:12].startswith(BAD_PREFIXES):
        raise UserError(f"“{safe}” contains content that isn't allowed.", field="files")
    return safe, ext


def _fs_path(key: str) -> Path:
    root = Path(get_config().upload_dir).resolve()
    p = (root / key).resolve()
    if root not in p.parents:
        raise ValueError("bad storage key")
    return p


def store(db: Session, name: str, declared_mime: str | None, data: bytes,
          allowed_ext: set[str] | None = None) -> Stored:
    safe, ext = validate(name, declared_mime, data, allowed_ext)
    scan = scan_file(data, safe)
    key = secrets.token_hex(16) + ext
    if get_config().storage_backend == "db":
        db.add(FileBlob(storage_key=key, data=data))
        db.flush()
    else:
        p = _fs_path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return Stored(key, safe, (declared_mime or "application/octet-stream")[:120], len(data),
                  hashlib.sha256(data).hexdigest(), scan)


def read(db: Session, key: str) -> bytes:
    if get_config().storage_backend == "db":
        blob = db.get(FileBlob, key)
        if blob is None:
            raise FileNotFoundError(key)
        return blob.data
    return _fs_path(key).read_bytes()


def delete(db: Session, key: str) -> None:
    blob = db.get(FileBlob, key)
    if blob:
        db.delete(blob)
    try:
        _fs_path(key).unlink(missing_ok=True)
    except ValueError:
        pass


def read_upload(upload, max_bytes: int = MAX_BYTES) -> bytes:
    """Read a Starlette UploadFile (sync) refusing to buffer more than the limit."""
    data = upload.file.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise UserError(f"“{sanitize_filename(upload.filename)}” is larger than 10 MB.", field="files")
    return data


def signed_link(kind: str, ident: int) -> str:
    return f"/admin/files/{kind}/{ident}?t=" + security.sign("file-link", {"k": kind, "id": ident})


def check_link(kind: str, ident: int, token: str | None) -> bool:
    d = security.unsign("file-link", token or "", LINK_TTL_SECONDS)
    return bool(d and d.get("k") == kind and d.get("id") == ident)


def download_mime(name: str) -> str:
    return SERVE_MIME.get(Path(name).suffix.lower(), "application/octet-stream")

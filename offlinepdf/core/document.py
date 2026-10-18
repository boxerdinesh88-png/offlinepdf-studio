"""Document session: open/save/recovery with pikepdf as source of truth.

Design notes (see docs/DECISIONS.md):
- The session holds the whole document as **bytes in memory**.  Nothing keeps
  a file handle open, so atomic ``os.replace`` saves always work on Windows.
- ``pikepdf`` owns every mutation and serialization; PyMuPDF is a derived,
  read-only view used for rendering and text extraction and is refreshed from
  fresh bytes after mutations (``refresh_view``).
- Saves are non-destructive by default: ``save_as`` refuses to overwrite
  unless explicitly asked, and every save is verified (reopen + page count +
  qpdf syntax check) *before* the target file is replaced.
- Inserted images live in ``session.images`` (an editable layer) and are only
  baked into the bytes written by ``export_bytes`` (save, autosave).  Baked
  images are lifted back into the layer on open, so they stay editable.
"""

from __future__ import annotations

import io
import logging
import os
import time
import uuid
from pathlib import Path

import pikepdf
import pymupdf

from offlinepdf.core.history import History
from offlinepdf.core.image_ops import ImageLayer, bake_images, lift_images
from offlinepdf.core.recovery import RecoveryStore

__all__ = [
    "CorruptDocumentError",
    "DocumentError",
    "PasswordRequiredError",
    "PdfSession",
]

logger = logging.getLogger(__name__)


class DocumentError(Exception):
    """Friendly base class for document problems (message is user-safe)."""


class PasswordRequiredError(DocumentError):
    """The document is encrypted and a correct password was not supplied."""


class CorruptDocumentError(DocumentError):
    """The file cannot be opened as a PDF."""


class PdfSession:
    """One open PDF document plus its undo history and dirty state."""

    def __init__(
        self,
        *,
        path: Path | None,
        pdf: pikepdf.Pdf,
        mupdf: pymupdf.Document,
        password: str,
        images: ImageLayer | None = None,
    ) -> None:
        self._path = path
        self._pdf = pdf
        self._mupdf = mupdf
        self._password = password
        self._dirty = False
        self._closed = False
        self._opened = time.monotonic()
        self.history = History(self)
        self.images = images if images is not None else ImageLayer()
        #: keep invisible OCR text on unedited lines when writing to disk
        self.keep_ocr_layer = True
        self.recovery = RecoveryStore()
        self.recovery_error: str | None = None

    # --- construction -----------------------------------------------------

    @classmethod
    def open(cls, path: str | Path, *, password: str = "") -> PdfSession:
        """Open ``path``. Raises PasswordRequiredError or CorruptDocumentError."""
        p = Path(path)
        try:
            data = p.read_bytes()
        except OSError as exc:
            raise DocumentError(f"Cannot read file: {p.name}") from exc
        return cls.open_bytes(data, source_path=p, password=password)

    @classmethod
    def open_bytes(
        cls,
        data: bytes,
        *,
        source_path: Path | None = None,
        password: str = "",
    ) -> PdfSession:
        """Open a document from memory (also used for recovery restore)."""
        if not data.startswith(b"%PDF"):
            # Be tolerant: some writers add junk before the header; pikepdf is too.
            idx = data.find(b"%PDF")
            if idx < 0 or idx > 1024:
                raise CorruptDocumentError("This file does not look like a PDF document.")
            data = data[idx:]

        try:
            pdf = pikepdf.Pdf.open(io.BytesIO(data), password=password)
        except pikepdf.PasswordError as exc:
            raise PasswordRequiredError(
                "The document is password protected. Enter the password to open it."
            ) from exc
        except pikepdf.PdfError as exc:
            raise CorruptDocumentError(
                "The document is damaged and could not be repaired."
            ) from exc

        try:
            images = lift_images(pdf)
        except Exception:  # never block opening over an image problem
            logger.exception("could not lift inserted images; keeping them baked")
            images = ImageLayer()
        if images:
            buf = io.BytesIO()
            pdf.save(buf, encryption=True if pdf.is_encrypted else None)
            data = buf.getvalue()

        try:
            mupdf = pymupdf.open(stream=data, filetype="pdf")
            if mupdf.needs_pass and not mupdf.authenticate(password):
                mupdf.close()
                pdf.close()
                raise PasswordRequiredError(
                    "The document is password protected. Enter the password to open it."
                )
        except PasswordRequiredError:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            candidate = locals().get("mupdf")
            if candidate is not None:
                candidate.close()
            pdf.close()
            raise CorruptDocumentError("The document could not be rendered.") from exc

        session = cls(
            path=source_path, pdf=pdf, mupdf=mupdf, password=password, images=images
        )
        warnings = session.pdf_warnings()
        if warnings:
            logger.warning("opened with %d qpdf warnings: %s", len(warnings), warnings[:3])
        logger.info(
            "opened %s (%d pages%s)",
            source_path.name if source_path else "<memory>",
            session.page_count,
            ", encrypted" if session.is_encrypted else "",
        )
        return session

    # --- properties -------------------------------------------------------

    @property
    def path(self) -> Path | None:
        return self._path

    @property
    def pdf(self) -> pikepdf.Pdf:
        self._ensure_open()
        return self._pdf

    @property
    def mupdf(self) -> pymupdf.Document:
        """Derived PyMuPDF view; call :meth:`refresh_view` after mutations."""
        self._ensure_open()
        return self._mupdf

    @property
    def page_count(self) -> int:
        return len(self._pdf.pages)

    @property
    def is_encrypted(self) -> bool:
        return bool(self._pdf.is_encrypted)

    @property
    def is_signed(self) -> bool:
        """True when the document carries digital signatures.

        Editing invalidates the signature, so the UI warns before it mutates
        a signed file (see ``/AcroForm`` ``/SigFlags`` and ``/FT /Sig``).
        """
        self._ensure_open()
        try:
            acroform = self._pdf.Root.get("/AcroForm")
            if acroform is None:
                return False
            sigflags = acroform.get("/SigFlags")
            if sigflags is not None and str(sigflags) not in ("0", ""):
                return True
            fields = acroform.get("/Fields")
            if fields is None:
                return False
            return any(str(field.get("/FT", "")) == "/Sig" for field in fields)
        except Exception:  # malformed dictionaries are not fatal
            return False

    @property
    def pdf_version(self) -> str:
        return str(self._pdf.pdf_version)

    @property
    def is_dirty(self) -> bool:
        return self._dirty

    @property
    def is_closed(self) -> bool:
        return self._closed

    @property
    def display_name(self) -> str:
        if self._path is not None:
            return self._path.name
        return "Untitled"

    def mark_dirty(self) -> None:
        """Flag unsaved changes (drives autosave and the window title)."""
        self._dirty = True

    def pdf_warnings(self) -> list[str]:
        """qpdf warnings recorded while opening."""
        try:
            return [str(w) for w in self._pdf.get_warnings()]
        except Exception:
            return []

    def metadata(self) -> dict[str, str]:
        """Document info dictionary (title/author/...) as plain strings."""
        out: dict[str, str] = {}
        try:
            for key, value in self._pdf.docinfo.items():
                out[str(key)] = str(value)
        except Exception:
            logger.exception("metadata read failed")
        return out

    # --- serialization ----------------------------------------------------

    def to_bytes(self) -> bytes:
        """Serialize the current document state (keeping encryption)."""
        buf = io.BytesIO()
        self._pdf.save(
            buf, encryption=True if self._pdf.is_encrypted else None
        )
        return buf.getvalue()

    def export_bytes(self) -> bytes:
        """Bytes for writing to disk: the document with the image layer baked in.

        The in-memory document is left untouched so images stay editable;
        baking happens on a throwaway copy.
        """
        data = self.to_bytes()
        strip_ocr = not self.keep_ocr_layer
        if not self.images and not strip_ocr:
            return data
        copy = pikepdf.Pdf.open(io.BytesIO(data), password=self._password)
        try:
            if strip_ocr:
                from offlinepdf.core.ocr.textlayer import strip_unedited

                strip_unedited(copy)
            if self.images:
                bake_images(copy, self._mupdf, self.images)
            buf = io.BytesIO()
            copy.save(buf, encryption=True if copy.is_encrypted else None)
            return buf.getvalue()
        finally:
            copy.close()

    def refresh_view(self) -> None:
        """Rebuild the PyMuPDF view from the authoritative pikepdf state."""
        self._ensure_open()
        data = self.to_bytes()
        self._mupdf.close()
        self._mupdf = pymupdf.open(stream=data, filetype="pdf")
        self._mupdf.authenticate(self._password)

    def replace_bytes(self, data: bytes) -> None:
        """Swap the whole document state (used by undo/redo commands).

        The path, password and dirty flag are preserved; the PyMuPDF view is
        rebuilt from the new bytes so rendering always matches the source of
        truth.
        """
        self._ensure_open()
        try:
            pdf = pikepdf.Pdf.open(io.BytesIO(data), password=self._password)
        except pikepdf.PdfError as exc:  # pragma: no cover - corrupted history
            raise DocumentError("Undo failed: the saved state is unreadable.") from exc
        self._pdf.close()
        self._pdf = pdf
        self.refresh_view()
        self.mark_dirty()

    def save_as(self, target: str | Path, *, overwrite: bool = False) -> Path:
        """Verify-then-atomically write to ``target``.

        The existing file is only replaced when ``overwrite`` is True and the
        write has passed reopen/page-count/syntax verification.
        """
        self._ensure_open()
        dest = Path(target)
        if dest.exists() and not overwrite:
            raise DocumentError(f"{dest.name} already exists (enable overwrite to replace it).")

        data = self.export_bytes()
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(f".{dest.name}.{uuid.uuid4().hex}.saving")
        try:
            tmp.write_bytes(data)
            self._verify_file(tmp)
            os.replace(tmp, dest)
        finally:
            tmp.unlink(missing_ok=True)

        old_path = self._path
        self._path = dest
        self._dirty = False
        if old_path is not None and old_path != dest:
            self.recovery.delete(old_path)
        self.recovery.delete(dest)
        logger.info("saved %s (%d bytes)", dest, len(data))
        return dest

    def save(self) -> Path:
        """Overwrite the current file (explicit user action only)."""
        if self._path is None:
            raise DocumentError("This document has no file yet; use Save As.")
        return self.save_as(self._path, overwrite=True)

    def _verify_file(self, path: Path) -> None:
        """Reopen strictly and compare before the target is replaced."""
        try:
            check = pikepdf.Pdf.open(
                path, attempt_recovery=False, password=self._password
            )
        except Exception as exc:
            raise DocumentError(
                "Verification failed: the saved file could not be reopened."
            ) from exc
        try:
            if len(check.pages) != self.page_count:
                raise DocumentError("Verification failed: page count changed after saving.")
            errors = check.check_pdf_syntax()
            if errors:
                logger.warning("syntax check reported: %s", errors[:5])
        finally:
            check.close()

    # --- recovery ---------------------------------------------------------

    def write_recovery(self) -> bool:
        """Autosave a snapshot; returns False when there is nothing to save."""
        if self._path is None or self._closed:
            return False
        try:
            self.recovery.write(self._path, self.export_bytes())
            return True
        except OSError as exc:
            self.recovery_error = str(exc)
            logger.warning("recovery write failed: %s", exc)
            return False

    def clear_recovery(self) -> None:
        """Drop the snapshot after a successful real save/close."""
        if self._path is not None:
            self.recovery.delete(self._path)

    # --- lifecycle --------------------------------------------------------

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._mupdf.close()
        finally:
            self._pdf.close()
        logger.info("closed %s", self.display_name)

    def _ensure_open(self) -> None:
        if self._closed:
            raise DocumentError("The document is already closed.")

    # --- convenience for UI/tests ----------------------------------------

    def render_pixmap(self, page_index: int, scale: float = 1.0) -> bytes:
        """Render one page at ``scale`` and return PNG bytes."""
        page = self._mupdf.load_page(page_index)
        pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
        return pix.tobytes("png")

    def verify_roundtrip(self) -> bool:
        """True when the current state serializes and reopens cleanly."""
        try:
            data = self.to_bytes()
            check = pikepdf.Pdf.open(
                io.BytesIO(data), attempt_recovery=False, password=self._password
            )
            ok = len(check.pages) == self.page_count
            check.close()
            return ok
        except Exception:
            return False

    def __enter__(self) -> PdfSession:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def age_seconds(self) -> float:
        """Time since open (diagnostics)."""
        return time.monotonic() - self._opened

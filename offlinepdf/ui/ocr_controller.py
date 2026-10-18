"""OCR controllers: background recognition without blocking the UI.

* :class:`OcrController` - M6 Tools action: recognise the visible page and
  surface the words in the status bar.
* :class:`MakeEditableController` - "Auto-Make Editable": a cancellable
  queue that recognises scanned pages (visible page first) on a worker
  thread.  The worker opens its own copy of the document (PyMuPDF objects
  are not thread-safe); results come back as ``OcrLine`` lists and are
  written into the document on the UI thread by the caller.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Any

import pymupdf
from PySide6.QtCore import QObject, QThread, Signal, Slot

from offlinepdf.core.document import PdfSession
from offlinepdf.core.ocr import OcrWord, ocr_page
from offlinepdf.core.ocr.cache import OcrCache
from offlinepdf.core.ocr.pipeline import OcrOptions, recognize_page

__all__ = ["MakeEditableController", "OcrController", "line_ordered"]

logger = logging.getLogger(__name__)

_DEFAULT_SCALE = 2.0


def line_ordered(words: list[OcrWord]) -> str:
    """Join recognised words into reading order (top-to-bottom, left-to-right)."""
    if not words:
        return ""
    ordered = sorted(words, key=lambda w: (round(w.box[0][1] / 6.0), w.box[0][0]))
    return " ".join(word.text for word in ordered)


class OcrController(QObject):
    """Runs OCR off the UI thread and reports results."""

    finished = Signal(list)  # list[OcrWord]
    failed = Signal(str)
    _worker: Any = None
    _thread: Any = None

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._busy = False

    @property
    def busy(self) -> bool:
        return self._busy

    def run(self, session: PdfSession | None, page_index: int, language: str) -> bool:
        """Start OCR; True when a job was launched (not already busy)."""
        if self._busy or session is None:
            return False
        self._busy = True

        self._thread = QThread(self)
        self._worker = _OcrJob(session, page_index, language)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._on_done)
        self._worker.failed.connect(self._on_fail)
        self._worker.finished.connect(self._thread.quit)
        self._worker.failed.connect(self._thread.quit)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.start()
        return True

    def _on_done(self, words: list[OcrWord]) -> None:
        self._busy = False
        self.finished.emit(words)

    def _on_fail(self, message: str) -> None:
        self._busy = False
        self.failed.emit(message)


class _OcrJob(QObject):
    """Pulls words from one page in a worker thread."""

    finished = Signal(list)  # list[OcrWord]
    failed = Signal(str)

    def __init__(self, session: PdfSession, page_index: int, language: str) -> None:
        super().__init__()
        self._session = session
        self._page_index = page_index
        self._language = language

    @Slot()
    def run(self) -> None:
        try:
            words = ocr_page(
                self._session, self._page_index, language=self._language, scale=_DEFAULT_SCALE
            )
        except Exception as exc:  # surfaced to the status bar / dialog
            logger.exception("OCR failed")
            self.failed.emit(str(exc))
            return
        self.finished.emit(words)


class MakeEditableController(QObject):
    """Queue of pages to make editable, processed one at a time off-thread."""

    pageStarted = Signal(int)
    pageReady = Signal(int, list)  # page index, list[OcrLine]
    pageFailed = Signal(int, str)
    progress = Signal(int, int)  # done, total (this run)
    finished = Signal(bool)  # cancelled?

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._session: PdfSession | None = None
        self._queue: deque[int] = deque()
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._thread: QThread | None = None
        self._worker: _MakeEditableJob | None = None
        self._seen: set[int] = set()  # queued or processed this session
        self._done = 0
        self._total = 0
        self.options = OcrOptions()
        self.cache: OcrCache | None = None

    # -- state ------------------------------------------------------------ #
    @property
    def busy(self) -> bool:
        return self._thread is not None

    def set_session(self, session: PdfSession | None) -> None:
        self.cancel()
        self._session = session
        self._seen.clear()
        self._done = self._total = 0

    def was_requested(self, page: int) -> bool:
        return page in self._seen

    def mark_handled(self, page: int) -> None:
        """Do not auto-queue ``page`` again (e.g. the user reverted it)."""
        self._seen.add(page)

    # -- queue ------------------------------------------------------------ #
    def enqueue(self, pages: list[int], *, front: bool = False, force: bool = False) -> int:
        """Queue pages (skipping ones already handled unless ``force``)."""
        if self._session is None:
            return 0
        added = 0
        with self._lock:
            for page in reversed(pages) if front else pages:
                if page in self._seen and not force:
                    if front and page in self._queue:
                        self._queue.remove(page)
                        self._queue.appendleft(page)
                    continue
                self._seen.add(page)
                if front:
                    self._queue.appendleft(page)
                else:
                    self._queue.append(page)
                added += 1
        self._total += added
        if added:
            self.progress.emit(self._done, self._total)
        self._start()
        return added

    def prioritize(self, page: int) -> None:
        """Move a queued page to the front (e.g. the user scrolled to it)."""
        with self._lock:
            if page in self._queue:
                self._queue.remove(page)
                self._queue.appendleft(page)

    def cancel(self) -> None:
        """Drop queued pages and stop after the page being recognised."""
        with self._lock:
            dropped = len(self._queue)
            for page in self._queue:
                self._seen.discard(page)
            self._queue.clear()
        self._total = max(0, self._total - dropped)
        if self._thread is not None:
            self._cancel.set()

    def wait(self, msecs: int = 60000) -> bool:
        """Block until the worker stops (tests / shutdown)."""
        thread = self._thread
        return thread.wait(msecs) if thread is not None else True

    # -- worker ----------------------------------------------------------- #
    def _start(self) -> None:
        if self._thread is not None or self._session is None:
            return
        with self._lock:
            if not self._queue:
                return
        try:
            data = self._session.mupdf.tobytes(garbage=0)  # decrypted snapshot
        except Exception as exc:
            logger.exception("snapshot for OCR failed")
            self.pageFailed.emit(-1, f"The document could not be prepared for OCR: {exc}")
            return
        self._cancel.clear()
        thread = QThread(self)
        worker = _MakeEditableJob(
            data, self._queue, self._lock, self._cancel, self.options, self.cache
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.pageStarted.connect(self.pageStarted.emit)
        worker.pageReady.connect(self._on_ready)
        worker.pageFailed.connect(self._on_failed)
        worker.done.connect(self._on_done)
        worker.done.connect(thread.quit)
        self._thread, self._worker = thread, worker
        thread.start()

    def _on_ready(self, page: int, lines: list[Any]) -> None:
        self._done += 1
        self.progress.emit(self._done, self._total)
        self.pageReady.emit(page, lines)

    def _on_failed(self, page: int, message: str) -> None:
        if page >= 0:
            self._done += 1
            self.progress.emit(self._done, self._total)
        self.pageFailed.emit(page, message)

    def _on_done(self, cancelled: bool) -> None:
        thread = self._thread
        self._thread = None
        self._worker = None
        if thread is not None:
            thread.wait(5000)
            thread.deleteLater()
        with self._lock:
            pending = bool(self._queue)
        if pending:
            # Pages queued after the snapshot (or for a newly opened document;
            # cancel() empties the queue, so anything left is new work).
            self._start()
            return
        self._done = self._total = 0
        self.finished.emit(cancelled)


class _MakeEditableJob(QObject):
    """Drains the shared queue against a private copy of the document."""

    pageStarted = Signal(int)
    pageReady = Signal(int, list)
    pageFailed = Signal(int, str)
    done = Signal(bool)

    def __init__(
        self,
        data: bytes,
        queue: deque[int],
        lock: threading.Lock,
        cancel: threading.Event,
        options: OcrOptions,
        cache: OcrCache | None,
    ) -> None:
        super().__init__()
        self._data = data
        self._queue = queue
        self._lock = lock
        self._cancel = cancel
        self._options = options
        self._cache = cache

    @Slot()
    def run(self) -> None:
        try:
            doc = pymupdf.open(stream=self._data, filetype="pdf")
        except Exception as exc:
            self.pageFailed.emit(-1, f"The document could not be opened for OCR: {exc}")
            self.done.emit(False)
            return
        try:
            while not self._cancel.is_set():
                with self._lock:
                    if not self._queue:
                        break
                    page = self._queue.popleft()
                if page >= doc.page_count:
                    continue
                self.pageStarted.emit(page)
                try:
                    lines = recognize_page(
                        doc,
                        page,
                        self._options,
                        cache=self._cache,
                        cancelled=self._cancel.is_set,
                    )
                except Exception as exc:  # reported per page, never fatal
                    logger.exception("OCR failed on page %d", page + 1)
                    self.pageFailed.emit(page, str(exc))
                    continue
                if self._cancel.is_set():
                    break
                self.pageReady.emit(page, lines)
        finally:
            doc.close()
            self.done.emit(self._cancel.is_set())

"""OCR engine: page image -> recognized words with confidence.

Wraps the vendored RapidOCR ONNX models behind a small, fully-offline API.
Heavy dependencies (``rapidocr_onnxruntime``, ``opencv``, ``onnxruntime``,
``numpy``) are imported lazily so that merely importing this module stays
light - the models themselves are never downloaded at runtime.
"""

from __future__ import annotations

import io
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from offlinepdf.core.document import PdfSession

from .manifest import LanguagePack, require_language

__all__ = ["OcrEngine", "OcrError", "OcrWord", "ocr_page", "shared_engine"]


class OcrError(RuntimeError):
    """Raised when OCR cannot be set up or run (missing deps/models)."""


@dataclass(frozen=True)
class OcrWord:
    """One recognized text region.

    ``box`` holds the four corners as ``(x, y)`` floats in **page points**
    (PDF coordinate space), so the UI can draw boxes without further math.
    """

    text: str
    confidence: float
    box: tuple[tuple[float, float], tuple[float, float], tuple[float, float], tuple[float, float]]


class OcrEngine:
    """Runs RapidOCR against one installed language pack.

    Instances are cheap to hold; construction of the ONNX runtime is deferred
    until the first :meth:`recognize` call, and results are reported in the
    pixel space of the input image.
    """

    def __init__(self, language: str = "en", root: Path | None = None) -> None:
        try:
            self.pack: LanguagePack = require_language(language, root)
        except KeyError as exc:
            raise OcrError(str(exc)) from exc
        self._rapid: Any | None = None
        self._lock = threading.Lock()

    def _client(self) -> Any:
        if self._rapid is None:
            try:
                from rapidocr_onnxruntime import RapidOCR
            except ImportError as exc:  # pragma: no cover - deps vendored in lock
                raise OcrError("rapidocr-onnxruntime is not installed") from exc
            try:
                self._rapid = RapidOCR(
                    det_model_path=str(self.pack.det_model),
                    rec_model_path=str(self.pack.rec_model),
                    use_angle_cls=False,
                )
            except Exception as exc:
                raise OcrError(f"failed to initialise OCR engine: {exc}") from exc
        return self._rapid

    def recognize(self, image: bytes) -> list[OcrWord]:
        """Recognize text in a PNG (or any PIL-decodable) image.

        Returns empty list when no text region passes the confidence gate.
        The box coordinates are in the input image's pixel space.
        """
        try:
            import numpy as np
            from PIL import Image

            arr = np.asarray(Image.open(io.BytesIO(image)).convert("RGB"))
        except Exception as exc:
            raise OcrError(f"cannot decode OCR input image: {exc}") from exc
        return self.recognize_array(arr)

    def recognize_crops(self, crops: list[Any]) -> list[tuple[str, float]]:
        """Recognition only (no detection) for pre-cut RGB crops, e.g. words."""
        if not crops:
            return []
        client = self._client()
        try:
            with self._lock:
                results, _elapse = client.text_recognizer(crops)
        except Exception as exc:
            raise OcrError(f"OCR inference failed: {exc}") from exc
        return [(str(text), float(score)) for text, score in results]

    def recognize_array(self, arr: Any) -> list[OcrWord]:
        """Recognize text in an RGB ``numpy`` array (H x W x 3, uint8).

        Calls are serialised per engine: the ONNX sessions already use every
        core, and RapidOCR keeps per-call state.
        """
        client = self._client()
        try:
            with self._lock:
                result, _elapse = client(arr)
        except Exception as exc:
            raise OcrError(f"OCR inference failed: {exc}") from exc
        if not result:
            return []
        words: list[OcrWord] = []
        for row in result:
            box_pts, text, score = row[0], row[1], float(row[2])
            box = (
                (float(box_pts[0][0]), float(box_pts[0][1])),
                (float(box_pts[1][0]), float(box_pts[1][1])),
                (float(box_pts[2][0]), float(box_pts[2][1])),
                (float(box_pts[3][0]), float(box_pts[3][1])),
            )
            words.append(OcrWord(text=str(text), confidence=score, box=box))
        return words


_ENGINES: dict[str, OcrEngine] = {}
_ENGINES_LOCK = threading.Lock()


def shared_engine(language: str = "en") -> OcrEngine:
    """Process-wide engine per language (model loading costs ~1 s)."""
    with _ENGINES_LOCK:
        engine = _ENGINES.get(language)
        if engine is None:
            engine = OcrEngine(language)
            _ENGINES[language] = engine
        return engine


def ocr_page(
    session: PdfSession,
    page_index: int,
    language: str = "en",
    *,
    scale: float = 2.0,
) -> list[OcrWord]:
    """Run OCR on ``page_index`` and return words in PDF point coordinates.

    The page is rendered to an image at ``scale`` (points -> pixels); box
    coordinates are divided by ``scale`` to land back in PDF space.
    """
    engine = OcrEngine(language)
    pixmap = session.render_pixmap(page_index, scale)
    words = engine.recognize(pixmap)
    if scale == 1.0:
        return words
    rescaled: list[OcrWord] = []
    for word in words:
        box = (
            (word.box[0][0] / scale, word.box[0][1] / scale),
            (word.box[1][0] / scale, word.box[1][1] / scale),
            (word.box[2][0] / scale, word.box[2][1] / scale),
            (word.box[3][0] / scale, word.box[3][1] / scale),
        )
        rescaled.append(OcrWord(text=word.text, confidence=word.confidence, box=box))
    return rescaled

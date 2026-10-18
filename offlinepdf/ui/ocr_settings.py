"""OCR settings dialog (Tools ▸ OCR Settings…)."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from offlinepdf.config import Settings
from offlinepdf.core.ocr import available_languages
from offlinepdf.core.ocr.pipeline import OcrOptions
from offlinepdf.core.ocr.preprocess import PreprocessOptions

__all__ = ["OcrSettingsDialog", "ocr_options_from"]


def ocr_options_from(settings: Settings) -> OcrOptions:
    """Build pipeline options from the stored settings."""
    return OcrOptions(
        language=settings.get_str("ocr_language", "en") or "en",
        dpi=max(100, min(600, settings.get_int("ocr_dpi", 300))),
        preprocess=PreprocessOptions(
            deskew=settings.get_bool("ocr_deskew", True),
            denoise=settings.get_bool("ocr_denoise", True),
            contrast=settings.get_bool("ocr_contrast", True),
            binarize=settings.get_bool("ocr_binarize", False),
        ),
    )


class OcrSettingsDialog(QDialog):
    """Edits the ``ocr_*`` settings; :meth:`exec` returns Accepted when saved."""

    def __init__(self, settings: Settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("OCR Settings")
        self._settings = settings
        self.cache_cleared = False
        layout = QVBoxLayout(self)

        general = QGroupBox("Make scanned pages editable")
        form = QFormLayout(general)
        self._auto = QCheckBox("Automatically when a scanned PDF is opened")
        self._auto.setChecked(settings.get_bool("ocr_auto", True))
        form.addRow(self._auto)
        self._language = QComboBox()
        current = settings.get_str("ocr_language", "en")
        for pack in available_languages():
            self._language.addItem(pack.name, pack.code)
            if pack.code == current:
                self._language.setCurrentIndex(self._language.count() - 1)
        form.addRow("Language", self._language)
        self._dpi = QSpinBox()
        self._dpi.setRange(100, 600)
        self._dpi.setSingleStep(50)
        self._dpi.setSuffix(" dpi")
        self._dpi.setValue(settings.get_int("ocr_dpi", 300))
        form.addRow("Render resolution", self._dpi)
        self._threshold = QDoubleSpinBox()
        self._threshold.setRange(0.0, 100.0)
        self._threshold.setSuffix(" %")
        self._threshold.setDecimals(0)
        raw = settings.get("ocr_review_threshold", 0.70)
        self._threshold.setValue(100.0 * float(raw if isinstance(raw, (int, float)) else 0.70))
        form.addRow("Highlight words below", self._threshold)
        self._searchable = QCheckBox("Keep invisible text on unedited lines when saving")
        self._searchable.setToolTip("Makes the whole scanned document searchable and selectable")
        self._searchable.setChecked(settings.get_bool("ocr_searchable_layer", True))
        form.addRow(self._searchable)
        layout.addWidget(general)

        prep = QGroupBox("Image clean-up before recognition")
        prep_form = QFormLayout(prep)
        self._deskew = QCheckBox("Straighten skewed scans")
        self._deskew.setChecked(settings.get_bool("ocr_deskew", True))
        self._denoise = QCheckBox("Remove speckle noise")
        self._denoise.setChecked(settings.get_bool("ocr_denoise", True))
        self._contrast = QCheckBox("Boost contrast")
        self._contrast.setChecked(settings.get_bool("ocr_contrast", True))
        self._binarize = QCheckBox("Black && white (only for very faint scans)")
        self._binarize.setChecked(settings.get_bool("ocr_binarize", False))
        for box in (self._deskew, self._denoise, self._contrast, self._binarize):
            prep_form.addRow(box)
        layout.addWidget(prep)

        clear = QPushButton("Clear OCR cache")
        clear.clicked.connect(self._clear_cache)
        layout.addWidget(clear)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _clear_cache(self) -> None:
        from offlinepdf.core.ocr.cache import OcrCache

        OcrCache().clear()
        self.cache_cleared = True

    def _save(self) -> None:
        s = self._settings
        s.set("ocr_auto", self._auto.isChecked())
        code = self._language.currentData()
        if code:
            s.set("ocr_language", str(code))
        s.set("ocr_dpi", self._dpi.value())
        s.set("ocr_review_threshold", round(self._threshold.value() / 100.0, 3))
        s.set("ocr_searchable_layer", self._searchable.isChecked())
        s.set("ocr_deskew", self._deskew.isChecked())
        s.set("ocr_denoise", self._denoise.isChecked())
        s.set("ocr_contrast", self._contrast.isChecked())
        s.set("ocr_binarize", self._binarize.isChecked())
        s.save()
        self.accept()

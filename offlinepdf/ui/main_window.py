"""Main window: file lifecycle, viewer chrome, thumbnails, status, themes."""

from __future__ import annotations

import logging
from pathlib import Path

import pymupdf
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QCloseEvent,
    QDragEnterEvent,
    QDropEvent,
    QKeySequence,
)
from PySide6.QtWidgets import (
    QApplication,
    QDockWidget,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QStyle,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from offlinepdf import APP_NAME, __version__
from offlinepdf.config import Settings, version_banner
from offlinepdf.core.document import (
    CorruptDocumentError,
    DocumentError,
    PasswordRequiredError,
    PdfSession,
)
from offlinepdf.core.image_ops import SUPPORTED_EXTENSIONS, ExportOptions
from offlinepdf.core.ocr import OcrWord
from offlinepdf.core.ocr.cache import OcrCache
from offlinepdf.core.ocr.detect import classify_page, scan_dpi
from offlinepdf.core.ocr.layout import OcrLine
from offlinepdf.core.ocr.textlayer import make_page_editable, rerecognise_page, revert_page
from offlinepdf.core.recovery import RecoveryStore, TempWorkspace, purge_stale_recovery
from offlinepdf.ui.edit_controller import EditController
from offlinepdf.ui.fonts import register_bundled_fonts
from offlinepdf.ui.image_panel import ImagePanel
from offlinepdf.ui.image_tool import ImageController
from offlinepdf.ui.inspect_controller import InspectController
from offlinepdf.ui.inspector import StyleInspector
from offlinepdf.ui.ocr_controller import MakeEditableController, OcrController, line_ordered
from offlinepdf.ui.ocr_review import OcrReviewOverlay
from offlinepdf.ui.ocr_settings import OcrSettingsDialog, ocr_options_from
from offlinepdf.ui.theme import apply_theme
from offlinepdf.ui.thumbnails import ThumbnailList
from offlinepdf.ui.viewer import MARGIN_PT, Viewer

__all__ = ["MainWindow"]

logger = logging.getLogger(__name__)

_MAX_PASSWORD_TRIES = 3
_LOW_RES_DPI = 150.0  # scans below this are read less reliably

MODE_VIEW = "view"
MODE_INSPECT = "inspect"
MODE_EDIT = "edit"


class MainWindow(QMainWindow):
    """Top-level window hosting the viewer and document lifecycle."""

    def __init__(self, settings: Settings | None = None) -> None:
        super().__init__()
        self.settings = settings or Settings()
        self._session: PdfSession | None = None
        self._updating_ui = False

        self.setWindowTitle(APP_NAME)
        self.resize(1200, 840)
        register_bundled_fonts()  # WYSIWYG editing with the faces we embed
        self.setAcceptDrops(True)

        self._build_ui()
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
        self._build_actions()
        self._build_menus()
        self._build_toolbar()
        self._restore_settings()
        self._connect_viewer()
        self._update_actions_enabled()

        self._autosave_timer = QTimer(self)
        interval = max(5, self.settings.get_int("autosave_seconds", 30)) * 1000
        self._autosave_timer.setInterval(interval)
        self._autosave_timer.timeout.connect(self._autosave_tick)
        self._autosave_timer.start()

        purge_stale_recovery()
        logger.info("main window ready")

    # --- construction -----------------------------------------------------

    def _build_ui(self) -> None:
        self._viewer = Viewer(self)

        self._thumbs = ThumbnailList(self)
        self._thumbs.setObjectName("thumbnails")
        thumb_dock = QDockWidget("Pages", self)
        thumb_dock.setObjectName("thumbnailsDock")
        thumb_dock.setWidget(self._thumbs)
        thumb_dock.setFeatures(
            QDockWidget.DockWidgetFeature.DockWidgetMovable
            | QDockWidget.DockWidgetFeature.DockWidgetFloatable
        )
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, thumb_dock)
        self._thumb_dock = thumb_dock

        # Page/zoom controls live in a thin strip above the viewer.
        self._page_spin = QSpinBox(self)
        self._page_spin.setMinimum(1)
        self._page_spin.setMaximum(1)
        self._page_spin.setFixedWidth(72)
        self._page_spin.valueChanged.connect(self._on_page_spin)

        self._page_label = QLabel("of 0", self)
        self._zoom_label = QLabel("100%", self)
        self._zoom_label.setFixedWidth(56)
        self._zoom_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        top = QWidget(self)
        top.setObjectName("pageStrip")
        top.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        top_layout = QHBoxLayout(top)
        top_layout.setContentsMargins(int(MARGIN_PT), 6, int(MARGIN_PT), 2)
        top_layout.addWidget(QLabel("Page", top))
        top_layout.addWidget(self._page_spin)
        top_layout.addWidget(self._page_label)
        top_layout.addStretch(1)
        top_layout.addWidget(self._zoom_label)
        top.setFixedHeight(34)

        # QMainWindow::setCentralWidget replaces; so wrap viewer+top strip.
        container = QWidget(self)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(top)
        layout.addWidget(self._build_ocr_bar(container))
        layout.addWidget(self._viewer, 1)
        self.setCentralWidget(container)

        self._status_message = QLabel("", self)
        self._status_message.setObjectName("statusMessage")
        self.statusBar().addWidget(self._status_message, 1)
        self._status_page = QLabel("", self)
        self._status_zoom = QLabel("", self)
        self.statusBar().addPermanentWidget(self._status_page)
        self.statusBar().addPermanentWidget(self._status_zoom)

        # Right dock: style inspector (M2) + glyph coverage + presets.
        self._inspector = StyleInspector(self)
        style_dock = QDockWidget("Style", self)
        style_dock.setObjectName("styleDock")
        style_dock.setWidget(self._inspector)
        style_dock.setFeatures(
            QDockWidget.DockWidgetFeature.DockWidgetMovable
            | QDockWidget.DockWidgetFeature.DockWidgetFloatable
        )
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, style_dock)
        self._style_dock = style_dock

        self._controller = InspectController(self._viewer, self._inspector, self)
        self._edit_controller = EditController(self._viewer, self._controller, self)
        self._ocr_controller = OcrController(self)
        self._mode = MODE_VIEW

        # Image tool: on-canvas editing + properties dock, tabbed with Style.
        self._image_controller = ImageController(self._viewer, self)
        self._image_panel = ImagePanel(self._image_controller, self)
        image_dock = QDockWidget("Image", self)
        image_dock.setObjectName("imageDock")
        image_dock.setWidget(self._image_panel)
        image_dock.setFeatures(
            QDockWidget.DockWidgetFeature.DockWidgetMovable
            | QDockWidget.DockWidgetFeature.DockWidgetFloatable
        )
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, image_dock)
        self.tabifyDockWidget(style_dock, image_dock)
        style_dock.raise_()
        self._image_dock = image_dock

        # Auto-Make Editable: scanned-page detection + background OCR queue.
        self._make_editable = MakeEditableController(self)
        self._make_editable.cache = OcrCache()
        self._ocr_review = OcrReviewOverlay(self._viewer.canvas)
        self._scan_timer = QTimer(self)
        self._scan_timer.setInterval(0)
        self._scan_timer.timeout.connect(self._scan_tick)
        self._scan_next = 0
        self._scanned_pages: list[int] = []
        self._pending_edit: tuple[int, float, float] | None = None
        self._rerecognise: set[int] = set()
        self._low_res_note = ""
        self._ocr_failures: list[str] = []

    def _build_ocr_bar(self, parent: QWidget) -> QFrame:
        """Notice strip shown while scanned pages are made editable."""
        bar = QFrame(parent)
        bar.setObjectName("ocrNotice")
        bar.setFrameShape(QFrame.Shape.StyledPanel)
        row = QHBoxLayout(bar)
        row.setContentsMargins(int(MARGIN_PT), 4, int(MARGIN_PT), 4)
        self._ocr_label = QLabel("", bar)
        self._ocr_label.setWordWrap(True)
        self._ocr_progress = QProgressBar(bar)
        self._ocr_progress.setFixedWidth(180)
        self._ocr_progress.setTextVisible(True)
        self._ocr_start_btn = QPushButton("Make editable", bar)
        self._ocr_start_btn.setProperty("primary", True)
        self._ocr_start_btn.clicked.connect(self._make_all_editable)
        self._ocr_cancel_btn = QPushButton("Cancel", bar)
        self._ocr_cancel_btn.clicked.connect(self._cancel_ocr)
        self._ocr_close_btn = QPushButton("Dismiss", bar)
        self._ocr_close_btn.clicked.connect(bar.hide)
        row.addWidget(self._ocr_label, 1)
        row.addWidget(self._ocr_progress)
        row.addWidget(self._ocr_start_btn)
        row.addWidget(self._ocr_cancel_btn)
        row.addWidget(self._ocr_close_btn)
        bar.hide()
        self._ocr_bar = bar
        return bar

    def _build_actions(self) -> None:
        style = self.style()
        self.act_open = QAction(
            style.standardIcon(QStyle.StandardPixmap.SP_DialogOpenButton), "Open…", self
        )
        self.act_open.setShortcut(QKeySequence.StandardKey.Open)
        self.act_open.triggered.connect(self.open_dialog)

        self.act_save = QAction(
            style.standardIcon(QStyle.StandardPixmap.SP_DialogSaveButton), "Save", self
        )
        self.act_save.setShortcut(QKeySequence.StandardKey.Save)
        self.act_save.triggered.connect(self.save)

        self.act_save_as = QAction("Save As…", self)
        self.act_save_as.setShortcut(QKeySequence.StandardKey.SaveAs)
        self.act_save_as.triggered.connect(self.save_as)

        self.act_close_doc = QAction("Close Document", self)
        self.act_close_doc.triggered.connect(self.close_document)

        self.act_undo = QAction("Undo", self)
        self.act_undo.setShortcut(QKeySequence.StandardKey.Undo)
        self.act_undo.triggered.connect(self._undo)
        self.act_undo.setEnabled(False)

        self.act_redo = QAction("Redo", self)
        self.act_redo.setShortcut(QKeySequence.StandardKey.Redo)
        self.act_redo.triggered.connect(self._redo)
        self.act_redo.setEnabled(False)

        self.act_quit = QAction("Quit", self)
        self.act_quit.setShortcut(QKeySequence.StandardKey.Quit)
        self.act_quit.triggered.connect(self.close)

        self.act_zoom_in = QAction("Zoom In", self)
        self.act_zoom_in.setShortcut(QKeySequence.StandardKey.ZoomIn)
        self.act_zoom_in.triggered.connect(self._viewer.zoom_in)

        self.act_zoom_out = QAction("Zoom Out", self)
        self.act_zoom_out.setShortcut(QKeySequence.StandardKey.ZoomOut)
        self.act_zoom_out.triggered.connect(self._viewer.zoom_out)

        self.act_actual = QAction("Actual Size", self)
        self.act_actual.setShortcut("Ctrl+1")
        self.act_actual.triggered.connect(self._viewer.actual_size)

        self.act_fit_width = QAction("Fit Width", self)
        self.act_fit_width.setShortcut("Ctrl+0")
        self.act_fit_width.triggered.connect(self._viewer.fit_width)

        self.act_next_page = QAction("Next Page", self)
        self.act_next_page.setShortcut("PgDown")
        self.act_next_page.triggered.connect(
            lambda: self._goto_page(self._viewer.current_page() + 1)
        )

        self.act_prev_page = QAction("Previous Page", self)
        self.act_prev_page.setShortcut("PgUp")
        self.act_prev_page.triggered.connect(
            lambda: self._goto_page(self._viewer.current_page() - 1)
        )

        self.act_thumbnails = QAction("Thumbnails", self)
        self.act_thumbnails.setCheckable(True)
        self.act_thumbnails.setShortcut("F4")
        self.act_thumbnails.toggled.connect(self._thumb_dock.setVisible)

        self.act_dark = QAction("Dark Mode", self)
        self.act_dark.setCheckable(True)
        self.act_dark.setShortcut("Ctrl+Alt+D")
        self.act_dark.toggled.connect(self._on_dark_toggled)

        self.act_about = QAction("About OfflinePDF Studio…", self)
        self.act_about.triggered.connect(self._show_about)

        self.act_help = QAction("Help", self)
        self.act_help.setShortcut(QKeySequence.StandardKey.HelpContents)
        self.act_help.triggered.connect(self._show_help)

        self.act_thumbnails_dock = QAction("Style Inspector", self)
        self.act_thumbnails_dock.setCheckable(True)
        self.act_thumbnails_dock.setChecked(True)
        self.act_thumbnails_dock.toggled.connect(self._style_dock.setVisible)

        # Mode actions (View / Inspect / Edit) - mutually exclusive.
        self._mode_group = QActionGroup(self)
        self._mode_group.setExclusive(True)
        self.act_mode_view = self._make_mode_action("View", MODE_VIEW)
        self.act_mode_inspect = self._make_mode_action("Inspect", MODE_INSPECT)
        self.act_mode_edit = self._make_mode_action("Edit", MODE_EDIT)
        self.act_mode_inspect.setChecked(True)

        self.act_add_image = QAction("Add Image…", self)
        self.act_add_image.setShortcut("Ctrl+Shift+I")
        self.act_add_image.setToolTip("Insert a PNG, JPG or WebP image (or drop / paste one)")
        self.act_add_image.triggered.connect(self.add_image_dialog)

        self.act_cut = QAction("Cut Image", self)
        self.act_cut.setShortcut(QKeySequence.StandardKey.Cut)
        self.act_cut.triggered.connect(lambda: self._image_controller.copy_selected(cut=True))

        self.act_copy = QAction("Copy Image", self)
        self.act_copy.setShortcut(QKeySequence.StandardKey.Copy)
        self.act_copy.triggered.connect(lambda: self._image_controller.copy_selected())

        self.act_paste = QAction("Paste", self)
        self.act_paste.setShortcut(QKeySequence.StandardKey.Paste)
        self.act_paste.triggered.connect(self._paste)

        self.act_duplicate = QAction("Duplicate Image", self)
        self.act_duplicate.setShortcut("Ctrl+D")
        self.act_duplicate.triggered.connect(self._image_controller.duplicate_selected)

        self.act_delete_image = QAction("Delete Image", self)
        self.act_delete_image.triggered.connect(self._image_controller.delete_selected)

        self.act_make_editable = QAction("Make Text Editable", self)
        self.act_make_editable.setShortcut("Ctrl+Shift+E")
        self.act_make_editable.setToolTip(
            "Recognise the text of the visible scanned page and make it editable"
        )
        self.act_make_editable.triggered.connect(self._make_current_page_editable)

        self.act_revert_scan = QAction("Revert Page to Original Scan", self)
        self.act_revert_scan.setToolTip(
            "Remove recognised text and all text edits from the visible page"
        )
        self.act_revert_scan.triggered.connect(self._revert_current_page)

        self.act_make_searchable = QAction("Make Document Searchable", self)
        self.act_make_searchable.setToolTip(
            "Add an invisible text layer to every scanned page (looks unchanged)"
        )
        self.act_make_searchable.triggered.connect(self._make_document_searchable)

        self.act_ocr_settings = QAction("OCR Settings…", self)
        self.act_ocr_settings.triggered.connect(self._show_ocr_settings)

        self.act_ocr_review = QAction("Highlight Uncertain OCR Text", self)
        self.act_ocr_review.setCheckable(True)
        self.act_ocr_review.setChecked(True)
        self.act_ocr_review.toggled.connect(self._on_ocr_review_toggled)

        self.act_ocr_page = QAction("Recognize Text (OCR)…", self)
        self.act_ocr_page.setShortcut("Ctrl+Shift+R")
        self.act_ocr_page.setEnabled(False)
        self.act_ocr_page.triggered.connect(self._run_ocr_on_current_page)

    def _build_menus(self) -> None:
        m_file = self.menuBar().addMenu("&File")
        m_file.addAction(self.act_open)
        m_file.addAction(self.act_save)
        m_file.addAction(self.act_save_as)
        m_file.addAction(self.act_close_doc)
        m_file.addSeparator()
        m_file.addAction(self.act_quit)

        m_edit = self.menuBar().addMenu("&Edit")
        m_edit.addAction(self.act_undo)
        m_edit.addAction(self.act_redo)
        m_edit.addSeparator()
        m_edit.addAction(self.act_cut)
        m_edit.addAction(self.act_copy)
        m_edit.addAction(self.act_paste)
        m_edit.addAction(self.act_duplicate)
        m_edit.addAction(self.act_delete_image)

        m_view = self.menuBar().addMenu("&View")
        m_view.addAction(self.act_zoom_in)
        m_view.addAction(self.act_zoom_out)
        m_view.addAction(self.act_actual)
        m_view.addAction(self.act_fit_width)
        m_view.addSeparator()
        m_view.addAction(self.act_next_page)
        m_view.addAction(self.act_prev_page)
        m_view.addSeparator()
        m_view.addAction(self.act_thumbnails)
        m_view.addAction(self.act_thumbnails_dock)
        m_view.addAction(self._image_dock.toggleViewAction())
        m_view.addAction(self.act_ocr_review)
        m_view.addAction(self.act_dark)

        m_mode = self.menuBar().addMenu("&Mode")
        m_mode.addAction(self.act_mode_view)
        m_mode.addAction(self.act_mode_inspect)
        m_mode.addAction(self.act_mode_edit)

        m_insert = self.menuBar().addMenu("&Insert")
        m_insert.addAction(self.act_add_image)

        m_tools = self.menuBar().addMenu("&Tools")
        m_tools.addAction(self.act_make_editable)
        m_tools.addAction(self.act_make_searchable)
        m_tools.addAction(self.act_revert_scan)
        m_tools.addAction(self.act_ocr_page)
        m_tools.addSeparator()
        m_tools.addAction(self.act_ocr_settings)

        m_help = self.menuBar().addMenu("&Help")
        m_help.addAction(self.act_help)
        m_help.addAction(self.act_about)

    def _build_toolbar(self) -> None:
        bar = QToolBar("Main", self)
        bar.setObjectName("mainToolbar")
        bar.setMovable(False)
        bar.addAction(self.act_open)
        bar.addAction(self.act_save)
        bar.addSeparator()
        bar.addAction(self.act_prev_page)
        bar.addAction(self.act_next_page)
        bar.addSeparator()
        bar.addAction(self.act_zoom_out)
        bar.addAction(self.act_zoom_in)
        bar.addAction(self.act_fit_width)
        bar.addSeparator()
        bar.addAction(self.act_thumbnails)
        bar.addAction(self.act_dark)
        bar.addSeparator()
        bar.addAction(self.act_mode_view)
        bar.addAction(self.act_mode_inspect)
        bar.addAction(self.act_mode_edit)
        bar.addSeparator()
        bar.addAction(self.act_make_editable)
        bar.addAction(self.act_add_image)
        self.addToolBar(bar)

    def _make_mode_action(self, label: str, mode: str) -> QAction:
        action = QAction(label, self)
        action.setCheckable(True)
        action.setData(mode)
        self._mode_group.addAction(action)
        action.triggered.connect(self._on_mode_changed)
        return action

    def _on_mode_changed(self) -> None:
        action = self._mode_group.checkedAction()
        mode = str(action.data()) if action is not None else MODE_VIEW
        self._mode = mode
        self._controller.set_enabled(mode in (MODE_INSPECT, MODE_EDIT))
        if mode != MODE_EDIT:
            self._edit_controller.close_editor()
        if mode == MODE_EDIT and self._session is not None:
            self._set_status("Edit mode: double-click a line of text to edit it")
            self._auto_ocr_visible_page()
        elif mode == MODE_INSPECT:
            self._set_status("Inspect mode: click text to see its detected style")
        else:
            self._set_status("")

    @property
    def mode(self) -> str:
        """Current interaction mode: ``view`` / ``inspect`` / ``edit``."""
        return self._mode

    @property
    def controller(self) -> InspectController:
        """Inspect-mode controller (hit testing, profiles, overlays)."""
        return self._controller

    def _restore_settings(self) -> None:
        self.act_thumbnails.setChecked(self.settings.get_bool("thumbnails_visible", True))
        self._thumb_dock.setVisible(self.act_thumbnails.isChecked())
        dark = self.settings.get_str("theme", "light") == "dark"
        # apply without re-saving
        self.act_dark.blockSignals(True)
        self.act_dark.setChecked(dark)
        self.act_dark.blockSignals(False)
        app = QApplication.instance()
        if app is not None:
            apply_theme(app, dark)  # type: ignore[arg-type]

    def _connect_viewer(self) -> None:
        self._viewer.zoomChanged.connect(self._on_zoom_changed)
        self._viewer.visiblePageChanged.connect(self._on_visible_page)
        self._thumbs.pageSelected.connect(self._goto_page)
        self._controller.statusChanged.connect(self._on_inspect_status)
        self._controller.editRequested.connect(self._on_edit_requested)
        self._edit_controller.statusChanged.connect(self._on_inspect_status)
        self._ocr_controller.finished.connect(self._on_ocr_finished)
        self._ocr_controller.failed.connect(self._on_ocr_failed)
        self._image_controller.statusChanged.connect(self._on_inspect_status)
        self._image_controller.errorOccurred.connect(
            lambda message: QMessageBox.warning(self, APP_NAME, message)
        )
        self._image_controller.selectionChanged.connect(self._on_image_selection)
        self._image_panel.exportOptionsChanged.connect(self._on_image_export_options)
        self._make_editable.pageStarted.connect(self._on_ocr_page_started)
        self._make_editable.pageReady.connect(self._on_ocr_page_ready)
        self._make_editable.pageFailed.connect(self._on_ocr_page_failed)
        self._make_editable.progress.connect(self._on_ocr_progress)
        self._make_editable.finished.connect(self._on_ocr_finished_all)
        self._viewer.spanClicked.connect(self._on_canvas_click)
        self._viewer.visiblePageChanged.connect(self._make_editable.prioritize)
        self._apply_ocr_settings()
        self._image_panel.set_export_options(self._image_export_options())
        self._on_mode_changed()

    # --- document lifecycle ----------------------------------------------

    def eventFilter(self, watched: object, event: object) -> bool:
        # macOS (and some Linux desktops) deliver "open file" requests as
        # application-level QFileOpenEvent events rather than argv. Handle
        # them so double-clicking a PDF opens it in the running app.
        try:
            from PySide6.QtCore import QEvent

            if isinstance(event, QEvent) and event.type() == QEvent.Type.FileOpen:
                path = getattr(event, "file", lambda: "")()
                if path:
                    self.open_path(Path(path))
                return True
        except Exception:
            logger.exception("failed to handle file-open event")
        return super().eventFilter(watched, event)

    def open_dialog(self) -> None:
        start = self.settings.get_str("last_dir", "")
        patterns = "PDF files (*.pdf);;All files (*)"
        path, _ = QFileDialog.getOpenFileName(self, "Open PDF", start, patterns)
        if not path:
            return
        self.settings.set("last_dir", str(Path(path).parent))
        self.settings.save()
        self.open_path(Path(path))

    def open_path(self, path: Path, *, restore_recovery: bool | None = None) -> bool:
        """Open ``path``; returns True on success.

        Handles password retries, friendly corruption errors and the
        recovery-snapshot prompt.  ``restore_recovery`` answers the prompt
        programmatically (tests).
        """
        if not self._confirm_unsaved():
            return False
        password = ""
        tries = 0
        session: PdfSession | None = None
        while True:
            try:
                session = PdfSession.open(path, password=password)
                break
            except PasswordRequiredError:
                tries += 1
                if tries > _MAX_PASSWORD_TRIES:
                    QMessageBox.warning(
                        self, APP_NAME, "Too many incorrect passwords. The file was not opened."
                    )
                    return False
                entered, ok = QInputDialog.getText(
                    self,
                    "Password required",
                    f"“{path.name}” is password protected:",
                    echo=QLineEdit.EchoMode.Password,
                )
                if not ok:
                    return False
                password = entered
            except CorruptDocumentError as exc:
                QMessageBox.critical(self, APP_NAME, f"Could not open {path.name}.\n\n{exc}")
                return False
            except DocumentError as exc:
                QMessageBox.warning(self, APP_NAME, str(exc))
                return False

        assert session is not None

        # Offer a crash-recovery snapshot when one exists and is newer.
        store = RecoveryStore()
        if restore_recovery is None:
            meta = store.meta(path)
            snapshot_newer = False
            if meta is not None:
                try:
                    saved_at = meta.get("saved_at", 0)
                    snapshot_newer = (
                        isinstance(saved_at, (int, float))
                        and float(saved_at) > path.stat().st_mtime
                    )
                except (OSError, TypeError, ValueError):
                    snapshot_newer = False
            if snapshot_newer:
                answer = QMessageBox.question(
                    self,
                    "Recover document",
                    f"A newer autosave of {path.name} was found.\nRestore it?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.Yes,
                )
                restore_recovery = answer == QMessageBox.StandardButton.Yes

        if restore_recovery:
            data = store.read(path)
            if data:
                try:
                    recovered = PdfSession.open_bytes(data, source_path=path)
                    session.close()
                    session = recovered
                    self._set_status(f"Restored autosave of {path.name}")
                except DocumentError:
                    logger.warning("recovery snapshot unusable, keeping original")

        self._attach_session(session)
        self._set_status(f"Opened {path.name}")
        return True

    def _attach_session(self, session: PdfSession) -> None:
        self._detach_session()
        self._session = session
        self._viewer.set_session(session)
        self._thumbs.set_session(session)
        self._controller.set_session(session)
        self._edit_controller.set_session(session)
        session.images.options = self._image_export_options()
        session.keep_ocr_layer = self.settings.get_bool("ocr_searchable_layer", True)
        self._image_controller.set_session(session)
        self._make_editable.set_session(session)
        self._ocr_review.set_session(session)
        self._start_scan()
        session.history.add_listener(self._on_history_changed)
        pages = session.page_count
        self._page_spin.setMaximum(max(1, pages))
        self._page_label.setText(f"of {pages}")
        self._set_title()
        self._update_actions_enabled()
        self._update_page_widgets(self._viewer.current_page())
        self._update_zoom_widgets(self._viewer.zoom)
        logger.info("attached %s", session.display_name)

    def _detach_session(self) -> None:
        self._scan_timer.stop()
        self._make_editable.set_session(None)
        self._ocr_review.set_session(None)
        self._pending_edit = None
        self._ocr_bar.hide()
        if self._session is not None:
            self._session.close()
            self._session = None
        self._viewer.set_session(None)
        self._thumbs.set_session(None)
        self._controller.set_session(None)
        self._edit_controller.set_session(None)
        self._image_controller.set_session(None)

    def _confirm_unsaved(self) -> bool:
        """Offer Save/Discard/Cancel for unsaved changes; False means Cancel."""
        if self._session is None or not self._session.is_dirty:
            return True
        answer = QMessageBox.question(
            self,
            APP_NAME,
            f"Save changes to {self._session.display_name}?",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )
        if answer == QMessageBox.StandardButton.Cancel:
            return False
        if answer == QMessageBox.StandardButton.Save:
            return self.save()
        self._session.clear_recovery()
        return True

    def close_document(self) -> bool:
        """Close the current document after handling unsaved changes."""
        if self._session is None:
            return True
        if not self._confirm_unsaved():
            return False
        self._detach_session()
        self._set_title()
        self._update_actions_enabled()
        self._set_status("")
        return True

    def save(self) -> bool:
        """Save the document to its current path (explicit user action)."""
        if self._session is None:
            return False
        if self._session.path is None:
            return self.save_as()
        try:
            self._session.save()
        except DocumentError as exc:
            QMessageBox.critical(self, APP_NAME, str(exc))
            return False
        self._set_title()
        self._set_status(f"Saved {self._session.display_name}")
        return True

    def save_as(self) -> bool:
        if self._session is None:
            return False
        start = str(self._session.path.parent) if self._session.path else ""
        suggested = self._session.display_name
        path, _ = QFileDialog.getSaveFileName(
            self, "Save As", str(Path(start) / suggested), "PDF files (*.pdf)"
        )
        if not path:
            return False
        if not path.lower().endswith(".pdf"):
            path += ".pdf"
        try:
            self._session.save_as(path, overwrite=True)
        except DocumentError as exc:
            QMessageBox.critical(self, APP_NAME, str(exc))
            return False
        self._set_title()
        self._set_status(f"Saved {self._session.display_name}")
        return True

    # --- images -----------------------------------------------------------

    def add_image_dialog(self) -> None:
        """Insert ▸ Add Image: pick a file, then the page (default: visible)."""
        if self._session is None:
            return
        start = self.settings.get_str("last_image_dir", self.settings.get_str("last_dir", ""))
        patterns = "Images (*.png *.jpg *.jpeg *.webp);;All files (*)"
        path, _ = QFileDialog.getOpenFileName(self, "Add Image", start, patterns)
        if not path:
            return
        self.settings.set("last_image_dir", str(Path(path).parent))
        self.settings.save()
        page = self._viewer.current_page()
        if self._session.page_count > 1:
            number, ok = QInputDialog.getInt(
                self,
                "Add Image",
                "Insert on page:",
                page + 1,
                1,
                self._session.page_count,
            )
            if not ok:
                return
            page = number - 1
        self._image_controller.insert_file(Path(path), page=page)

    def _paste(self) -> None:
        if self._session is not None and not self._image_controller.paste():
            self._set_status("Nothing to paste (copy an image first)", timeout_ms=3000)

    def _on_image_selection(self, item: object) -> None:
        if item is not None and not self._image_dock.isHidden():
            self._image_dock.raise_()
        self._update_actions_enabled()

    def _image_export_options(self) -> ExportOptions:
        return ExportOptions(
            max_px=max(0, self.settings.get_int("image_max_px", 3000)),
            jpeg_quality=min(100, max(10, self.settings.get_int("image_jpeg_quality", 90))),
            png_lossless=self.settings.get_bool("image_png_lossless", True),
        )

    def _on_image_export_options(self, options: object) -> None:
        if not isinstance(options, ExportOptions):
            return
        self.settings.set("image_max_px", options.max_px)
        self.settings.set("image_jpeg_quality", options.jpeg_quality)
        self.settings.set("image_png_lossless", options.png_lossless)
        self.settings.save()
        if self._session is not None:
            self._session.images.options = options
            if self._session.images:
                self._session.mark_dirty()
                self._set_title()

    # --- autosave ---------------------------------------------------------

    def _autosave_tick(self) -> None:
        if (
            self._session is not None
            and self._session.is_dirty
            and self._session.write_recovery()
        ):
            self._set_status("Autosaved recovery snapshot", timeout_ms=4000)

    # --- viewer plumbing --------------------------------------------------

    def _goto_page(self, index: int) -> None:
        self._viewer.scroll_to_page(index)

    def _on_page_spin(self, value: int) -> None:
        if not self._updating_ui:
            self._viewer.scroll_to_page(value - 1)

    def _on_visible_page(self, index: int) -> None:
        self._update_page_widgets(index)

    def _update_page_widgets(self, index: int) -> None:
        count = self._viewer.page_count()
        self._updating_ui = True
        try:
            self._page_spin.blockSignals(True)
            self._page_spin.setValue(index + 1)
            self._page_spin.setMaximum(max(1, count))
            self._page_spin.blockSignals(False)
        finally:
            self._updating_ui = False
        self._page_label.setText(f"of {count}")
        if count:
            self._status_page.setText(f"Page {index + 1} / {count}   ")
        else:
            self._status_page.setText("No document   ")

    def _on_zoom_changed(self, zoom: float) -> None:
        self._update_zoom_widgets(zoom)

    def _update_zoom_widgets(self, zoom: float) -> None:
        text = f"{round(zoom * 100)}%"
        self._zoom_label.setText(text)
        self._status_zoom.setText(text + "   ")

    def _on_dark_toggled(self, dark: bool) -> None:
        app = QApplication.instance()
        if app is not None:
            apply_theme(app, dark)  # type: ignore[arg-type]
        self._viewer.canvas.update()
        self._thumbs.update()
        self.settings.set("theme", "dark" if dark else "light")
        self.settings.save()

    # --- inspect / edit mode ---------------------------------------------

    def _on_inspect_status(self, message: str) -> None:
        self._status_message.setText(message)

    def _on_edit_requested(self, hit: object) -> None:
        """Double-click in Edit mode: open the inline editor (M3)."""
        if self._mode != MODE_EDIT:
            return
        if not self._editor_controller_open(hit):
            self._set_status("This text cannot be edited here.")

    def _editor_controller_open(self, hit: object) -> bool:
        """Open the inline editor over ``hit``; True when it opened."""
        return self._edit_controller.open(hit)

    # --- OCR (M6) -------------------------------------------------------

    def _run_ocr_on_current_page(self) -> None:
        if self._session is None or self._ocr_controller.busy:
            return
        page_index = self._viewer.current_page()
        self.act_ocr_page.setEnabled(False)
        self._set_status(f"Recognizing text on page {page_index + 1}…")
        if not self._ocr_controller.run(self._session, page_index, "en"):
            self.act_ocr_page.setEnabled(True)

    def _on_ocr_finished(self, words: list[object]) -> None:
        self.act_ocr_page.setEnabled(self._session is not None)
        recognized: list[OcrWord] = [
            word for word in words if isinstance(word, OcrWord)
        ]
        if not recognized:
            self._set_status("OCR found no text on this page.")
            return
        page_index = self._viewer.current_page() + 1
        preview = line_ordered(recognized)
        text = preview[:80] + ("…" if len(preview) > 80 else "")
        self._set_status(f"OCR page {page_index}: {text}")

    def _on_ocr_failed(self, message: str) -> None:
        self.act_ocr_page.setEnabled(self._session is not None)
        QMessageBox.warning(self, APP_NAME, f"OCR failed.\n\n{message}")

    # --- Auto-Make Editable (scanned pages) -------------------------------

    def _apply_ocr_settings(self) -> None:
        self._make_editable.options = ocr_options_from(self.settings)
        raw = self.settings.get("ocr_review_threshold", 0.70)
        self._ocr_review.threshold = float(raw) if isinstance(raw, (int, float)) else 0.70
        self._ocr_review.invalidate()
        if self._session is not None:
            self._session.keep_ocr_layer = self.settings.get_bool("ocr_searchable_layer", True)

    def _page_kind(self, page: int) -> str:
        if self._session is None or not 0 <= page < self._session.page_count:
            return "blank"
        try:
            return classify_page(self._session.mupdf.load_page(page)).kind
        except Exception:
            logger.exception("page classification failed")
            return "text"

    def _start_scan(self) -> None:
        """Classify pages a few at a time on idle (500-page files stay responsive)."""
        self._scan_next = 0
        self._scanned_pages = []
        self._ocr_failures = []
        self._ocr_bar.hide()
        self._scan_timer.start()

    def _scan_tick(self) -> None:
        session = self._session
        if session is None:
            self._scan_timer.stop()
            return
        stop = min(session.page_count, self._scan_next + 8)
        for page in range(self._scan_next, stop):
            if self._page_kind(page) == "scanned":
                self._scanned_pages.append(page)
        self._scan_next = stop
        if stop < session.page_count:
            return
        self._scan_timer.stop()
        if not self._scanned_pages:
            return
        count = len(self._scanned_pages)
        logger.info("%d scanned page(s) detected", count)
        dpis = [
            d for d in (scan_dpi(session.mupdf.load_page(p)) for p in self._scanned_pages) if d > 0
        ]
        lowest = min(dpis, default=0.0)
        self._low_res_note = (
            f" Low-resolution scan (about {lowest:.0f} DPI): some text may be misread."
            if 0 < lowest < _LOW_RES_DPI
            else ""
        )
        if self.settings.get_bool("ocr_auto", True):
            self._make_all_editable()
        else:
            self._show_ocr_bar(
                f"This PDF contains scanned text on {count} page(s). "
                "Make it editable to change the text.",
                running=False,
            )

    def _show_ocr_bar(self, message: str, *, running: bool) -> None:
        self._ocr_label.setText(message + self._low_res_note)
        self._ocr_progress.setVisible(running)
        self._ocr_cancel_btn.setVisible(running)
        self._ocr_start_btn.setVisible(not running and bool(self._remaining_scanned()))
        self._ocr_close_btn.setVisible(not running)
        self._ocr_bar.show()

    def _remaining_scanned(self) -> list[int]:
        return [p for p in self._scanned_pages if self._page_kind(p) == "scanned"]

    def _make_all_editable(self) -> None:
        pages = self._remaining_scanned()
        if self._session is None or not pages:
            return
        current = self._viewer.current_page()
        pages.sort(key=lambda p: (abs(p - current), p))
        self._make_editable.enqueue(pages, force=True)
        self._show_ocr_bar(
            "This PDF contains scanned text. Making it editable…", running=True
        )

    def _make_current_page_editable(self) -> None:
        if self._session is None:
            return
        page = self._viewer.current_page()
        kind = self._page_kind(page)
        if kind == "ocr":
            answer = QMessageBox.question(
                self,
                APP_NAME,
                f"Page {page + 1} is already editable. Recognise its text again?\n\n"
                "Recognised lines you have not edited are replaced; your edits are kept.",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            self._rerecognise.add(page)
        if kind == "text":
            answer = QMessageBox.question(
                self,
                APP_NAME,
                f"Page {page + 1} already has real text. Recognise the remaining "
                "image text on it anyway?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        if page not in self._scanned_pages:
            self._scanned_pages.append(page)
        self._make_editable.enqueue([page], front=True, force=True)
        self._show_ocr_bar(f"Making page {page + 1} editable…", running=True)

    def _auto_ocr_visible_page(self) -> None:
        """Entering Edit mode on a scan recognises it at once (like Acrobat)."""
        if self._session is None:
            return
        page = self._viewer.current_page()
        if self._page_kind(page) != "scanned" or self._make_editable.was_requested(page):
            return
        if page not in self._scanned_pages:
            self._scanned_pages.append(page)
        self._make_editable.enqueue([page], front=True)
        self._show_ocr_bar(f"Making page {page + 1} editable…", running=True)

    def _revert_current_page(self) -> None:
        if self._session is None:
            return
        page = self._viewer.current_page()
        if self._page_kind(page) != "ocr":
            self._set_status(
                f"Page {page + 1} has no recognised text to remove.", timeout_ms=4000
            )
            return
        answer = QMessageBox.question(
            self,
            APP_NAME,
            f"Revert page {page + 1} to the original scan?\n\n"
            "Recognised text and every text edit on this page are removed "
            "(Undo brings them back).",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._edit_controller.close_editor()
        removed = revert_page(self._session, page)
        self._make_editable.mark_handled(page)  # don't immediately re-recognise
        self._set_status(f"Page {page + 1} reverted to the original scan ({removed} line(s)).")

    def _make_document_searchable(self) -> None:
        """Invisible text on every scanned page; nothing visible changes."""
        session = self._session
        if session is None:
            return
        session.keep_ocr_layer = True
        pages = [p for p in range(session.page_count) if self._page_kind(p) == "scanned"]
        if not pages:
            self._set_status("Every page already has searchable text.", timeout_ms=4000)
            return
        for page in pages:
            if page not in self._scanned_pages:
                self._scanned_pages.append(page)
        current = self._viewer.current_page()
        pages.sort(key=lambda p: (abs(p - current), p))
        self._make_editable.enqueue(pages, force=True)
        self._show_ocr_bar(
            f"Making {len(pages)} scanned page(s) searchable… (save to keep it)", running=True
        )

    def _cancel_ocr(self) -> None:
        self._make_editable.cancel()
        self._ocr_label.setText("Stopping after the current page…")

    def _on_canvas_click(self, hit: object, double_click: bool) -> None:
        """Double-click on scanned text in Edit mode: recognise that page first."""
        if (
            not double_click
            or self._mode != MODE_EDIT
            or self._session is None
            or not isinstance(hit, tuple)
        ):
            return
        page, x, y = int(hit[0]), float(hit[1]), float(hit[2])
        try:
            if self._controller.hit_at(page, x, y) is not None:
                return  # real (or already recognised) text: the normal editor opens
        except Exception:
            logger.exception("hit test failed")
        kind = self._page_kind(page)
        over_image = self._point_over_image(page, x, y)
        if kind == "ocr":
            self._set_status(
                "No recognised text at this spot - the OCR may have missed it."
                if over_image
                else "There is no text here to edit.",
                timeout_ms=6000,
            )
            return
        if kind != "scanned" and not over_image:
            self._set_status("There is no text here to edit.", timeout_ms=6000)
            return
        # A scan, or a picture on a normal page: recognise the page, then
        # open the editor at the clicked spot (see _on_ocr_page_ready).
        self._pending_edit = (page, x, y)
        if page not in self._scanned_pages:
            self._scanned_pages.append(page)
        self._make_editable.enqueue([page], front=True, force=True)
        self._show_ocr_bar(f"Recognising the text on page {page + 1}…", running=True)
        self._set_status(
            f"Page {page + 1} is a scan - recognising its text so you can edit it…"
        )

    def _point_over_image(self, page: int, x: float, y: float) -> bool:
        """True when view point ``(x, y)`` lies on an embedded image."""
        if self._session is None:
            return False
        try:
            mupdf_page = self._session.mupdf.load_page(page)
            point = pymupdf.Point(x, y) * mupdf_page.derotation_matrix
            return any(
                pymupdf.Rect(info["bbox"]).contains(point)
                for info in mupdf_page.get_image_info()
            )
        except Exception:
            return False

    def _on_ocr_page_started(self, page: int) -> None:
        self._ocr_label.setText(f"Making page {page + 1} editable…")

    def _on_ocr_progress(self, done: int, total: int) -> None:
        self._ocr_progress.setMaximum(max(1, total))
        self._ocr_progress.setValue(done)
        self._ocr_progress.setFormat(f"{done} / {total} pages")

    def _on_ocr_page_ready(self, page: int, lines: list[object]) -> None:
        session = self._session
        if session is None or not 0 <= page < session.page_count:
            return
        rerun = page in self._rerecognise
        self._rerecognise.discard(page)
        if self._page_kind(page) == "ocr" and not rerun:
            return  # already handled (e.g. requested twice)
        ocr_lines = [line for line in lines if isinstance(line, OcrLine)]
        if not ocr_lines:
            self._set_status(f"No text was found on page {page + 1}.", timeout_ms=5000)
            return
        try:
            if rerun:
                written = rerecognise_page(session, page, ocr_lines)
            else:
                written = make_page_editable(session, page, ocr_lines)
        except Exception as exc:
            logger.exception("could not write the OCR layer")
            self._on_ocr_page_failed(page, str(exc))
            return
        uncertain = len(self._ocr_review.uncertain_quads(page))
        message = f"Page {page + 1}: {written} line(s) recognised"
        if uncertain:
            message += f", {uncertain} highlighted for review"
        self._set_status(message)
        pending = self._pending_edit
        if pending is not None and pending[0] == page:
            self._pending_edit = None
            found = self._controller.hit_at(*pending)
            if found is not None and self._mode == MODE_EDIT:
                self._edit_controller.open(found)

    def _on_ocr_page_failed(self, page: int, message: str) -> None:
        where = f"page {page + 1}" if page >= 0 else "the document"
        self._ocr_failures.append(f"OCR failed on {where}: {message}")
        self._set_status(self._ocr_failures[-1])

    def _on_ocr_finished_all(self, cancelled: bool) -> None:
        remaining = self._remaining_scanned()
        if cancelled:
            text = f"Stopped. {len(remaining)} scanned page(s) are not editable yet."
        elif self._ocr_failures:
            text = self._ocr_failures[-1] + " Other pages are editable."
        else:
            text = (
                "Scanned text is now editable: switch to Edit mode and double-click a line."
                if self._scanned_pages
                else "Done."
            )
        self._ocr_failures = []
        self._show_ocr_bar(text, running=False)

    def _on_ocr_review_toggled(self, enabled: bool) -> None:
        self._ocr_review.enabled = enabled
        self._viewer.canvas.update()

    def _show_ocr_settings(self) -> None:
        dialog = OcrSettingsDialog(self.settings, self)
        if dialog.exec() == OcrSettingsDialog.DialogCode.Accepted:
            self._apply_ocr_settings()
            self._set_status("OCR settings saved", timeout_ms=3000)
            if self._session is not None and self.settings.get_bool("ocr_auto", True):
                self._make_all_editable()

    # --- undo / redo ------------------------------------------------------

    def _undo(self) -> None:
        if self._session is None:
            return
        history = self._session.history
        try:
            did = history.undo()
        except DocumentError as exc:
            QMessageBox.warning(self, APP_NAME, str(exc))
            return
        if not did:
            self._set_status("Nothing to undo")
            return
        description = history.redo_description
        self._set_status(f"Undid {description}" if description else "Undid edit")

    def _redo(self) -> None:
        if self._session is None:
            return
        history = self._session.history
        try:
            did = history.redo()
        except DocumentError as exc:
            QMessageBox.warning(self, APP_NAME, str(exc))
            return
        if not did:
            self._set_status("Nothing to redo")
            return
        description = history.undo_description
        self._set_status(f"Redid {description}" if description else "Redid edit")

    def _on_history_changed(self, session: PdfSession) -> None:
        history = session.history
        self.act_undo.setEnabled(history.can_undo)
        self.act_redo.setEnabled(history.can_redo)
        undo_text = f"Undo {history.undo_description}" if history.undo_description else "Undo"
        redo_text = f"Redo {history.redo_description}" if history.redo_description else "Redo"
        self.act_undo.setText(undo_text)
        self.act_redo.setText(redo_text)
        self._set_title()
        self._update_actions_enabled()
        last = history.last_command
        if last is not None and not last.mutates_document:
            # Image-layer change: the PDF pages are untouched, only repaint.
            self._image_controller.on_history_changed()
            self._viewer.canvas.update()
            self._thumbs.refresh()
            return
        self._refresh_after_mutation()
        self._image_controller.on_history_changed()

    def _refresh_after_mutation(self) -> None:
        self._viewer.refresh()
        self._thumbs.refresh()
        self._controller.invalidate()
        self._ocr_review.invalidate()

    # --- misc handlers ----------------------------------------------------

    def _set_title(self) -> None:
        name = self._session.display_name if self._session else ""
        dirty = "*" if (self._session and self._session.is_dirty) else ""
        if name:
            self.setWindowTitle(f"{name}{dirty} - {APP_NAME}")
        else:
            self.setWindowTitle(APP_NAME)

    def _set_status(self, message: str, timeout_ms: int = 0) -> None:
        self._status_message.setText(message)
        if timeout_ms:
            QTimer.singleShot(timeout_ms, lambda: self._status_message.setText(""))

    def _update_actions_enabled(self) -> None:
        has = self._session is not None
        for action in (
            self.act_save,
            self.act_save_as,
            self.act_close_doc,
            self.act_next_page,
            self.act_prev_page,
            self.act_fit_width,
            self.act_ocr_page,
            self.act_add_image,
            self.act_paste,
            self.act_make_editable,
            self.act_make_searchable,
            self.act_revert_scan,
        ):
            action.setEnabled(has)
        has_image = has and self._image_controller.selected is not None
        for action in (self.act_cut, self.act_copy, self.act_duplicate, self.act_delete_image):
            action.setEnabled(has_image)
        history = self._session.history if self._session is not None else None
        self.act_undo.setEnabled(history is not None and history.can_undo)
        self.act_redo.setEnabled(history is not None and history.can_redo)

    def _show_about(self) -> None:
        QMessageBox.about(
            self,
            f"About {APP_NAME}",
            f"<b>{APP_NAME} {__version__}</b><br>"
            f"{version_banner()}<br><br>"
            "A fully offline PDF editor. Documents never leave this machine.<br>"
            "Developed by Dinesh Kumar.<br>"
            "Third-party licenses: see THIRD_PARTY_LICENSES.md.",
        )

    def _show_help(self) -> None:
        from offlinepdf.config import app_install_dir, resource_path

        candidates = [
            resource_path("assets", "help", "index.html"),
            app_install_dir() / "assets" / "help" / "index.html",
            app_install_dir() / "help" / "index.html",
        ]
        for candidate in candidates:
            if candidate.exists():
                QMessageBox.information(self, "Help", f"Bundled help: {candidate}")
                return
        QMessageBox.information(
            self,
            "Help",
            "Bundled offline help ships with release builds "
            "(assets/help/index.html).",
        )

    # --- drag & drop ------------------------------------------------------

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        mime = event.mimeData()
        if self._session is not None and mime.hasImage():
            event.acceptProposedAction()
            return
        if mime.hasUrls():
            for url in mime.urls():
                path = url.toLocalFile().lower()
                if path.endswith(".pdf") or (
                    self._session is not None and path.endswith(SUPPORTED_EXTENSIONS)
                ):
                    event.acceptProposedAction()
                    return

    def dropEvent(self, event: QDropEvent) -> None:
        mime = event.mimeData()
        images = [
            Path(url.toLocalFile())
            for url in mime.urls()
            if url.toLocalFile().lower().endswith(SUPPORTED_EXTENSIONS)
        ]
        if self._session is not None and (images or mime.hasImage()):
            # Drop onto a page inserts there, centred on the drop point.
            canvas = self._viewer.canvas
            pos = canvas.mapFrom(self, event.position().toPoint())
            hit = canvas.locate(pos.x(), pos.y())
            page = hit[0] if hit is not None else None
            at = (hit[1], hit[2]) if hit is not None else None
            for image_path in images:
                self._image_controller.insert_file(image_path, page=page, at=at)
            if not images:
                self._image_controller.insert_qimage(mime.imageData(), page=page)
            event.acceptProposedAction()
            return
        for url in mime.urls():
            path = url.toLocalFile()
            if path.lower().endswith(".pdf"):
                self.open_path(Path(path))
                event.acceptProposedAction()
                return

    # --- lifecycle --------------------------------------------------------

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._session is not None and not self.close_document():
            event.ignore()
            return
        self._make_editable.cancel()
        self._make_editable.wait(15000)
        TempWorkspace().wipe()
        try:
            self.settings.save()
        except OSError:
            logger.warning("settings save failed on exit")
        event.accept()

    @property
    def session(self) -> PdfSession | None:
        return self._session

    @property
    def viewer(self) -> Viewer:
        return self._viewer

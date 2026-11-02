"""Application themes (light/dark). Chrome only: page content is never tinted.

Both themes use Qt's Fusion style so every widget honours the palette (the
native Windows style ignores parts of it, which made dark mode patchy).  A
theme is a set of colour tokens; :func:`apply_theme` derives the
``QPalette`` and one stylesheet from it.  Widgets that need semantic colours
set a ``role`` property instead of hard-coding them::

    label.setProperty("role", "muted")   # muted | warning | error | notice

and :func:`set_role` re-polishes a widget whose role changes at runtime.
Custom-painted surfaces (the page canvas) read :func:`current` directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QWidget

__all__ = ["DARK", "LIGHT", "Theme", "apply_theme", "build_dark_palette", "current", "set_role"]


@dataclass(frozen=True)
class Theme:
    name: str
    window: str  # app background, toolbars, docks
    surface: str  # cards, group boxes, menus, lists
    surface_alt: str  # alternate rows, headers, tracks
    input: str  # line edits, spin boxes, combo boxes
    border: str
    border_strong: str
    text: str
    muted: str
    disabled: str
    hover: str
    pressed: str
    accent: str
    accent_hover: str
    accent_text: str  # text on accent_soft
    accent_soft: str
    on_accent: str  # text on accent
    canvas: str  # area around the pages
    page_shadow: str
    warning: str
    error: str
    notice_bg: str
    notice_border: str
    tooltip_bg: str
    tooltip_text: str
    scroll: str
    scroll_hover: str


LIGHT = Theme(
    name="light",
    window="#f4f5f7",
    surface="#ffffff",
    surface_alt="#f0f2f5",
    input="#ffffff",
    border="#d9dde3",
    border_strong="#b9c0ca",
    text="#1d2127",
    muted="#646b76",
    disabled="#a4aab3",
    hover="#e8ebef",
    pressed="#dce1e7",
    accent="#2563eb",
    accent_hover="#1d4fd7",
    accent_text="#1846b8",
    accent_soft="#dfe9fd",
    on_accent="#ffffff",
    canvas="#e3e6ea",
    page_shadow="#000000",
    warning="#a85a06",
    error="#c0392b",
    notice_bg="#eaf1fe",
    notice_border="#b9cff8",
    tooltip_bg="#1d2127",
    tooltip_text="#f4f5f7",
    scroll="#c3c8cf",
    scroll_hover="#a7aeb8",
)

DARK = Theme(
    name="dark",
    window="#1e2023",
    surface="#26292d",
    surface_alt="#2d3035",
    input="#1a1c1f",
    border="#3a3e45",
    border_strong="#4d525a",
    text="#e7e9ec",
    muted="#9ba1aa",
    disabled="#62676f",
    hover="#30343a",
    pressed="#383c43",
    accent="#4c8dff",
    accent_hover="#6aa1ff",
    accent_text="#a9c8ff",
    accent_soft="#233656",
    on_accent="#ffffff",
    canvas="#141517",
    page_shadow="#000000",
    warning="#f2b35b",
    error="#ff7a6e",
    notice_bg="#1d2a40",
    notice_border="#2f4870",
    tooltip_bg="#e7e9ec",
    tooltip_text="#1d2127",
    scroll="#474c54",
    scroll_hover="#5d636c",
)

_current: Theme = LIGHT


def current() -> Theme:
    """The theme applied last (for custom-painted widgets)."""
    return _current


def _palette(t: Theme) -> QPalette:
    pal = QPalette()
    c = {name: QColor(getattr(t, name)) for name in Theme.__dataclass_fields__ if name != "name"}
    role = QPalette.ColorRole
    group = QPalette.ColorGroup
    for grp in (group.Active, group.Inactive, group.Disabled):
        pal.setColor(grp, role.Window, c["window"])
        pal.setColor(grp, role.WindowText, c["text"])
        pal.setColor(grp, role.Base, c["input"])
        pal.setColor(grp, role.AlternateBase, c["surface_alt"])
        pal.setColor(grp, role.Text, c["text"])
        pal.setColor(grp, role.Button, c["surface"])
        pal.setColor(grp, role.ButtonText, c["text"])
        pal.setColor(grp, role.BrightText, c["error"])
        pal.setColor(grp, role.Highlight, c["accent"])
        pal.setColor(grp, role.HighlightedText, c["on_accent"])
        pal.setColor(grp, role.Link, c["accent"])
        pal.setColor(grp, role.LinkVisited, c["accent_hover"])
        pal.setColor(grp, role.ToolTipBase, c["tooltip_bg"])
        pal.setColor(grp, role.ToolTipText, c["tooltip_text"])
        pal.setColor(grp, role.PlaceholderText, c["muted"])
        pal.setColor(grp, role.Light, c["surface"])
        pal.setColor(grp, role.Midlight, c["hover"])
        pal.setColor(grp, role.Mid, c["border"])
        pal.setColor(grp, role.Dark, c["border_strong"])
        pal.setColor(grp, role.Shadow, QColor(0, 0, 0))
        if hasattr(role, "Accent"):  # Qt >= 6.6
            pal.setColor(grp, role.Accent, c["accent"])
    for r in (role.WindowText, role.Text, role.ButtonText):
        pal.setColor(group.Disabled, r, c["disabled"])
    pal.setColor(group.Disabled, role.Highlight, c["border_strong"])
    pal.setColor(group.Inactive, role.Highlight, c["accent"])
    return pal


def build_dark_palette() -> QPalette:
    """Dark palette for the application chrome (kept for compatibility)."""
    return _palette(DARK)


_QSS = """
QMainWindow, QDialog {{ background: {window}; }}
QWidget {{ color: {text}; }}
QWidget#pageStrip {{ background: {window}; border-bottom: 1px solid {border}; }}

/* toolbar */
QToolBar {{ background: {window}; border: none; border-bottom: 1px solid {border};
           padding: 4px 8px; spacing: 2px; }}
QToolBar::separator {{ width: 1px; background: {border}; margin: 5px 6px; }}
QToolButton {{ background: transparent; color: {text}; border: 1px solid transparent;
              border-radius: 6px; padding: 4px 9px; }}
QToolButton:hover {{ background: {hover}; }}
QToolButton:pressed {{ background: {pressed}; }}
QToolButton:checked {{ background: {accent_soft}; color: {accent_text};
                      border-color: {accent_soft}; }}
QToolButton:disabled {{ color: {disabled}; }}

/* menus */
QMenuBar {{ background: {window}; color: {text}; padding: 2px 4px; }}
QMenuBar::item {{ background: transparent; padding: 4px 10px; border-radius: 5px; }}
QMenuBar::item:selected {{ background: {hover}; }}
QMenuBar::item:pressed {{ background: {pressed}; }}
QMenu {{ background: {surface}; color: {text}; border: 1px solid {border};
        border-radius: 8px; padding: 5px; }}
QMenu::item {{ padding: 6px 30px 6px 14px; border-radius: 5px; }}
QMenu::item:selected {{ background: {accent}; color: {on_accent}; }}
QMenu::item:disabled {{ color: {disabled}; background: transparent; }}
QMenu::separator {{ height: 1px; background: {border}; margin: 5px 8px; }}

/* docks + tabs */
QDockWidget {{ color: {text}; }}
QDockWidget::title {{ background: {window}; padding: 7px 10px; text-align: left;
                     border-bottom: 1px solid {border}; }}
QMainWindow::separator {{ background: {border}; width: 1px; height: 1px; }}
QTabBar {{ background: transparent; }}
QTabBar::tab {{ background: transparent; color: {muted}; padding: 6px 14px; border: none;
               border-bottom: 2px solid transparent; }}
QTabBar::tab:hover {{ color: {text}; }}
QTabBar::tab:selected {{ color: {text}; border-bottom: 2px solid {accent}; }}
QTabWidget::pane {{ border: 1px solid {border}; border-radius: 8px; background: {surface};
                   top: -1px; }}

/* containers */
QGroupBox {{ background: {surface}; border: 1px solid {border}; border-radius: 8px;
            margin-top: 16px; padding: 10px 8px 8px 8px; }}
QGroupBox::title {{ subcontrol-origin: margin; subcontrol-position: top left; left: 10px;
                   padding: 0 4px; color: {muted}; }}
QScrollArea {{ background: transparent; border: none; }}
QScrollArea#pdfViewer, QScrollArea#pdfViewer > QWidget#qt_scrollarea_viewport {{
    background: {canvas}; }}
QFrame#ocrNotice {{ background: {notice_bg}; border: 1px solid {notice_border};
                   border-radius: 8px; margin: 6px 12px 2px 12px; }}
QFrame#ocrNotice QLabel {{ background: transparent; color: {text}; }}

/* buttons */
QPushButton {{ background: {surface}; color: {text}; border: 1px solid {border};
              border-radius: 6px; padding: 5px 12px; min-height: 18px; }}
QPushButton:hover {{ background: {hover}; border-color: {border_strong}; }}
QPushButton:pressed {{ background: {pressed}; }}
QPushButton:default, QPushButton[primary="true"] {{ background: {accent}; color: {on_accent};
                                                    border-color: {accent}; }}
QPushButton:default:hover, QPushButton[primary="true"]:hover {{ background: {accent_hover}; }}
QPushButton:disabled {{ color: {disabled}; background: {surface_alt}; border-color: {border}; }}

/* inputs */
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit, QTextEdit {{
    background: {input}; color: {text}; border: 1px solid {border}; border-radius: 6px;
    padding: 3px 6px; selection-background-color: {accent}; selection-color: {on_accent}; }}
QLineEdit:hover, QSpinBox:hover, QDoubleSpinBox:hover, QComboBox:hover {{
    border-color: {border_strong}; }}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus,
QPlainTextEdit:focus, QTextEdit:focus {{ border-color: {accent}; }}
QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled, QComboBox:disabled {{
    color: {disabled}; background: {surface_alt}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox::down-arrow {{ image: url({icon_down}); width: 10px; height: 10px; }}
QSpinBox, QDoubleSpinBox {{ padding-right: 20px; }}
QSpinBox::up-button, QDoubleSpinBox::up-button {{ subcontrol-origin: border;
    subcontrol-position: top right; width: 18px; border: none; background: transparent;
    border-top-right-radius: 6px; }}
QSpinBox::down-button, QDoubleSpinBox::down-button {{ subcontrol-origin: border;
    subcontrol-position: bottom right; width: 18px; border: none; background: transparent;
    border-bottom-right-radius: 6px; }}
QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {{ background: {hover}; }}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{ image: url({icon_up}); width: 9px; height: 9px; }}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{ image: url({icon_down}); width: 9px;
    height: 9px; }}
QSpinBox::up-arrow:disabled, QDoubleSpinBox::up-arrow:disabled,
QSpinBox::down-arrow:disabled, QDoubleSpinBox::down-arrow:disabled {{ image: none; }}
QComboBox QAbstractItemView {{ background: {surface}; color: {text}; border: 1px solid {border};
                              selection-background-color: {accent};
                              selection-color: {on_accent}; outline: none; }}
QCheckBox, QRadioButton {{ spacing: 7px; background: transparent; }}
QCheckBox::indicator {{ width: 15px; height: 15px; border: 1px solid {border_strong};
    border-radius: 4px; background: {input}; }}
QCheckBox::indicator:hover {{ border-color: {accent}; }}
QCheckBox::indicator:checked {{ background: {accent}; border-color: {accent};
    image: url({icon_check}); }}
QCheckBox::indicator:disabled {{ background: {surface_alt}; border-color: {border}; }}
QMenu::indicator {{ width: 14px; height: 14px; left: 6px; }}
QMenu::indicator:checked {{ image: url({icon_check_menu}); }}

/* lists + tables */
QListView, QTreeView, QTableView {{ background: {surface};
    alternate-background-color: {surface_alt}; color: {text}; border: 1px solid {border};
    border-radius: 6px; gridline-color: {border};
    selection-background-color: {accent_soft}; selection-color: {accent_text}; outline: none; }}
QListView#thumbnails {{ background: {canvas}; border: none; border-radius: 0; }}
QListView#thumbnails::item {{ color: {muted}; border-radius: 6px; padding: 4px; }}
QListView#thumbnails::item:hover {{ background: {hover}; }}
QListView#thumbnails::item:selected {{ background: {accent_soft}; color: {accent_text}; }}
QHeaderView::section {{ background: {surface_alt}; color: {muted}; border: none;
                       border-bottom: 1px solid {border}; padding: 5px 6px; }}
QTableCornerButton::section {{ background: {surface_alt}; border: none; }}

/* sliders + progress */
QSlider::groove:horizontal {{ height: 4px; background: {border}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {accent}; border-radius: 2px; }}
QSlider::handle:horizontal {{ background: {surface}; border: 2px solid {accent};
                             width: 12px; height: 12px; margin: -6px 0; border-radius: 8px; }}
QProgressBar {{ background: {surface_alt}; color: {text}; border: 1px solid {border};
               border-radius: 6px; text-align: center; min-height: 14px; }}
QProgressBar::chunk {{ background: {accent}; border-radius: 5px; }}

/* scrollbars */
QScrollBar:vertical {{ background: transparent; width: 11px; margin: 2px; }}
QScrollBar:horizontal {{ background: transparent; height: 11px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {scroll}; border-radius: 3px; min-height: 30px; }}
QScrollBar::handle:horizontal {{ background: {scroll}; border-radius: 3px; min-width: 30px; }}
QScrollBar::handle:hover {{ background: {scroll_hover}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; border: none; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}

/* status bar + tooltips */
QStatusBar {{ background: {window}; color: {muted}; border-top: 1px solid {border}; }}
QStatusBar::item {{ border: none; }}
QStatusBar QLabel {{ color: {muted}; padding: 0 4px; }}
QStatusBar QLabel#statusMessage {{ color: {text}; }}
QToolTip {{ background: {tooltip_bg}; color: {tooltip_text}; border: none;
           border-radius: 5px; padding: 5px 8px; }}

/* semantic label roles */
QLabel[role="muted"] {{ color: {muted}; }}
QLabel[role="warning"] {{ color: {warning}; }}
QLabel[role="error"] {{ color: {error}; background: transparent; }}
QLabel[role="notice"] {{ background: {notice_bg}; color: {text}; border: 1px solid {notice_border};
                        border-radius: 6px; padding: 6px 8px; }}
"""


# Glyphs as polylines in a 12x12 box, drawn to PNG (QSS needs image files).
_GLYPHS = {
    "icon_up": ((2.5, 7.8), (6.0, 4.3), (9.5, 7.8)),
    "icon_down": ((2.5, 4.3), (6.0, 7.8), (9.5, 4.3)),
    "icon_check": ((2.6, 6.4), (5.0, 8.8), (9.6, 3.4)),
    "icon_check_menu": ((2.6, 6.4), (5.0, 8.8), (9.6, 3.4)),
}


def _draw_glyph(points: tuple[tuple[float, float], ...], color: str, path: Path) -> None:
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QImage, QPainter, QPen

    scale = 4  # crisp on high-DPI screens; QSS sizes the image down
    image = QImage(12 * scale, 12 * scale, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor(color), 1.7 * scale)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.drawPolyline([QPointF(x * scale, y * scale) for x, y in points])
    painter.end()
    # No format argument: PySide6 6.11 rejects ``save(str, b"PNG")``; Qt infers PNG from the suffix.
    try:
        saved = image.save(str(path))
    except (TypeError, ValueError) as exc:
        raise OSError(f"could not save theme icon {path}: {exc}") from exc
    if not saved:
        raise OSError(f"could not save theme icon {path}")


def _icons(t: Theme) -> dict[str, str]:
    """Theme-coloured arrow/checkmark images for the stylesheet."""
    from offlinepdf.config import ensure_app_dirs

    colors = {
        "icon_up": t.muted,
        "icon_down": t.muted,
        "icon_check": t.on_accent,
        "icon_check_menu": t.text,
    }
    paths: dict[str, str] = {}
    try:
        folder = Path(ensure_app_dirs()["cache"]) / "theme"
        folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        return dict.fromkeys(_GLYPHS, "")
    for key, points in _GLYPHS.items():
        path = folder / f"{key}_{colors[key].lstrip('#')}.png"
        try:
            if not path.exists():
                _draw_glyph(points, colors[key], path)
            paths[key] = path.as_posix()
        except OSError:
            paths[key] = ""
    return paths


def stylesheet(t: Theme) -> str:
    fields = {name: getattr(t, name) for name in Theme.__dataclass_fields__}
    fields.update(_icons(t))
    return _QSS.format(**fields)


def apply_theme(app: QApplication, dark: bool) -> None:
    """Switch the whole application to the light or dark theme."""
    global _current
    theme = DARK if dark else LIGHT
    _current = theme
    app.setStyle("Fusion")
    app.setPalette(_palette(theme))
    app.setStyleSheet(stylesheet(theme))
    for widget in app.topLevelWidgets():
        widget.update()


def set_role(widget: QWidget, role: str) -> None:
    """Change a widget's semantic ``role`` and re-apply the stylesheet to it."""
    widget.setProperty("role", role)
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()

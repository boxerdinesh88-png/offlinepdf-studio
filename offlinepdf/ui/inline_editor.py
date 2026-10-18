"""Inline single-line text editor overlay for Edit mode (Milestone M3).

The editor floats over one span inside the page canvas: a line edit that
renders the typed text live with the resolved font, a status badge showing
the font decision (same font / substitute / synthetic + confidence) and, for
approximate matches, a dropdown with the top-3 alternatives and the decision
trace.

Enter commits, Esc cancels.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QKeyEvent, QPalette
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from offlinepdf.core.font_resolver import (
    STRATEGY_REUSE,
    STRATEGY_SYNTHETIC,
    FontDecision,
)
from offlinepdf.core.text_model import TextSpan
from offlinepdf.ui import theme

__all__ = ["InlineEditor"]

_BOLD_FLAG = 16
_ITALIC_FLAG = 2
_STRATEGY_LABEL = {
    STRATEGY_REUSE: "same font",
    STRATEGY_SYNTHETIC: "synthetic",
}


class _LineEdit(QLineEdit):
    """Line edit that reports Escape (Enter already emits returnPressed)."""

    escapePressed = Signal()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.escapePressed.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class InlineEditor(QFrame):
    """Floating editor positioned over one span of the page canvas."""

    committed = Signal(str)
    cancelled = Signal()
    previewRequested = Signal(str)
    infoRequested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("inlineEditor")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self._line = _LineEdit(self)
        self._line.setClearButtonEnabled(False)
        self._badge = QLabel("", self)
        self._badge.setObjectName("inlineBadge")
        self._more = QToolButton(self)
        self._more.setText("▾")
        self._more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._more.setArrowType(Qt.ArrowType.NoArrow)
        self._more.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._more.hide()

        badge_row = QHBoxLayout()
        badge_row.setContentsMargins(4, 0, 4, 3)
        badge_row.setSpacing(4)
        badge_row.addWidget(self._badge)
        badge_row.addWidget(self._more)
        badge_row.addStretch(1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(3, 3, 3, 0)
        layout.setSpacing(0)
        layout.addWidget(self._line)
        layout.addLayout(badge_row)

        self.setStyleSheet(
            "#inlineEditor {"
            " background: palette(base);"
            " border: 1px solid palette(mid);"
            " border-radius: 4px;"
            "}"
            "#inlineBadge {"
            " padding: 0px 6px;"
            " border-radius: 3px;"
            " color: palette(text);"
            " background: palette(alternate-base);"
            "}"
        )

        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(120)
        self._preview_timer.timeout.connect(self._request_preview)

        self._line.returnPressed.connect(lambda: self.committed.emit(self._line.text()))
        self._line.escapePressed.connect(self.cancelled.emit)
        self._line.textChanged.connect(lambda _text: self._preview_timer.start())

    # ------------------------------------------------------------------ #
    # state
    # ------------------------------------------------------------------ #
    @property
    def text(self) -> str:
        return self._line.text()

    def set_text(self, text: str) -> None:
        self._line.setText(text)
        self._line.selectAll()

    def focus_editor(self) -> None:
        self._line.setFocus(Qt.FocusReason.OtherFocusReason)
        self._line.selectAll()

    # ------------------------------------------------------------------ #
    # live preview
    # ------------------------------------------------------------------ #
    def set_preview(self, decision: FontDecision | None, warning: str) -> None:
        """Update badge, dropdown and live font from the resolver preview."""
        if warning:
            self._badge.setText(warning)
            self._badge.setStyleSheet(f"color: {theme.current().error}; background: transparent;")
            self._more.hide()
            return
        if decision is None:
            self._badge.clear()
            self._more.hide()
            return
        label = _STRATEGY_LABEL.get(decision.strategy, "substitute font")
        family = f" · {decision.family}" if decision.family else ""
        if decision.strategy == STRATEGY_REUSE:
            self._badge.setStyleSheet("")
            self._badge.setText(f"{label} · {decision.confidence:.0%}{family}")
        else:
            # Never substitute silently: the saved text will use another face.
            self._badge.setStyleSheet(
                f"color: {theme.current().warning}; background: transparent;"
            )
            self._badge.setText(
                f"⚠ Fallback font{family} replaces "
                f"{decision.requested or 'the original'} · {decision.confidence:.0%}"
            )
        if decision.approximate and (decision.alternatives or decision.trace):
            self._more.show()
            self._more.setMenu(self._build_menu(decision))
        else:
            self._more.hide()

    def apply_style(
        self,
        decision: FontDecision | None,
        span: TextSpan,
        zoom: float,
        *,
        original_family: str | None = None,
        weight: int | None = None,
        italic: bool | None = None,
        stretch: float = 100.0,
        letter_spacing: float = 0.0,
    ) -> None:
        """Render the typed text live with the style the saved PDF will use.

        ``original_family`` is the PDF's own embedded font loaded into Qt; it
        is used while the edit can reuse it, otherwise the fallback family.
        ``weight``/``italic`` come from the font program (MuPDF's flags miss
        them for many subset fonts); ``stretch`` is the horizontal scale (Tz)
        and ``letter_spacing`` the character spacing (Tc) in points.
        """
        font = QFont()
        reuse = decision is None or decision.strategy == STRATEGY_REUSE
        families = [
            name
            for name in (
                original_family if reuse else None,
                decision.family if decision is not None else None,
                original_family,
                span.font_mupdf,
            )
            if name
        ]
        families.append("sans-serif")
        font.setFamilies(list(dict.fromkeys(families)))
        font.setPixelSize(max(6, round(span.size_mupdf * zoom)))
        if weight is not None:
            font.setWeight(QFont.Weight(max(100, min(900, int(weight)))))
        else:
            font.setBold(bool(span.flags & _BOLD_FLAG))
        font.setItalic(bool(span.flags & _ITALIC_FLAG) if italic is None else italic)
        if abs(stretch - 100.0) > 0.5:
            font.setStretch(max(1, min(4000, round(stretch))))
        if letter_spacing:
            font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, letter_spacing * zoom)
        self._line.setFont(font)
        color = QColor(span.color_int & 0xFFFFFF)
        palette = self._line.palette()
        palette.setColor(QPalette.ColorRole.Text, color)
        self._line.setPalette(palette)
        # The app stylesheet colours every input with the theme; the editor
        # must show the span's own colour on paper white to be WYSIWYG.
        self._line.setStyleSheet(
            f"QLineEdit {{ color: {color.name()}; background: #ffffff; border: none;"
            " padding: 0px 2px; }"
        )

    def badge_height(self) -> int:
        return self._badge.sizeHint().height()

    def text_size(self) -> tuple[int, int]:
        """Pixels needed to show the current text in the current font."""
        metrics = QFontMetrics(self._line.font())
        width = metrics.horizontalAdvance(self._line.text() or " ")
        return width, metrics.height()

    def show_warning(self, message: str) -> None:
        self._badge.setText(message)
        self._badge.setStyleSheet(f"color: {theme.current().error}; background: transparent;")
        self._more.hide()

    def _build_menu(self, decision: FontDecision) -> QMenu:
        menu = QMenu(self._more)
        if decision.alternatives:
            head = menu.addAction("Top alternatives")
            head.setEnabled(False)
            for candidate in decision.alternatives[:3]:
                action = menu.addAction(
                    f"{candidate.display}  ({candidate.score:.2f})"
                )
                action.setToolTip(candidate.reason)
                action.triggered.connect(
                    lambda checked=False, why=candidate.reason: self.infoRequested.emit(
                        why
                    )
                )
        if decision.trace:
            if decision.alternatives:
                menu.addSeparator()
            head = menu.addAction("Decision trace")
            head.setEnabled(False)
            for line in decision.trace:
                action = menu.addAction(line)
                action.setEnabled(False)
        return menu

    # ------------------------------------------------------------------ #
    def _request_preview(self) -> None:
        self.previewRequested.emit(self._line.text())

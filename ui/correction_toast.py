"""Floating correction toast that shows transcription and learns from user edits."""

import re
import sys
from PyQt6.QtWidgets import (
    QWidget, QHBoxLayout, QApplication, QPlainTextEdit, QPushButton,
)
from PyQt6.QtCore import Qt, pyqtSignal, QTimer, QEvent
from PyQt6.QtGui import QPainter, QColor, QPainterPath, QTextOption, QCursor

from ui.pill_widget import canonical_pill_rect

# Common English words that shouldn't appear in Spanish transcriptions
_ENGLISH_ARTIFACTS = {
    "you", "the", "go", "ahead", "thank", "thanks", "please", "sorry",
    "okay", "right", "well", "just", "like", "know", "think", "want",
    "need", "have", "this", "that", "what", "when", "where", "which",
    "here", "there", "been", "being", "does", "done", "going", "would",
    "could", "should", "about", "after", "before", "between", "through",
    "again", "also", "always", "because", "really", "actually", "basically",
    "literally", "absolutely", "definitely", "obviously", "exactly",
    "hello", "goodbye", "welcome", "sure", "fine", "great", "good",
    "nice", "cool", "awesome", "amazing", "wonderful", "perfect",
    "let", "make", "take", "give", "come", "look", "see", "say",
    "tell", "ask", "try", "keep", "still", "much", "many", "very",
    "some", "any", "all", "each", "every", "both", "few", "more",
    "other", "another", "such", "only", "then", "than", "now",
    "with", "from", "into", "over", "under", "around",
}

# Tech words OK in Spanglish
_TECH_PASS = {
    "github", "gitlab", "docker", "kubernetes", "react", "next", "vue",
    "angular", "svelte", "typescript", "javascript", "python", "node",
    "npm", "api", "rest", "graphql", "webhook", "middleware", "backend",
    "frontend", "deploy", "deployment", "commit", "push", "pull", "merge",
    "branch", "clone", "fork", "debug", "pipeline", "sprint", "feature",
    "release", "hotfix", "workflow", "dashboard", "template", "snippet",
    "cache", "redis", "postgres", "sqlite", "mongodb", "server",
    "serverless", "cloud", "edge", "stream", "streaming", "link",
    "feedback", "setup", "update", "upgrade", "layout", "responsive",
    "bug", "fix", "issue", "ticket", "config", "settings", "production",
    "staging", "endpoint", "runtime", "framework", "library", "package",
    "module", "component", "plugin", "refactor", "index", "schema",
    "query", "migration", "prompt", "token", "embedding", "vector",
    "ok", "okay", "supabase", "neon", "prisma", "stripe", "clerk",
}


def detect_suspicious(text: str) -> list[str]:
    """Return list of suspicious words that look like English artifacts in Spanish text."""
    words = re.findall(r'\b[a-zA-Z]+\b', text)
    suspicious: list[str] = []
    for word in words:
        low = word.lower()
        if low in _ENGLISH_ARTIFACTS and low not in _TECH_PASS:
            suspicious.append(word)
    return suspicious


class CorrectionToast(QWidget):
    """Floating editable toast showing transcription. User edits -> dictionary learns."""

    correction_submitted = pyqtSignal(str, str)  # (original, corrected)

    _MIN_HEIGHT = 46
    _MAX_LINES = 6
    _COMFORTABLE_WIDTH = 560
    # Distance from the toast's bottom edge to the pill's canonical top edge
    # (canonical_pill_rect().top(), see ui/pill_widget.py — 2026-08-25;
    # before that it was the screen-bottom-derived pill top). Derived from
    # the old fixed layout: offset(84) = _BOTTOM_GAP(38) + _MIN_HEIGHT(46).
    # Keeping this constant means growth expands the toast upward, never
    # toward the pill's spot.
    _BOTTOM_GAP = 38

    def __init__(self, parent=None):
        super().__init__(parent)
        # Simple flags — NO WindowDoesNotAcceptFocus (causes crash on flag change)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

        self._original = ""
        self._has_suspicious = False
        self._bg_color = QColor(15, 15, 15, 230)
        self._pending_text = ""  # For delayed show
        self._hover = False  # mouse over the widget — pauses auto-hide
        # Screen resolved once in _do_show() (cursor's screen at that
        # moment) and reused by _content_height()/_resize_to_content() —
        # so a mid-edit resize can't jump the toast to a different monitor
        # just because the mouse drifted there while typing.
        self._anchor_screen = None

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 4, 6, 4)
        layout.setSpacing(4)

        # Editable text — ClickFocus prevents stealing focus on show
        self.text_edit = QPlainTextEdit()
        self.text_edit.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.text_edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.text_edit.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.text_edit.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.text_edit.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        # QPlainTextEdit's built-in minimumSizeHint (~68px tall) would otherwise force the
        # layout to floor the toast at that height regardless of content — we manage the
        # height ourselves in _content_height()/_resize_to_content(). Note: must be >0,
        # since Qt treats an explicit (0, 0) minimum size as "unset" and falls back to
        # minimumSizeHint() anyway.
        self.text_edit.setMinimumHeight(1)
        self.text_edit.setStyleSheet("""
            QPlainTextEdit {
                background-color: transparent;
                color: #D0D0D0;
                border: none;
                font-family: Consolas, monospace;
                font-size: 15px;
                selection-background-color: rgba(255, 180, 0, 80);
            }
        """)
        layout.addWidget(self.text_edit)

        # Confirm button (hidden until text is edited)
        self.btn_ok = QPushButton("OK")
        self.btn_ok.setFixedSize(30, 26)
        self.btn_ok.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_ok.setStyleSheet("""
            QPushButton {
                background-color: rgba(80, 200, 80, 180);
                color: white; border: none; border-radius: 6px;
                font-size: 11px; font-weight: bold;
            }
            QPushButton:hover { background-color: rgba(80, 220, 80, 220); }
        """)
        self.btn_ok.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_ok.clicked.connect(self._on_submit)
        self.btn_ok.setVisible(False)
        layout.addWidget(self.btn_ok)
        layout.setAlignment(self.btn_ok, Qt.AlignmentFlag.AlignTop)

        # Dismiss button
        self.btn_dismiss = QPushButton("x")
        self.btn_dismiss.setFixedSize(26, 26)
        self.btn_dismiss.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_dismiss.setStyleSheet("""
            QPushButton {
                background-color: transparent; color: #808080;
                border: none; border-radius: 13px; font-size: 14px;
            }
            QPushButton:hover { color: #FFFFFF; background-color: rgba(255, 255, 255, 15); }
        """)
        self.btn_dismiss.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_dismiss.clicked.connect(self._dismiss)
        layout.addWidget(self.btn_dismiss)
        layout.setAlignment(self.btn_dismiss, Qt.AlignmentFlag.AlignTop)

        # Auto-hide timer
        self._auto_hide = QTimer(self)
        self._auto_hide.setSingleShot(True)
        self._auto_hide.timeout.connect(self._dismiss)

        self.text_edit.textChanged.connect(self._on_text_changed)
        self.text_edit.installEventFilter(self)

    def enterEvent(self, event):
        self._hover = True
        self._auto_hide.stop()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover = False
        self._maybe_restart_timer()
        super().leaveEvent(event)

    def eventFilter(self, obj, event):
        if obj is self.text_edit:
            if event.type() == QEvent.Type.FocusIn:
                self._auto_hide.stop()
            elif event.type() == QEvent.Type.FocusOut:
                self._maybe_restart_timer()
            elif event.type() == QEvent.Type.KeyPress:
                if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                    if not (event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
                        self._on_submit()
                        return True
                    # Shift+Enter: fall through, let QPlainTextEdit insert the newline
        return super().eventFilter(obj, event)

    def _maybe_restart_timer(self):
        """Resume auto-hide (10s) once hover+focus are both clear and there's no pending edit."""
        if not self.isVisible():
            return  # stale event after dismiss — don't arm a timer for the next show
        if self._hover or self.text_edit.hasFocus():
            return
        if self.text_edit.toPlainText() != self._original:
            return  # pending edit — _on_text_changed already stopped the timer
        self._auto_hide.start(10000)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(0.0, 0.0, float(self.width()), float(self.height()), 17, 17)
        bg = QColor(40, 25, 0, 235) if self._has_suspicious else self._bg_color
        painter.fillPath(path, bg)
        painter.setPen(QColor(255, 180, 0, 60) if self._has_suspicious else QColor(255, 255, 255, 30))
        painter.drawRoundedRect(0, 0, self.width(), self.height(), 17, 17)
        painter.end()

    def show_transcription(self, text: str):
        """Queue showing the toast after a delay so it doesn't interfere with paste."""
        self._pending_text = text
        # Show 800ms after paste to avoid focus interference
        QTimer.singleShot(800, self._do_show)

    def _do_show(self):
        """Actually display the toast."""
        text = self._pending_text
        if not text:
            return

        self._original = text
        suspicious = detect_suspicious(text)
        self._has_suspicious = len(suspicious) > 0

        # Width first — word-wrap (and therefore height) depends on it.
        # Screen resolved HERE (cursor's screen, same rule PillWidget uses —
        # 2026-08-25, was always primaryScreen() before) and cached in
        # self._anchor_screen so _content_height()/_resize_to_content()
        # (called while the user is still editing) stay pinned to this same
        # monitor instead of re-resolving and possibly jumping screens.
        font_metrics = self.text_edit.fontMetrics()
        text_width = font_metrics.horizontalAdvance(text) + 100
        screen = QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
        self._anchor_screen = screen
        max_width = self._COMFORTABLE_WIDTH
        if screen:
            max_width = min(max_width, screen.availableGeometry().width() - 80)
        width = max(250, min(text_width, max_width))
        self.setFixedWidth(width)
        # setFixedWidth only sets a constraint — force a real resize now so the layout
        # cascades to text_edit synchronously (needed before measuring content height below).
        self.resize(width, self.height() or self._MIN_HEIGHT)

        self.text_edit.setPlainText(text)
        self.btn_ok.setVisible(False)

        height = self._content_height()

        # Position above the pill's CANONICAL spot (2026-08-25), not a live
        # pill.geometry() query — the pill is very likely already hidden
        # (back to IDLE) by the time this toast appears; see
        # canonical_pill_rect()'s docstring in ui/pill_widget.py. Anchored
        # to the toast's bottom edge (_BOTTOM_GAP above the pill's top) so
        # growth still expands upward, same as before.
        if screen:
            geo = screen.availableGeometry()
            x = geo.center().x() - (width // 2)
            pill_top = canonical_pill_rect(screen).top()
            y = pill_top - self._BOTTOM_GAP - height
            self.setGeometry(x, y, width, height)
        else:
            self.setFixedHeight(height)

        self.show()
        self._reapply_topmost()

        # Long timeout: 20s normal, 30s if suspicious words
        self._auto_hide.start(30000 if self._has_suspicious else 20000)

    def _content_height(self) -> int:
        """Height needed for the current content, clamped to [1 line, ~6 lines or 40% of screen].

        Measured with QFontMetrics.boundingRect instead of
        self.text_edit.document().size() — QPlainTextEdit's own document layout only
        re-syncs to the widget's width on a real resizeEvent, which can lag behind a
        programmatic resize/setPlainText and yield a stale (usually 1-line) height.
        boundingRect has no such lag: it's a pure function of text + width.
        """
        fm = self.text_edit.fontMetrics()
        doc_margin = int(self.text_edit.document().documentMargin())
        frame = self.text_edit.frameWidth()
        inner_width = max(20, self.text_edit.width() - 2 * (doc_margin + frame))
        text = self.text_edit.toPlainText() or " "
        rect = fm.boundingRect(0, 0, inner_width, 0, int(Qt.TextFlag.TextWordWrap), text)
        text_edit_height = rect.height() + 2 * (doc_margin + frame) + 4

        margins = self.layout().contentsMargins()
        total_height = text_edit_height + margins.top() + margins.bottom()

        line_height = fm.lineSpacing()
        screen = self._anchor_screen or QApplication.primaryScreen()
        screen_cap = screen.availableGeometry().height() * 0.4 if screen else 600
        max_height = min(self._MIN_HEIGHT + line_height * (self._MAX_LINES - 1), screen_cap)

        return int(max(self._MIN_HEIGHT, min(total_height, max_height)))

    def _resize_to_content(self):
        """Grow/shrink the toast height while editing, anchored to the pill's
        canonical top edge (grows upward) — reuses self._anchor_screen (set
        once in _do_show()) rather than re-resolving "screen under cursor",
        so an in-progress edit can't make the toast jump monitors just
        because the mouse drifted while typing.
        """
        if not self.isVisible():
            return
        new_height = self._content_height()
        if new_height == self.height():
            return
        screen = self._anchor_screen or QApplication.primaryScreen()
        if screen:
            pill_top = canonical_pill_rect(screen).top()
            y = pill_top - self._BOTTOM_GAP - new_height
            self.setGeometry(self.x(), y, self.width(), new_height)
        else:
            self.setFixedHeight(new_height)
        self._reapply_topmost()

    def _reapply_topmost(self):
        """Force topmost on Windows — must be reapplied after every size/position change."""
        if sys.platform == "win32":
            try:
                import ctypes
                hwnd = int(self.winId())
                if hwnd:
                    ctypes.windll.user32.SetWindowPos(
                        hwnd, -1, 0, 0, 0, 0, 0x0002 | 0x0001 | 0x0010,
                    )
            except Exception:
                pass

    def _on_text_changed(self):
        changed = self.text_edit.toPlainText() != self._original
        self.btn_ok.setVisible(changed)
        if changed:
            self._auto_hide.stop()
        self._resize_to_content()

    def _on_submit(self):
        corrected = self.text_edit.toPlainText().strip()
        if corrected and corrected != self._original:
            self.correction_submitted.emit(self._original, corrected)
        self._dismiss()

    def _dismiss(self):
        self._auto_hide.stop()
        self.hide()

"""Floating panel that shows the corrections of the last English dictation.

Appears on the right edge of the screen under the cursor, never takes focus
(the text is already pasted and the user keeps typing), stays until closed
with the X or Esc, and is REPLACED (not stacked) by the next dictation.

Everything here is defensive: a failure while painting is logged and ignored,
it must never break a dictation.
"""

import html
import sys
import threading
import traceback

from PyQt6.QtCore import QObject, QPointF, QRectF, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QCursor, QPainter, QPainterPath, QPen, QPolygonF
from PyQt6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QVBoxLayout, QWidget,
)

from core.win_topmost import apply_noactivate_style, ensure_topmost
from ui.english_shadowing import MicButton, Shadower

PANEL_WIDTH = 380
EDGE_MARGIN = 16
MAX_HEIGHT_RATIO = 0.70
SCROLL_FROM_ROWS = 6          # from this many rows on, rows scroll inside the panel
VISIBLE_ROWS_WHEN_SCROLLING = 5
MAX_HEIGHT_PX = 720           # never taller than a 768 px screen can hold
FOOTER_HEIGHT = 30            # "Explícame" + "0,75×" row under the scroll area
SLOW_RATE = 0.75

_BG = QColor(0x0B, 0x0B, 0x0F, 235)            # #0B0B0F at ~92 %
_BORDER = QColor(255, 255, 255, 20)             # rgba(255,255,255,0.08)
_TEXT = "#F5F5F7"
_MUTED = "#9A9AA6"
_CYAN = "#22D3EE"
_FONT = "Arial, sans-serif"                     # same family the app's widgets use
_ERR = "#FCA5A5"
_RADIUS = 16


def _esc(text: str) -> str:
    return html.escape(str(text or ""), quote=False)


def _label(html_text: str, size_px: int, color: str) -> QLabel:
    lbl = QLabel(html_text)
    lbl.setTextFormat(Qt.TextFormat.RichText)
    lbl.setWordWrap(True)
    lbl.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    lbl.setStyleSheet(
        f"QLabel {{ color: {color}; font-family: {_FONT}; font-size: {size_px}px;"
        " background: transparent; }"
    )
    return lbl


PLAY_SIZE = 24
_TIP_PLAY = "Escuchar"
_TIP_STOP = "Parar"


class PlayButton(QPushButton):
    """Small round play/stop button. Icon is painted (no font glyph needed).

    States: idle (triangle), loading (three dots, dimmed), playing (square).
    Never takes focus, so the app the user types in keeps it.
    """

    def __init__(self, text: str, parent=None):
        super().__init__(parent)
        self.text_to_say = text
        self.state = "idle"
        self.setFixedSize(PLAY_SIZE, PLAY_SIZE)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName(_TIP_PLAY)
        self.setToolTip(_TIP_PLAY)

    def set_state(self, state: str, hint: str = "") -> None:
        self.state = state
        tip = {"idle": _TIP_PLAY, "loading": _TIP_STOP, "playing": _TIP_STOP}[state]
        self.setToolTip(hint or tip)
        self.setAccessibleName(tip)
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        hover = self.underMouse()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255, 38 if hover else 18))
        p.drawEllipse(r)
        color = QColor(_CYAN)
        if self.state == "loading":
            color.setAlpha(150)
        elif not hover and self.state == "idle":
            color.setAlpha(210)
        p.setBrush(color)
        c = r.center()
        if self.state == "playing":
            p.drawRoundedRect(QRectF(c.x() - 4, c.y() - 4, 8, 8), 1.5, 1.5)
        elif self.state == "loading":
            for dx in (-5, 0, 5):
                p.drawEllipse(QPointF(c.x() + dx, c.y()), 1.6, 1.6)
        else:
            p.drawPolygon(QPolygonF([QPointF(c.x() - 3, c.y() - 5), QPointF(c.x() - 3, c.y() + 5),
                                     QPointF(c.x() + 5, c.y())]))
        p.end()

    def enterEvent(self, e):
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self.update()
        super().leaveEvent(e)


class Speaker(QObject):
    """One shared player: generating (worker thread) + playing, one at a time.

    No queue, no prefetch: pressing another button cuts the current one.
    """

    _finished = pyqtSignal(int, str, str)      # token, wav path ("" on error), hint

    def __init__(self, parent=None):
        super().__init__(parent)
        self._token = 0
        self._current = None
        self._player = None
        self.rate = 1.0                         # 0.75 = slow voice (pitch is preserved by Qt)
        self._finished.connect(self._on_finished)

    def set_rate(self, rate: float) -> None:
        self.rate = float(rate)
        try:
            if self._player is not None:
                self._player.setPlaybackRate(self.rate)
        except Exception:
            pass

    def toggle(self, button: "PlayButton") -> None:
        try:
            was_active = button is self._current and button.state != "idle"
            self.stop()
            if was_active:
                return
            self._current = button
            button.set_state("loading")
            threading.Thread(target=self._work, args=(self._token, button.text_to_say),
                             daemon=True).start()
        except Exception:
            print("[english-tts] toggle failed (ignored):")
            traceback.print_exc()

    def stop(self) -> None:
        """Cut whatever is generating or playing and reset its button."""
        self._token += 1                        # late results of older presses are dropped
        cur, self._current = self._current, None
        try:
            if self._player is not None:
                self._player.stop()
        except Exception:
            pass
        self._reset(cur)

    @staticmethod
    def _reset(button, hint: str = "") -> None:
        if button is None:
            return
        try:
            button.set_state("idle", hint)
        except RuntimeError:                    # button already deleted with its row
            pass

    # -- worker thread: no widgets here, only the signal
    def _work(self, token: int, text: str) -> None:
        try:
            from core import english_tts
            path = str(english_tts.synthesize(text))
            self._finished.emit(token, path, "")
        except Exception as e:
            print(f"[english-tts] could not get audio: {e}")
            self._finished.emit(token, "", getattr(e, "hint", "No se pudo generar el audio"))

    def _make_player(self):
        from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer
        player = QMediaPlayer(self)
        self._audio_out = QAudioOutput(self)
        self._audio_out.setVolume(1.0)
        player.setAudioOutput(self._audio_out)
        player.mediaStatusChanged.connect(self._on_status)
        player.errorOccurred.connect(self._on_player_error)
        return player

    def _fail(self, hint: str) -> None:
        cur, self._current = self._current, None
        self._reset(cur, hint)

    def _on_finished(self, token: int, path: str, hint: str) -> None:
        if token != self._token or self._current is None:
            return                              # stopped or replaced meanwhile
        if not path:
            self._fail(hint)
            return
        try:
            if self._player is None:
                self._player = self._make_player()
            self._player.setSource(QUrl.fromLocalFile(path))
            self._player.setPlaybackRate(self.rate)
            self._player.play()
            self._current.set_state("playing")
        except Exception as e:
            print(f"[english-tts] playback failed (ignored): {e}")
            self._fail("No se pudo reproducir el audio")

    def _on_status(self, status) -> None:
        from PyQt6.QtMultimedia import QMediaPlayer
        if status == QMediaPlayer.MediaStatus.EndOfMedia and self._current is not None:
            self._token += 1
            cur, self._current = self._current, None
            self._reset(cur)

    def _on_player_error(self, _err, message: str = "") -> None:
        print(f"[english-tts] player error (ignored): {message}")
        self._fail("No se pudo reproducir el audio")


class Explainer(QObject):
    """Runs the "Explícame" LLM call off the UI thread; one request at a time.

    `explain_fn(original, corrected, corrections, upgrade) -> str` is swappable
    (tests inject a fake). A new request or cancel() drops late results.
    """

    _finished = pyqtSignal(int, str, str)      # token, lesson text, error hint ("" when ok)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._token = 0
        self.explain_fn = None
        self.on_result = None                  # callback(text, hint) on the Qt thread
        self._finished.connect(self._on_finished)

    def request(self, original, corrected, corrections, upgrade) -> None:
        self._token += 1
        threading.Thread(target=self._work, daemon=True, args=(
            self._token, original, corrected, corrections, upgrade)).start()

    def cancel(self) -> None:
        self._token += 1

    def _work(self, token, original, corrected, corrections, upgrade) -> None:
        try:
            fn = self.explain_fn
            if fn is None:
                from core.english_explain import explain as fn
            self._finished.emit(token, fn(original, corrected, corrections, upgrade), "")
        except Exception as e:
            print(f"[english-explain] failed: {e}")
            self._finished.emit(token, "", getattr(e, "hint", "No se pudo generar la explicación"))

    def _on_finished(self, token: int, text: str, hint: str) -> None:
        if token != self._token or self.on_result is None:
            return
        try:
            self.on_result(text, hint)
        except Exception:
            print("[english-explain] result handler failed (ignored):")
            traceback.print_exc()


def _pill(text: str, checkable: bool = False) -> QPushButton:
    btn = QPushButton(text)
    btn.setCheckable(checkable)
    btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setFixedHeight(FOOTER_HEIGHT)
    btn.setStyleSheet(
        f"QPushButton {{ background: rgba(255,255,255,0.08); color: {_TEXT}; border: none;"
        f" border-radius: {FOOTER_HEIGHT // 2}px; padding: 0 14px; font-family: {_FONT}; font-size: 13px; }}"
        "QPushButton:hover { background: rgba(255,255,255,0.14); }"
        f"QPushButton:checked {{ background: rgba(34,211,238,0.22); color: {_CYAN}; }}"
        f"QPushButton:disabled {{ color: {_MUTED}; }}"
    )
    return btn


class EnglishPopup(QWidget):
    """One instance per app; call show_result() for every English dictation."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool          # no taskbar entry
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setFixedWidth(PANEL_WIDTH)

        self._rows: list[QWidget] = []
        self._noactivate_done = False
        self.speaker = Speaker(self)
        self._play_buttons: list[PlayButton] = []
        self._mic_buttons: list[MicButton] = []
        self._content = None
        self._lay = None
        self.shadower = Shadower(self)
        self.shadower.on_result = self._on_shadow_result
        self.explainer = Explainer(self)
        self.explainer.on_result = self._on_explain_result
        self._dictation = {}                    # what "Explícame" explains
        self._explain_state = "idle"            # idle | loading | done | error
        self._explain_text = ""
        self._explain_box = None
        self._explain_label = None

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 12, 18)
        root.setSpacing(12)

        header = QHBoxLayout()
        header.setSpacing(8)
        title = _label("Tu inglés, corregido", 16, _TEXT)
        title.setStyleSheet(title.styleSheet() + " QLabel { font-weight: bold; }")
        header.addWidget(title, 1)
        self.btn_close = QPushButton("✕")
        self.btn_close.setAccessibleName("Cerrar")
        self.btn_close.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_close.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_close.setFixedSize(32, 32)
        self.btn_close.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {_MUTED}; border: none;"
            " border-radius: 16px; font-size: 14px; }"
            f"QPushButton:hover {{ background: rgba(255,255,255,0.10); color: {_TEXT}; }}"
        )
        self.btn_close.clicked.connect(self.hide)
        header.addWidget(self.btn_close, 0, Qt.AlignmentFlag.AlignTop)
        root.addLayout(header)

        self.scroll = QScrollArea()
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            "QScrollBar:vertical { background: transparent; width: 8px; margin: 2px 0; }"
            "QScrollBar::handle:vertical { background: rgba(255,255,255,0.22);"
            " border-radius: 4px; min-height: 28px; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
            "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }"
        )
        self.scroll.viewport().setStyleSheet("background: transparent;")
        root.addWidget(self.scroll)

        footer = QHBoxLayout()
        footer.setSpacing(8)
        self.btn_explain = _pill("Explícame")
        self.btn_explain.setToolTip("Una mini clase sobre este dictado")
        self.btn_explain.clicked.connect(self._on_explain_clicked)
        footer.addWidget(self.btn_explain)
        footer.addStretch(1)
        self.btn_slow = _pill("0,75×", checkable=True)
        self.btn_slow.setToolTip("Voz más lenta al escuchar")
        self.btn_slow.setAccessibleName("Voz lenta 0,75×")
        self.btn_slow.toggled.connect(self._on_slow_toggled)
        footer.addWidget(self.btn_slow)
        root.addLayout(footer)

    # ------------------------------------------------------------------ API

    def show_result(self, corrections, corrected: str = "", upgrade: str = "",
                    original: str = "") -> bool:
        """Fill the panel and show it. Returns False when nothing was shown.

        Never raises. With 0 corrections the panel is left closed (and an open
        one is hidden, so it never keeps showing a previous dictation).
        """
        try:
            items = [c for c in (corrections or []) if isinstance(c, dict)
                     and (c.get("original") or c.get("better"))]
            if not items:
                self.hide()
                return False
            self._dictation = {"original": original or "", "corrected": corrected or "",
                               "corrections": items, "upgrade": upgrade or ""}
            self._populate(items, corrected, upgrade)
            self._place()
            self.show()
            self._reassert_window_state()
            return True
        except Exception:
            print("[english-popup] could not show the panel (ignored):")
            traceback.print_exc()
            try:
                self.hide()
            except Exception:
                pass
            return False

    @property
    def row_count(self) -> int:
        return len(self._rows)

    @property
    def play_buttons(self) -> list:
        return list(self._play_buttons)

    @property
    def mic_buttons(self) -> list:
        return list(self._mic_buttons)

    def configure_shadowing(self, transcribe, mic_busy=None, on_attempt=None) -> None:
        """Wire the pieces that live in main.py (transcriber, dictation state, DB)."""
        self.shadower.transcribe = transcribe
        self.shadower.mic_busy = mic_busy
        self.shadower.on_attempt = on_attempt

    def cancel_shadowing(self) -> None:
        self.shadower.cancel()

    @staticmethod
    def _explain_enabled() -> bool:
        import config
        return bool(getattr(config, "ENGLISH_EXPLAIN_ENABLED", True))

    # ---------------------------------------------------- "Explícame" + slow voice

    def _on_slow_toggled(self, on: bool) -> None:
        self.speaker.set_rate(SLOW_RATE if on else 1.0)   # remembered while the app runs

    def _set_explain_button(self, text: str, enabled: bool = True) -> None:
        self.btn_explain.setText(text)
        self.btn_explain.setEnabled(enabled)

    def _on_explain_clicked(self) -> None:
        try:
            if self._explain_state == "loading":
                return
            if self._explain_state == "done":          # fold / unfold the lesson
                show = not self._explain_box.isVisible()
                self._explain_box.setVisible(show)
                self._set_explain_button("Ocultar" if show else "Explícame")
                self._fit()
                self._keep_on_screen()
                return
            d = self._dictation
            self._explain_state = "loading"
            self._explain_box.hide()
            self._set_explain_button("Explicando…", enabled=False)
            self.explainer.request(d.get("original", ""), d.get("corrected", ""),
                                   d.get("corrections", []), d.get("upgrade", ""))
        except Exception:
            print("[english-popup] explain click failed (ignored):")
            traceback.print_exc()

    def _on_explain_result(self, text: str, hint: str) -> None:
        if self._explain_label is None:
            return
        if text:
            self._explain_state, self._explain_text = "done", text
            self._explain_label.setText(text)
            self._explain_label.setStyleSheet(self._explain_label.styleSheet().replace(_ERR, _TEXT))
            self._set_explain_button("Ocultar")
        else:
            self._explain_state = "error"
            self._explain_label.setText(hint or "No se pudo generar la explicación")
            self._explain_label.setStyleSheet(self._explain_label.styleSheet().replace(_TEXT, _ERR))
            self._set_explain_button("Reintentar")
        self._explain_box.show()
        self._fit()
        self._keep_on_screen()
        self.scroll.verticalScrollBar().setValue(self._explain_box.y())

    @staticmethod
    def _shadowing_enabled() -> bool:
        import config
        return bool(getattr(config, "ENGLISH_SHADOWING_ENABLED", True))

    def _play_button(self, text: str) -> PlayButton:
        btn = PlayButton(text)
        btn.clicked.connect(lambda _=False, b=btn: (self.shadower.cancel(), self.speaker.toggle(b)))
        self._play_buttons.append(btn)
        return btn

    def _mic_button(self, text: str):
        """(MicButton, result label) for a phrase, or (None, None) when disabled."""
        if not self._shadowing_enabled():
            return None, None
        btn = MicButton(text)
        btn.clicked.connect(lambda _=False, b=btn: (self.speaker.stop(), self.shadower.toggle(b)))
        lbl = _label("", 12, _MUTED)
        lbl.hide()
        btn.result_label = lbl
        self._mic_buttons.append(btn)
        return btn, lbl

    def _on_shadow_result(self, _button) -> None:
        """A score line appeared or vanished: grow/shrink the panel to fit."""
        try:
            self._fit(keep_scroll=True)
            self._keep_on_screen()
        except Exception as e:
            print(f"[english-popup] relayout failed (ignored): {e}")

    # ------------------------------------------------------------- building

    def _populate(self, items: list, corrected: str, upgrade: str) -> None:
        self.speaker.stop()
        self.shadower.cancel()
        self.explainer.cancel()          # a late lesson must not land on a new dictation
        old = self.scroll.takeWidget()
        if old is not None:
            old.deleteLater()
        self._rows = []
        self._play_buttons = []
        self._mic_buttons = []

        content = QWidget()
        content.setStyleSheet("background: transparent;")
        lay = QVBoxLayout(content)
        lay.setContentsMargins(0, 0, 8, 0)
        lay.setSpacing(14)

        for it in items:
            row = self._build_row(it)
            self._rows.append(row)
            lay.addWidget(row)

        up = (upgrade or "").strip()
        if up and up != (corrected or "").strip():
            lay.addWidget(self._build_upgrade(up))
        self._explain_state, self._explain_text = "idle", ""
        self._explain_box = self._build_explain_box()
        self._explain_box.hide()
        lay.addWidget(self._explain_box)
        lay.addStretch(1)
        self._set_explain_button("Explícame")
        self.btn_explain.setVisible(self._explain_enabled())

        self.scroll.setWidget(content)
        self._content, self._lay = content, lay
        self._fit()

    def _fit(self, keep_scroll: bool = False) -> None:
        content, lay = self._content, self._lay
        keep = self.scroll.verticalScrollBar().value() if keep_scroll else 0
        lay.invalidate()
        lay.activate()

        # Height: content at the final width, capped by the 70 % rule and, from
        # 6 rows on, by "5 rows visible" so the scroll is obvious.
        inner_w = PANEL_WIDTH - 20 - 12 - 8
        content.setFixedWidth(inner_w)
        content.adjustSize()
        full_h = lay.totalHeightForWidth(inner_w)
        content.setFixedHeight(full_h)
        content.setMinimumHeight(0)
        content.setMaximumHeight(16777215)

        want = full_h
        if len(self._rows) >= SCROLL_FROM_ROWS:
            want = min(full_h, self._rows_height(VISIBLE_ROWS_WHEN_SCROLLING, lay, inner_w) + 28)

        screen = self._screen()
        chrome = 16 + 18 + 32 + 12 + 12 + FOOTER_HEIGHT   # margins + header + footer + spacing
        cap = (min(int(screen.availableGeometry().height() * MAX_HEIGHT_RATIO), MAX_HEIGHT_PX) - chrome
               if screen else want)
        view_h = max(60, min(want, cap))
        self.scroll.setFixedHeight(view_h)
        self.scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOn if view_h < full_h
            else Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.verticalScrollBar().setValue(keep)
        self.setFixedHeight(view_h + chrome)

    @staticmethod
    def _rows_height(n: int, lay: QVBoxLayout, inner_w: int) -> int:
        total = 0
        for i in range(min(n, lay.count())):
            w = lay.itemAt(i).widget()
            if w is None:
                continue
            total += lay.spacing() + (w.layout().totalHeightForWidth(inner_w) if w.layout() else w.sizeHint().height())
        return total

    def _build_explain_box(self) -> QWidget:
        box = QWidget()
        v = QVBoxLayout(box)
        v.setContentsMargins(0, 6, 0, 0)
        v.setSpacing(8)
        line = QFrame()
        line.setFixedHeight(1)
        line.setStyleSheet("background: rgba(255,255,255,0.08); border: none;")
        v.addWidget(line)
        lbl = _label("", 13, _TEXT)
        lbl.setTextFormat(Qt.TextFormat.PlainText)    # model text is never interpreted as HTML
        v.addWidget(lbl)
        self._explain_label = lbl
        return box

    def _build_row(self, it: dict) -> QWidget:
        row = QWidget()
        v = QVBoxLayout(row)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(4)
        original, better = _esc(it.get("original")), _esc(it.get("better"))
        res = None
        main = _label(
            f'<span style="color:{_MUTED}"><s>{original}</s> &nbsp;→&nbsp; </span>'
            f'<span style="color:{_CYAN}; font-weight:bold">{better}</span>',
            15, _TEXT)
        line = QHBoxLayout()
        line.setSpacing(6)
        line.addWidget(main, 1)
        better_text = str(it.get("better") or "").strip()
        if better_text:
            line.addWidget(self._play_button(better_text), 0, Qt.AlignmentFlag.AlignTop)
            mic, res = self._mic_button(better_text)
            if mic is not None:
                line.addWidget(mic, 0, Qt.AlignmentFlag.AlignTop)
        v.addLayout(line)
        if better_text and res is not None:
            v.addWidget(res)
        why = (it.get("why") or "").strip()
        if why:
            v.addWidget(_label(_esc(why), 12, _MUTED))
        return row

    def _build_upgrade(self, upgrade: str) -> QWidget:
        box = QWidget()
        v = QVBoxLayout(box)
        v.setContentsMargins(0, 6, 0, 0)
        v.setSpacing(6)
        line = QFrame()
        line.setFixedHeight(1)
        line.setStyleSheet("background: rgba(255,255,255,0.08); border: none;")
        v.addWidget(line)
        head = QHBoxLayout()
        head.setSpacing(6)
        head.addWidget(_label("Así sonaría más natural:", 12, _MUTED), 1)
        head.addWidget(self._play_button(upgrade), 0, Qt.AlignmentFlag.AlignVCenter)
        mic, res = self._mic_button(upgrade)
        if mic is not None:
            head.addWidget(mic, 0, Qt.AlignmentFlag.AlignVCenter)
        v.addLayout(head)
        v.addWidget(_label(_esc(upgrade), 14, _TEXT))
        if res is not None:
            v.addWidget(res)
        return box

    # ------------------------------------------------------------ placement

    def _screen(self):
        return QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()

    def _place(self) -> None:
        screen = self._screen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        x = geo.right() - self.width() - EDGE_MARGIN + 1
        y = geo.top() + (geo.height() - self.height()) // 2
        self.move(x, y)

    def _keep_on_screen(self) -> None:
        """After a resize, nudge the panel up only if its bottom left the screen."""
        screen = self._screen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        overflow = self.y() + self.height() - (geo.bottom() + 1)
        if overflow > 0:
            self.move(self.x(), max(geo.top(), self.y() - overflow))

    @staticmethod
    def _is_native_windows() -> bool:
        # Under the offscreen test platform winId() is not a real HWND.
        return sys.platform == "win32" and QApplication.platformName() == "windows"

    def _reassert_window_state(self) -> None:
        """Topmost + never-activate + no Alt+Tab entry, reapplied on every show."""
        if not self._is_native_windows():
            return
        try:
            hwnd = int(self.winId())
            if hwnd:
                apply_noactivate_style(hwnd)
                ensure_topmost(hwnd)
        except Exception as e:
            print(f"[english-popup] window state failed (ignored): {e}")

    def showEvent(self, event):
        # Style must be set before the first paint so the first show can't grab focus.
        if self._is_native_windows() and not self._noactivate_done:
            try:
                apply_noactivate_style(int(self.winId()))
                self._noactivate_done = True
            except Exception as e:
                print(f"[english-popup] noactivate failed (ignored): {e}")
        super().showEvent(event)

    # --------------------------------------------------------------- events

    def hideEvent(self, event):
        self.speaker.stop()                     # closing the panel silences it
        self.shadower.cancel()
        self.explainer.cancel()
        if self._explain_state == "loading":
            self._explain_state = "idle"
            self._set_explain_button("Explícame")
        super().hideEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.hide()
            return
        super().keyPressEvent(event)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = self.rect().adjusted(0, 0, -1, -1)
        path = QPainterPath()
        path.addRoundedRect(float(r.x()) + 0.5, float(r.y()) + 0.5, float(r.width()),
                            float(r.height()), _RADIUS, _RADIUS)
        p.fillPath(path, _BG)
        p.setPen(QPen(_BORDER, 1))
        p.drawPath(path)
        p.end()

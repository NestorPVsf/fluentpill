"""Shadowing for the English corrections panel: listen, repeat aloud, get a score.

MicButton sits next to each play button. Shadower owns the single recording at
a time: press = start, press again (or 12 s) = stop, then transcribe in a worker
thread and score. Nothing here may break a dictation: failures are logged and
shown as a short hint on the row.
"""

import threading
import traceback

from PyQt6.QtCore import QObject, QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import QPushButton

from core import shadowing

MIC_SIZE = 24
MAX_SECONDS = 12.0
MIN_SECONDS = 0.6
_TIP = {"idle": "Repite en voz alta", "recording": "Parar", "working": "Calificando…"}

COLORS = {"green": "#4ADE80", "amber": "#FBBF24", "red": "#F87171"}
_REC = "#F87171"
_CYAN = "#22D3EE"


class MicButton(QPushButton):
    """Round mic button; states idle / recording / working. Icon is painted."""

    def __init__(self, target: str, parent=None):
        super().__init__(parent)
        self.target = target
        self.state = "idle"
        self.result_label = None            # QLabel under the row, set by the popup
        self.setFixedSize(MIC_SIZE, MIC_SIZE)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.set_state("idle")

    def set_state(self, state: str, hint: str = "") -> None:
        self.state = state
        self.setToolTip(hint or _TIP[state])
        self.setAccessibleName(_TIP[state])
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        hover = self.underMouse()
        p.setPen(Qt.PenStyle.NoPen)
        rec = self.state == "recording"
        p.setBrush(QColor(248, 113, 113, 70) if rec else QColor(255, 255, 255, 38 if hover else 18))
        p.drawEllipse(r)
        c = r.center()
        color = QColor(_REC if rec else _CYAN)
        if self.state == "working":
            color.setAlpha(150)
            p.setBrush(color)
            for dx in (-5, 0, 5):
                p.drawEllipse(QPointF(c.x() + dx, c.y()), 1.6, 1.6)
        elif rec:
            p.setBrush(color)
            p.drawRoundedRect(QRectF(c.x() - 4, c.y() - 4, 8, 8), 1.5, 1.5)
        else:
            if not hover:
                color.setAlpha(210)
            p.setBrush(color)
            p.drawRoundedRect(QRectF(c.x() - 2.5, c.y() - 6, 5, 9), 2.5, 2.5)      # capsule
            pen = p.pen()
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(color, 1.4))
            p.drawArc(QRectF(c.x() - 4.5, c.y() - 4.5, 9, 9), 180 * 16, 180 * 16)  # cradle
            p.drawLine(QPointF(c.x(), c.y() + 4.5), QPointF(c.x(), c.y() + 7))
            p.setPen(pen)
        p.end()

    def enterEvent(self, e):
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self.update()
        super().leaveEvent(e)


class Shadower(QObject):
    """One shared recording + scoring pipeline, one button active at a time.

    transcribe(wav_buffer) -> str   blocking, runs in a worker thread
    mic_busy() -> bool              True while a dictation owns the microphone
    on_attempt(target, heard, score)  called on the Qt thread, for saving
    recorder_factory() -> recorder  AudioRecorder-like (tests inject a fake)
    on_result(button)               called after a result label changed
    """

    _done = pyqtSignal(int, str, str)       # token, heard, error hint

    def __init__(self, parent=None):
        super().__init__(parent)
        self.transcribe = None
        self.mic_busy = None
        self.on_attempt = None
        self.on_result = None
        self.recorder_factory = None
        self._recorder = None
        self._current = None
        self._token = 0
        self._done.connect(self._on_done)

    # ------------------------------------------------------------- Qt thread

    def toggle(self, button: MicButton) -> None:
        try:
            if self._current is button and button.state == "recording":
                self._finish()
            elif self._current is None:
                self._start(button)
            # working on another row: ignore the press, one attempt at a time
        except Exception:
            print("[shadowing] toggle failed (ignored):")
            traceback.print_exc()
            self.cancel()

    def _start(self, button: MicButton) -> None:
        if self.transcribe is None:
            self._show(button, "No disponible", None)
            return
        if self.mic_busy is not None and self.mic_busy():
            self._show(button, "El micrófono está en uso (dictado)", None)
            return
        if self._recorder is None:
            if self.recorder_factory is not None:
                self._recorder = self.recorder_factory()
            else:
                from core.recorder import AudioRecorder
                self._recorder = AudioRecorder()
        self._token += 1
        self._clear(button)
        try:
            self._recorder.start()
        except Exception as e:
            print(f"[shadowing] could not open the mic: {e}")
            self._show(button, "No se pudo abrir el micrófono", None)
            return
        self._current = button
        button.set_state("recording")
        token = self._token
        QTimer.singleShot(int(MAX_SECONDS * 1000), lambda: self._auto_stop(token))

    def _auto_stop(self, token: int) -> None:
        if token == self._token and self._current is not None and self._current.state == "recording":
            self._finish()

    def _finish(self) -> None:
        button, rec = self._current, self._recorder
        try:
            seconds = rec.stop()
        except Exception as e:
            print(f"[shadowing] stop failed: {e}")
            self._current = None
            self._show(button, "Error al grabar", None)
            return
        from config import SILENCE_PEAK_RMS
        if seconds < MIN_SECONDS or rec.peak_rms() < SILENCE_PEAK_RMS:
            self._current = None
            self._show(button, "0 · no se oyó nada", None)
            return
        button.set_state("working")
        wav = rec.get_wav_buffer()
        token = self._token
        threading.Thread(target=self._work, args=(token, wav), daemon=True).start()

    def cancel(self) -> None:
        """Drop whatever is recording or being scored (panel closed, dictation started)."""
        self._token += 1                    # a late transcription is ignored
        button, self._current = self._current, None
        try:
            if self._recorder is not None and self._recorder.is_recording:
                self._recorder.stop()
        except Exception:
            pass
        if button is not None:
            try:
                button.set_state("idle")
            except RuntimeError:            # button already deleted with its row
                pass

    # ---------------------------------------------------------- worker thread

    def _work(self, token: int, wav) -> None:
        try:
            heard = self.transcribe(wav) or ""
            self._done.emit(token, heard, "")
        except Exception as e:
            print(f"[shadowing] transcription failed: {e}")
            self._done.emit(token, "", "No se pudo transcribir")

    # --------------------------------------------------------------- results

    def _on_done(self, token: int, heard: str, err: str) -> None:
        button = self._current
        if token != self._token or button is None:
            return
        self._current = None
        try:
            if err:
                self._show(button, err, None)
                return
            result = shadowing.score(button.target, heard)
            self._show(button, shadowing.feedback(result, heard), shadowing.band(result["score"]))
            if self.on_attempt is not None and shadowing.words(heard):
                self.on_attempt(button.target, heard, result["score"])
        except Exception:
            print("[shadowing] scoring failed (ignored):")
            traceback.print_exc()
            try:
                button.set_state("idle")
            except RuntimeError:
                pass

    def _clear(self, button: MicButton) -> None:
        lbl = button.result_label
        if lbl is not None:
            lbl.hide()
            lbl.setText("")

    def _show(self, button: MicButton, text: str, band) -> None:
        try:
            button.set_state("idle")
            lbl = button.result_label
            if lbl is not None:
                color = COLORS.get(band, "#9A9AA6")
                lbl.setText(f'<span style="color:{color}">{_esc(text)}</span>')
                lbl.show()
            if self.on_result is not None:
                self.on_result(button)
        except RuntimeError:
            pass


def _esc(text: str) -> str:
    import html
    return html.escape(text, quote=False)

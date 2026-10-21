import queue
import numpy as np
from PyQt6.QtWidgets import QWidget
from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtGui import QPainter, QColor
from config import NUM_BARS, VIZ_FPS, BAR_DECAY, BAR_GAIN

# Attack factor (fast rise) and decay factor (slower fall)
_ATTACK_FACTOR = 0.45
_DECAY_FACTOR = 0.18


class AudioVisualizer(QWidget):
    """Subtle animated audio bars. Indigo waveform with separate attack/decay dynamics."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.num_bars = NUM_BARS
        self.bar_values = [0.0] * self.num_bars
        self.audio_queue: queue.Queue | None = None

        self._timer = QTimer()
        self._timer.setInterval(1000 // VIZ_FPS)
        self._timer.timeout.connect(self._update_bars)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

    def set_audio_queue(self, q: queue.Queue):
        self.audio_queue = q

    def start(self):
        self.bar_values = [0.0] * self.num_bars
        self._timer.start()

    def stop(self):
        self._timer.stop()
        self.bar_values = [0.0] * self.num_bars
        self.update()

    def _update_bars(self):
        if not self.audio_queue:
            return

        chunks = []
        while True:
            try:
                chunks.append(self.audio_queue.get_nowait())
            except queue.Empty:
                break

        if chunks:
            latest = chunks[-1]
            chunk = latest[:, 0] if latest.ndim > 1 else latest
            chunk = chunk.astype(np.float32) / 32768.0
            segments = np.array_split(chunk, self.num_bars)
            for i, seg in enumerate(segments):
                if len(seg) > 0:
                    rms = float(np.sqrt(np.mean(seg ** 2)))
                    target = min(rms * BAR_GAIN, 1.0)
                    if target > self.bar_values[i]:
                        # Attack: fast rise
                        self.bar_values[i] += (target - self.bar_values[i]) * _ATTACK_FACTOR
                    else:
                        # Decay: slower fall
                        self.bar_values[i] += (target - self.bar_values[i]) * _DECAY_FACTOR
        else:
            for i in range(self.num_bars):
                self.bar_values[i] *= BAR_DECAY
                if self.bar_values[i] < 0.01:
                    self.bar_values[i] = 0.0

        self.update()

    def paintEvent(self, event):
        from PyQt6.QtGui import QPainterPath, QLinearGradient
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        w = self.width()
        h = self.height()
        if w <= 0 or h <= 0:
            painter.end()
            return

        cy = h / 2.0

        # Smooth entry and exit points for the wave
        vals = [0.0] + self.bar_values + [0.0]
        n_pts = len(vals)
        step = w / (n_pts - 1)

        path = QPainterPath()

        # Top half of the waveform (smooth cubic curve)
        pts_top = []
        for i, val in enumerate(vals):
            # Apply a sine window so the wave tapers beautifully at the edges
            window = np.sin((i / (n_pts - 1)) * np.pi)
            amp = val * window * (h / 2.0) * 0.95
            if amp < 0.6:
                amp = 0.6  # Minimum thickness (thin line when silent)
            pts_top.append((i * step, cy - amp))

        path.moveTo(pts_top[0][0], pts_top[0][1])
        for i in range(n_pts - 1):
            x1, y1 = pts_top[i]
            x2, y2 = pts_top[i + 1]
            ctrl_x = (x1 + x2) / 2.0
            path.cubicTo(ctrl_x, y1, ctrl_x, y2, x2, y2)

        # Bottom half of the waveform (mirrored)
        pts_bot = []
        for i, val in enumerate(vals):
            window = np.sin((i / (n_pts - 1)) * np.pi)
            amp = val * window * (h / 2.0) * 0.95
            if amp < 0.6:
                amp = 0.6
            pts_bot.append((i * step, cy + amp))

        # Draw the bottom half backwards to close the shape
        for i in range(n_pts - 1, 0, -1):
            x1, y1 = pts_bot[i]
            x2, y2 = pts_bot[i - 1]
            ctrl_x = (x1 + x2) / 2.0
            path.cubicTo(ctrl_x, y1, ctrl_x, y2, x2, y2)

        path.closeSubpath()

        # Indigo gradient fill — matches the dashboard accent color
        gradient = QLinearGradient(0, 0, w, 0)
        gradient.setColorAt(0.0, QColor(124, 133, 245, 0))
        gradient.setColorAt(0.2, QColor(124, 133, 245, 140))
        gradient.setColorAt(0.5, QColor(124, 133, 245, 220))
        gradient.setColorAt(0.8, QColor(124, 133, 245, 140))
        gradient.setColorAt(1.0, QColor(124, 133, 245, 0))

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(gradient)
        painter.drawPath(path)

        painter.end()

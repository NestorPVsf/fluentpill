"""Render the English panel offscreen with one shadowing score of each colour.

Run: venv/Scripts/python.exe tools/capture_english_shadowing.py <out.png>
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtCore import QRect, Qt  # noqa: E402
from PyQt6.QtGui import QColor, QFont, QFontDatabase, QImage, QLinearGradient, QPainter  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from core import shadowing  # noqa: E402
from ui.english_popup import EnglishPopup  # noqa: E402

app = QApplication.instance() or QApplication([])
# offscreen has no system fonts: load Segoe UI so text renders instead of boxes
for f in ("segoeui.ttf", "segoeuib.ttf", "seguisym.ttf", "seguiemj.ttf"):
    QFontDatabase.addApplicationFont(os.path.join(os.environ.get("WINDIR", "C:/Windows"), "Fonts", f))
app.setFont(QFont("Segoe UI"))
rows = [
    {"original": "I want to went", "better": "I wanted to go", "why": "Después de 'to' va el verbo base."},
    {"original": "I am agree", "better": "I agree", "why": "'Agree' ya es verbo, sin 'am'."},
    {"original": "make a decision", "better": "I would like to make a decision", "why": "Más educado."},
]
upgrade = "Honestly, I'd rather decide this tomorrow."
heard = ["I wanted to go", "I am agree", "banana"]

panel = EnglishPopup()
panel.show_result(rows, "x", upgrade)
panel.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
panel.show()
targets = [m.target for m in panel.mic_buttons]
heard.append("Honestly I would rather decide this tomorrow")
for m, h in zip(panel.mic_buttons, heard):
    res = shadowing.score(m.target, h)
    panel.shadower._show(m, shadowing.feedback(res, h), shadowing.band(res["score"]))
    print(res["score"], shadowing.band(res["score"]))

w, h = 760, 768
panel.move(0, 0)
img = QImage(w, h, QImage.Format.Format_ARGB32)
p = QPainter(img)
g = QLinearGradient(0, 0, w, h)
g.setColorAt(0, QColor("#E8E2D0"))
g.setColorAt(1, QColor("#3B6EA5"))
p.fillRect(QRect(0, 0, w, h), g)
p.drawImage(w - panel.width() - 16, (h - panel.height()) // 2, panel.grab().toImage())
p.end()
os.makedirs(os.path.dirname(os.path.abspath(sys.argv[1])), exist_ok=True)
img.save(sys.argv[1])
print(sys.argv[1], "panel", panel.width(), "x", panel.height(), "of", h, "scroll_max=", panel.scroll.verticalScrollBar().maximum())

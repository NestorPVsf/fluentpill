"""Render the English panel offscreen with real rows from transcriptions.db.

Run: venv/Scripts/python.exe tools/capture_english_popup.py <db> <transcription_id> <out_dir>
Reads the DB read-only. Writes english-popup.png (real rows) and
english-popup-8.png (same rows repeated to 8, to show the scroll).
"""
import os
import sqlite3
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtCore import QRect, Qt  # noqa: E402
from PyQt6.QtGui import QColor, QImage, QLinearGradient, QPainter  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from ui.english_popup import EnglishPopup  # noqa: E402

app = QApplication.instance() or QApplication([])


def load(db, tid):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(
        "select * from english_corrections where transcription_id=? order by id", (tid,))]
    corrected = con.execute("select text from transcriptions where id=?", (tid,)).fetchone()
    con.close()
    return rows, (corrected[0] if corrected else "")


def shot(panel, path, w=760, h=900):
    """Panel over a busy backdrop so the translucency is judged, not assumed."""
    panel.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    panel.show()
    panel.move(0, 0)
    img = QImage(w, h, QImage.Format.Format_ARGB32)
    p = QPainter(img)
    g = QLinearGradient(0, 0, w, h)
    g.setColorAt(0, QColor("#E8E2D0"))
    g.setColorAt(1, QColor("#3B6EA5"))
    p.fillRect(QRect(0, 0, w, h), g)
    p.setPen(QColor(30, 30, 30))
    for y in range(20, h, 26):
        p.drawText(24, y, "Lorem ipsum dolor sit amet, the user keeps typing in the app behind " * 2)
    p.drawImage(w - panel.width() - 16, (h - panel.height()) // 2, panel.grab().toImage())
    p.end()
    img.save(path)
    print(path, panel.width(), panel.height(), "scroll_max=", panel.scroll.verticalScrollBar().maximum())


db, tid, out = sys.argv[1], int(sys.argv[2]), sys.argv[3]
os.makedirs(out, exist_ok=True)
rows, corrected = load(db, tid)
upgrade = next((r["upgrade"] for r in rows if r.get("upgrade")), "")
panel = EnglishPopup()
panel.show_result(rows, corrected, upgrade)
shot(panel, os.path.join(out, "english-popup.png"))
more = (rows * 2)[:8]
panel.show_result(more, corrected, upgrade)
shot(panel, os.path.join(out, "english-popup-8.png"))

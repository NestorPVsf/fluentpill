"""Check: English corrections panel (ui/english_popup.py), offscreen.

Run: venv/Scripts/python.exe tools/test_english_popup.py
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtWidgets import QApplication, QLabel  # noqa: E402

from ui.english_popup import EnglishPopup, MAX_HEIGHT_RATIO  # noqa: E402

app = QApplication.instance() or QApplication([])


def corr(n):
    return [{"original": f"wrong {i}", "better": f"right {i}", "why": f"Porque {i}.", "type": "grammar"}
            for i in range(n)]


def texts(panel):
    return [w.text() for w in panel.findChildren(QLabel)]


p = EnglishPopup()

# 3 corrections -> 3 rows, panel visible, on the right edge of the screen
assert p.show_result(corr(3), "a b c", "a better b c") is True
assert p.isVisible() and p.row_count == 3
geo = (QApplication.screenAt(p.pos()) or QApplication.primaryScreen()).availableGeometry()
assert p.x() + p.width() <= geo.right() + 1 and p.x() > geo.center().x(), "not on the right side"
assert p.height() <= geo.height() * MAX_HEIGHT_RATIO + 1
joined = " ".join(texts(p))
assert "Tu inglés, corregido" in joined
assert "Así sonaría más natural:" in joined and "a better b c" in joined

# upgrade equal to corrected, or missing -> no upgrade block
p.show_result(corr(2), "same text", "same text")
assert "Así sonaría" not in " ".join(texts(p))
p.show_result(corr(2), "x", "")
assert "Así sonaría" not in " ".join(texts(p))

# HTML in the data is shown as text, not interpreted
p.show_result([{"original": "<b>x</b>", "better": "a & b", "why": "<i>y</i>"}], "", "")
assert any("&lt;b&gt;x&lt;/b&gt;" in t for t in texts(p))

# second send REPLACES the content, no second window
before = len([w for w in QApplication.topLevelWidgets() if isinstance(w, EnglishPopup)])
p.show_result(corr(3), "", "")
p.show_result(corr(1), "", "")
after = len([w for w in QApplication.topLevelWidgets() if isinstance(w, EnglishPopup)])
assert p.row_count == 1 and before == after == 1
assert sum("right 0" in t for t in texts(p)) == 1 and not any("right 2" in t for t in texts(p))

# 0 corrections -> never opens; an open one is closed (no stale content)
q = EnglishPopup()
assert q.show_result([], "x", "y") is False and not q.isVisible()
assert q.show_result(None) is False
assert p.show_result([], "x", "y") is False and not p.isVisible()

# 8 rows -> scroll inside the panel, never above 70 % of the screen
p.show_result(corr(8), "x", "x better")
assert p.row_count == 8
assert p.scroll.verticalScrollBar().maximum() > 0, "8 rows must scroll"
assert p.height() <= geo.height() * MAX_HEIGHT_RATIO + 1
p.show_result(corr(5), "x", "")
assert p.scroll.verticalScrollBar().maximum() == 0, "5 rows fit without scroll"

# window flags: never steals focus, always on top, no taskbar entry
f = p.windowFlags()
assert f & Qt.WindowType.WindowStaysOnTopHint
assert f & Qt.WindowType.FramelessWindowHint
assert f & Qt.WindowType.Tool, "Tool = no taskbar entry"
assert p.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
assert p.focusPolicy() == Qt.FocusPolicy.NoFocus
assert p.btn_close.focusPolicy() == Qt.FocusPolicy.NoFocus
assert QApplication.activeWindow() is not p

# X and Esc close it
p.show_result(corr(2), "", "")
p.btn_close.click()
assert not p.isVisible()
from PyQt6.QtGui import QKeyEvent  # noqa: E402
from PyQt6.QtCore import QEvent  # noqa: E402
p.show_result(corr(2), "", "")
p.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier))
assert not p.isVisible()

# a failure while building must be swallowed (never breaks the dictation)
r = EnglishPopup()
r._populate = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
assert r.show_result(corr(2), "", "") is False and not r.isVisible()

print("test_english_popup OK")

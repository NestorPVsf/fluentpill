import math
import time
import sys
from ctypes import c_void_p

if sys.platform == "darwin":
    try:
        import AppKit
        import objc
    except ImportError:
        pass

from PyQt6.QtWidgets import QWidget, QApplication
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QPoint, QRect, QRectF
from PyQt6.QtGui import QPainter, QColor, QPainterPath, QPen, QPixmap, QCursor, QFont
from ui.audio_visualizer import AudioVisualizer
from core.win_topmost import (
    ensure_topmost,
    apply_noactivate_style,
    fullscreen_app_active,
    demote_topmost,
)
from config import (
    PILL_WIDTH_IDLE,
    PILL_WIDTH_RECORDING,
    PILL_WIDTH_STATUS,
    PILL_HEIGHT,
    PILL_OPACITY,
    PILL_CORNER_RADIUS,
    LOGO_SIZE,
    LOGO_PATH,
    PILL_ALWAYS_VISIBLE,
    PILL_VERTICAL_ANCHOR,
    PILL_REVEAL_MS,
    PILL_DISMISS_MS,
)


# ---------------------------------------------------------------------------
# Easing functions
# ---------------------------------------------------------------------------

def ease_out_expo(t: float) -> float:
    return 1.0 - math.pow(2.0, -10.0 * t) if t < 1.0 else 1.0


def ease_in_quad(t: float) -> float:
    return t * t


def ease_out_quint(t: float) -> float:
    """Strong ease-out: fast start, long settle. Used for the reveal pose
    (opacity + rise) — the "Apple" feel the 2026-08-25 brief asked for."""
    return 1.0 - math.pow(1.0 - t, 5.0)


def ease_in_cubic(t: float) -> float:
    """Ease-in: slow start, accelerating. Used for the (shorter, faster)
    dismiss pose (opacity + sink)."""
    return t * t * t


# Reveal/dismiss pose offsets, in pixels. Not exposed via config.py (the
# 2026-08-25 brief only asked for the two durations + the vertical anchor to
# be tunable) — small, deliberately subtle per the brief ("prioridad es que
# se vea limpio"), kept here as named constants instead of magic numbers.
_REVEAL_RISE_PX = 10   # pill starts this far BELOW its canonical spot, then rises into place
_DISMISS_SINK_PX = 6   # pill sinks this far below its current spot while fading out


# ---------------------------------------------------------------------------
# WidthAnimator — time-based width interpolation with configurable easing
# ---------------------------------------------------------------------------

class WidthAnimator:
    """Time-based width interpolator. Replaces the old frame-rate-dependent lerp."""

    def __init__(self):
        self.start_w: float = 0.0
        self.target_w: float = 0.0
        self.start_time: float = 0.0
        self.duration: float = 0.0
        self.easing = ease_out_expo

    def start(self, current: float, target: float, duration_ms: float, easing=None):
        self.start_w = float(current)
        self.target_w = float(target)
        self.start_time = time.monotonic() * 1000.0
        self.duration = float(duration_ms)
        self.easing = easing if easing is not None else ease_out_expo

    def value(self) -> tuple[float, bool]:
        """Returns (current_value, is_finished)."""
        now = time.monotonic() * 1000.0
        elapsed = now - self.start_time
        if elapsed >= self.duration:
            return self.target_w, True
        t = elapsed / self.duration
        t_e = self.easing(t)
        return self.start_w + (self.target_w - self.start_w) * t_e, False


# ---------------------------------------------------------------------------
# ColorAnimator — 200ms ease_out_expo interpolation between two QColors
# ---------------------------------------------------------------------------

class ColorAnimator:
    """Interpolates RGBA linearly, eased with ease_out_expo over 200ms."""

    DURATION_MS = 200.0

    def __init__(self, initial: QColor):
        self._current = initial
        self._start: QColor = initial
        self._target: QColor = initial
        self._start_time: float = 0.0
        self._active: bool = False

    def animate_to(self, target: QColor):
        if target.getRgb() == self._target.getRgb() and not self._active:
            return
        self._start = self._current
        self._target = target
        self._start_time = time.monotonic() * 1000.0
        self._active = True

    def tick(self) -> QColor:
        if not self._active:
            return self._current
        now = time.monotonic() * 1000.0
        elapsed = now - self._start_time
        if elapsed >= self.DURATION_MS:
            self._current = self._target
            self._active = False
            return self._current
        t = elapsed / self.DURATION_MS
        t_e = ease_out_expo(t)
        sr, sg, sb, sa = self._start.getRgb()
        er, eg, eb, ea = self._target.getRgb()
        r = int(sr + (er - sr) * t_e)
        g = int(sg + (eg - sg) * t_e)
        b = int(sb + (eb - sb) * t_e)
        a = int(sa + (ea - sa) * t_e)
        self._current = QColor(r, g, b, a)
        return self._current

    def current(self) -> QColor:
        return self._current

    def is_active(self) -> bool:
        return self._active


# ---------------------------------------------------------------------------
# State-based design tokens
# ---------------------------------------------------------------------------

_STATE_BG: dict[str, QColor] = {
    "idle":       QColor(8,  8,  10, 245),
    "recording":  QColor(8,  8,  10, 250),
    "processing": QColor(8,  8,  10, 250),
    "done":       QColor(6,  18, 12, 250),
    "error":      QColor(16, 6,  6,  250),
}

_STATE_BORDER: dict[str, tuple[QColor, int]] = {
    # Ring alpha bumped in IDLE so the pill stands out against typical dark
    # desktop wallpapers (originally 28 was too subtle on real screens).
    "idle":       (QColor(255, 255, 255, 90),  1),
    "recording":  (QColor(124, 133, 245, 90),  1),
    "processing": (QColor(255, 255, 255, 70),  1),
    "done":       (QColor(52,  211, 153, 90),  1),
    "error":      (QColor(248, 113, 113, 85),  1),
}

_STATE_LOGO_TINT: dict[str, QColor] = {
    "idle":       QColor(255, 255, 255, 210),
    "recording":  QColor(124, 133, 245, 240),
    "processing": QColor(255, 255, 255, 180),
    "done":       QColor(52,  211, 153, 230),
    "error":      QColor(248, 113, 113, 220),
}

# Transition config: (duration_ms, easing_fn)
_TRANSITION: dict[tuple[str, str], tuple[float, object]] = {
    ("idle",       "recording"):  (280.0, ease_out_expo),
    ("recording",  "processing"): (200.0, ease_out_expo),
    ("processing", "done"):       (180.0, ease_out_expo),
    ("done",       "idle"):       (350.0, ease_in_quad),
    ("processing", "error"):      (180.0, ease_out_expo),
    ("error",      "idle"):       (350.0, ease_in_quad),
}
_DEFAULT_TRANSITION = (250.0, ease_out_expo)


def _get_transition(from_state: str, to_state: str) -> tuple[float, object]:
    return _TRANSITION.get((from_state, to_state), _DEFAULT_TRANSITION)


# ---------------------------------------------------------------------------
# Canonical pill position — shared by PillWidget and other floating widgets
# ---------------------------------------------------------------------------

def _canonical_xy(screen, width: int) -> tuple[int, int]:
    """Raw canonical-position math for ``width`` on ``screen`` — horizontally
    centered within the available (taskbar-free) area, top edge pinned at
    PILL_VERTICAL_ANCHOR of the screen's FULL height, clamped so the bottom
    edge can never fall under the taskbar. Factored out of
    PillWidget._canonical_position() so that method (which passes the
    pill's actual current width) and the module-level canonical_pill_rect()
    below (which has no live PillWidget to ask — see its docstring) compute
    the exact same numbers from the exact same formula; nothing duplicated.
    """
    full = screen.geometry()
    avail = screen.availableGeometry()
    x = avail.left() + (avail.width() - width) // 2
    y = full.top() + round(full.height() * PILL_VERTICAL_ANCHOR)
    max_y = avail.bottom() - PILL_HEIGHT + 1
    y = max(avail.top(), min(y, max_y))
    return x, y


def canonical_pill_rect(screen=None) -> QRect:
    """The QRect the pill occupies at its canonical reveal spot (2026-08-25).

    Module-level and independent of any PillWidget instance — added so
    ui/refine_widget.py and ui/correction_toast.py can anchor themselves to
    "where the pill lives" WITHOUT depending on a live pill. That matters
    because by the time those widgets appear the pill has very likely
    already gone back to IDLE and hidden itself: refine/toast stay up 8s
    and 20-30s respectively, while the pill returns to IDLE ~1.4-2.2s after
    DONE/ERROR and dismisses shortly after that.
    Anchoring to a live ``pill.geometry()`` would read stale/incorrect
    numbers once hidden, or fail outright if the pill were ever torn down.
    If the user dragged the pill during that dictation, this rect (and
    therefore the refine widget / toast) won't reflect that drag — same
    tradeoff the pill itself makes: the NEXT reveal is canonical regardless
    of any drag, so anchoring here to anything but the canonical spot would
    just be a different kind of wrong.

    Sized at PILL_WIDTH_IDLE x PILL_HEIGHT (the pill's resting footprint).
    Callers only ever need the Y/top edge in practice — X centering doesn't
    depend on width in this formula (see _canonical_xy()), so which width
    is used for sizing the rect doesn't change where "top" ends up.

    ``screen`` defaults to whichever screen is under the cursor right now —
    the same rule PillWidget itself uses — falling back to primaryScreen().
    Pass an explicit screen to keep multiple calls (e.g. a widget that both
    positions itself once and later resizes) anchored to the SAME monitor
    even if the cursor moves in between.
    """
    if screen is None:
        screen = QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
    if not screen:
        return QRect()
    x, y = _canonical_xy(screen, PILL_WIDTH_IDLE)
    return QRect(x, y, PILL_WIDTH_IDLE, PILL_HEIGHT)


# ---------------------------------------------------------------------------
# PillWidget
# ---------------------------------------------------------------------------

class PillWidget(QWidget):
    """Minimal floating pill. Logo + waveform when recording, status icons otherwise."""

    state_changed = pyqtSignal(str)

    STATE_IDLE = "idle"
    STATE_RECORDING = "recording"
    STATE_PROCESSING = "processing"
    STATE_DONE = "done"
    STATE_ERROR = "error"

    def __init__(self):
        super().__init__()
        self._state = self.STATE_IDLE
        self._prev_state = self.STATE_IDLE
        # PILL_ALWAYS_VISIBLE=true restores the pre-2026-08-24 behavior (pill
        # permanently on screen, shrunk at rest) — read once at construction,
        # same as every other config.py constant in this file.
        self._always_visible = PILL_ALWAYS_VISIBLE
        self._target_width = float(PILL_WIDTH_IDLE)
        self._current_width = float(PILL_WIDTH_IDLE)
        # Fixed horizontal anchor (center x) the pill re-centers around on every
        # width change. Deriving the center from the previous frame's geometry
        # accumulates integer-rounding error (~1px left per frame on even widths)
        # and the pill visibly crept left with use.
        self._anchor_center_x: int | None = None
        self._drag_pos: QPoint | None = None
        self._drag_start_global: QPoint | None = None

        # ---- Logo --------------------------------------------------------
        self._logo_base = QPixmap(LOGO_PATH)
        self._logo: QPixmap | None = None
        if not self._logo_base.isNull():
            self._logo_base = self._logo_base.scaled(
                LOGO_SIZE + 4, LOGO_SIZE + 4,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        self._logo_tint_anim = ColorAnimator(_STATE_LOGO_TINT[self.STATE_IDLE])
        self._rebuild_logo_pixmap(_STATE_LOGO_TINT[self.STATE_IDLE])

        # ---- State icons flags -------------------------------------------
        self._show_checkmark = False
        self._show_spinner = False
        self._show_error = False
        self._spinner_angle = 0

        # ---- Checkmark draw-progress ------------------------------------
        self._checkmark_progress = 0.0
        self._checkmark_timer = QTimer()
        self._checkmark_timer.setInterval(8)
        self._checkmark_timer.timeout.connect(self._tick_checkmark)

        # ---- Error fade-in -----------------------------------------------
        self._error_opacity = 0.0
        self._error_timer = QTimer()
        self._error_timer.setInterval(8)
        self._error_timer.timeout.connect(self._tick_error)

        # ---- Icon fade-in (for cross-fade — simplified: fade-in only) ----
        self._icon_opacity = 1.0
        self._icon_fadein_timer = QTimer()
        self._icon_fadein_timer.setInterval(8)
        self._icon_fadein_timer.timeout.connect(self._tick_icon_fadein)

        # ---- Hover / drag alpha modulation -------------------------------
        self._hover = False
        self._dragging = False
        self._hover_extra_alpha = 0          # +22 on hover
        self._hover_anim_timer = QTimer()
        self._hover_anim_timer.setInterval(8)
        self._hover_anim_timer.timeout.connect(self._tick_hover_alpha)

        # ---- Width animator ---------------------------------------------
        self._width_animator = WidthAnimator()

        # ---- Logo tint timer (shared tick with width anim) ---------------
        # The _anim_timer drives both width and logo tint per frame

        # ---- Screen-removed handler -------------------------------------
        QApplication.instance().screenRemoved.connect(self._on_screen_removed)

        # ---- Window setup -----------------------------------------------
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFixedHeight(PILL_HEIGHT)
        self.setFixedWidth(PILL_WIDTH_IDLE)

        # ---- Visualizer --------------------------------------------------
        self.visualizer = AudioVisualizer(parent=self)
        self.visualizer.setVisible(False)

        # ---- Timers ------------------------------------------------------
        self._anim_timer = QTimer()
        self._anim_timer.setInterval(16)
        self._anim_timer.timeout.connect(self._animate_frame)

        self._spinner_timer = QTimer()
        self._spinner_timer.setInterval(40)   # 25fps, 30 deg/frame
        self._spinner_timer.timeout.connect(self._animate_spinner)

        self._done_timer = QTimer()
        self._done_timer.setSingleShot(True)
        self._done_timer.timeout.connect(lambda: self.set_state(self.STATE_IDLE))

        # ---- Topmost reassertion (Windows) --------------------------------
        # Only runs while recording/processing/done/error (i.e. while the
        # pill has something time-sensitive to show); idle never fights
        # anyone for the front. Stopped when set_state returns to IDLE.
        self._topmost_timer = QTimer()
        self._topmost_timer.setInterval(1000)
        self._topmost_timer.timeout.connect(self._reassert_topmost)
        # Tracks whether we've explicitly dropped WS_EX_TOPMOST because a
        # fullscreen app/game is in front. Once a window has ever been made
        # topmost, the flag stays set until something demotes it — merely
        # skipping ensure_topmost() would NOT stop Windows from still
        # painting the pill above a plain fullscreen window. Reset to a
        # known state whenever we return to IDLE.
        self._topmost_demoted = False

        # ---- Recording elapsed-time counter (M:SS, reset each session) ---
        self._recording_start: float | None = None
        self._elapsed_text = ""
        self._elapsed_timer = QTimer()
        self._elapsed_timer.setInterval(1000)
        self._elapsed_timer.timeout.connect(self._tick_elapsed)

        # ---- Reveal/dismiss pose animation (show-only-while-dictating
        # model, 2026-08-24; "Apple-style" curves added 2026-08-25) --------
        # Drives opacity + a small vertical offset together, time-based
        # (reuses WidthAnimator as a generic scalar interpolator — the name
        # is historical, the class has no width-specific logic). Separate
        # from _screen_fadein_timer (created lazily in _on_screen_removed
        # for the disconnected-monitor case, a rare edge case left as a
        # plain fade) and from _anim_timer (which independently animates
        # width and — via _anchor_center_x — the X coordinate only); this
        # timer only ever touches windowOpacity and Y, so the two don't
        # fight over the same axis. See _start_pose_anim()/_tick_pose_anim().
        self._pose_offset_anim = WidthAnimator()
        self._pose_opacity_anim = WidthAnimator()
        self._pose_anim_timer = QTimer()
        self._pose_anim_timer.setInterval(16)
        self._pose_anim_timer.timeout.connect(self._tick_pose_anim)
        self._pose_mode: str | None = None      # "in" | "out" | None
        self._pose_baseline_y: int = 0           # Y the animated offset is added to

        self._position_on_screen()
        # Force the native HWND/NSWindow to exist and the always-on-top /
        # no-activate styles to be applied NOW, while the widget is still
        # hidden — see _ensure_native_handle()'s docstring. Without this the
        # pill's first-ever appearance would be the first real dictation,
        # which is too late to have already-correct native styles.
        self._ensure_native_handle()

    # ------------------------------------------------------------------
    # Logo pixmap helpers
    # ------------------------------------------------------------------

    def _rebuild_logo_pixmap(self, tint: QColor):
        """Recolor the logo with the given tint using SourceIn composition."""
        if self._logo_base.isNull():
            self._logo = self._logo_base
            return
        pixmap = self._logo_base.copy()
        painter = QPainter(pixmap)
        painter.setCompositionMode(
            QPainter.CompositionMode.CompositionMode_SourceIn
        )
        painter.fillRect(pixmap.rect(), tint)
        painter.end()
        self._logo = pixmap

    # ------------------------------------------------------------------
    # Screen positioning
    # ------------------------------------------------------------------

    def _canonical_position(self, screen) -> tuple[int, int]:
        """The ONE fixed reveal spot on ``screen``: horizontally centered
        (within its available/taskbar-free area), top edge pinned at
        PILL_VERTICAL_ANCHOR of the screen's FULL height (0.72 * 1080 ≈
        778px — see config.py). The fraction is measured against the full
        geometry (taskbar included in that reference number), but the
        result is then clamped against the AVAILABLE geometry so the
        pill's bottom edge can never end up under/behind the taskbar
        regardless of anchor value or taskbar thickness.

        2026-08-25: this is now the ONLY reveal position — no saved/dragged
        position is read here (see _position_on_screen()'s docstring).
        Delegates to the module-level _canonical_xy() (shared with
        canonical_pill_rect(), used by refine_widget.py/correction_toast.py
        to anchor themselves to the pill without a live instance) so there
        is exactly one place this formula is written.
        """
        return _canonical_xy(screen, int(self._current_width))

    def _position_on_screen(self):
        """Always moves to the CANONICAL position (see _canonical_position)
        on whichever screen is under the cursor right now — "where the user
        is working", the same reasoning that already applied to multi-
        monitor before 2026-08-25.

        2026-08-25: stopped reading a saved/dragged position entirely — the
        user explicitly asked for the pill to always reappear in the same
        place, not wherever a previous drag left it. Dragging is still
        allowed while the pill is on screen during a dictation (see
        mouseReleaseEvent()), it just no longer persists past that reveal.
        """
        screen = QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
        if not screen:
            return
        x, y = self._canonical_position(screen)
        self.move(x, y)
        self._anchor_center_x = x + int(self._current_width) // 2

    def _on_screen_removed(self, removed_screen):
        """If pill is on a disconnected screen, migrate to primaryScreen."""
        current_screen = QApplication.screenAt(self.pos())
        if current_screen is None or current_screen == removed_screen:
            primary = QApplication.primaryScreen()
            if primary:
                # Fade out, reposition (canonical spot), fade in
                self.setWindowOpacity(0.0)
                x, y = self._canonical_position(primary)
                self.move(x, y)
                self._anchor_center_x = x + int(self._current_width) // 2
                # Animate back in
                self._screen_fadein_step = 0
                self._screen_fadein_timer = QTimer()
                self._screen_fadein_timer.setInterval(16)
                self._screen_fadein_timer.timeout.connect(self._tick_screen_fadein)
                self._screen_fadein_timer.start()
        self._reassert_topmost()

    def _tick_screen_fadein(self):
        self._screen_fadein_step += 1
        opacity = min(1.0, self._screen_fadein_step / 12.0)  # ~200ms at 16ms/tick
        self.setWindowOpacity(opacity)
        if opacity >= 1.0:
            self._screen_fadein_timer.stop()

    # ------------------------------------------------------------------
    # Show/hide on demand (2026-08-24: hidden at rest, visible only while a
    # dictation is in flight — the tray icon is the permanent presence now)
    # ------------------------------------------------------------------

    def _apply_visibility(self, state: str):
        """Called from set_state(), BEFORE the width animator is (re)armed.

        Only handles the SHOW side synchronously, via _reveal() — which also
        snaps the width straight to the target (no 52px->N tug with the
        window already on screen) and repositions to the canonical spot
        (see _position_on_screen()). The HIDE side for a fresh IDLE is
        intentionally NOT done here: it's deferred to _animate_frame() once
        the shrink-back-to-idle animation actually finishes, which now
        triggers _start_dismiss() (the fade+sink-out pose) instead of
        hiding outright — see that method. PILL_ALWAYS_VISIBLE=true
        short-circuits all of this — the pill only ever gets shown once and
        then just shrinks/grows, matching the pre-2026-08-24 behavior
        exactly.
        """
        if self._always_visible:
            if not self.isVisible():
                self._reveal()
            return
        if state == self.STATE_IDLE:
            return  # dismiss happens in _animate_frame() once settled
        if not self.isVisible():
            self._reveal()

    def _reveal(self):
        """Bring a currently-hidden pill on screen for a new state, then
        play the ~260ms "Apple-style" entrance pose.

        Snaps _current_width to _target_width FIRST (must happen before
        _position_on_screen(), which sizes/centers off _current_width, and
        before set_state()'s width_animator.start() call right after this
        returns — with start==target the animator is a no-op, i.e. no
        visible width tug on reveal, satisfying point 4 of the 2026-08-24
        brief). Then repositions to the canonical spot and starts the
        entrance pose: opacity 0->1 while rising _REVEAL_RISE_PX from
        below into place, eased with ease_out_quint (fast start, long
        settle) over PILL_REVEAL_MS.
        """
        self._current_width = self._target_width
        self._position_on_screen()
        self.setFixedWidth(int(self._current_width))
        self._layout_children()

        baseline_y = self.y()  # canonical spot, just set by _position_on_screen()
        self.move(self.x(), baseline_y + _REVEAL_RISE_PX)
        self.setWindowOpacity(0.0)
        self.show()
        self._start_pose_anim(
            mode="in",
            baseline_y=baseline_y,
            start_offset=float(_REVEAL_RISE_PX),
            end_offset=0.0,
            start_opacity=0.0,
            end_opacity=1.0,
            duration_ms=PILL_REVEAL_MS,
            easing=ease_out_quint,
        )

    def _start_dismiss(self):
        """Kick off the ~180ms fade+sink-out pose; hides for real once it
        settles (see _tick_pose_anim()). Animates from wherever the pill
        CURRENTLY is — if it was dragged during this dictation, it sinks
        from the dragged spot, not from the canonical one; the drag never
        persists past this dismiss (see mouseReleaseEvent()),
        so the NEXT reveal is canonical regardless.
        """
        baseline_y = self.y()  # wherever the pill currently sits — canonical or dragged
        self._start_pose_anim(
            mode="out",
            baseline_y=baseline_y,
            start_offset=0.0,
            end_offset=float(_DISMISS_SINK_PX),
            start_opacity=self.windowOpacity(),
            end_opacity=0.0,
            duration_ms=PILL_DISMISS_MS,
            easing=ease_in_cubic,
        )

    def _start_pose_anim(
        self, mode, baseline_y, start_offset, end_offset,
        start_opacity, end_opacity, duration_ms, easing,
    ):
        """Arms the shared pose animator (opacity + vertical offset) used
        by both _reveal() ("in") and _start_dismiss() ("out"). ``baseline_y``
        is the fixed Y the animated offset is added to — the canonical spot
        for a reveal, or wherever the pill currently sits for a dismiss (see
        callers). Only ever touches Y + windowOpacity — X is independently
        owned by _animate_frame()/_anchor_center_x, so the two timers never
        fight over the same axis (each reads the other's current value live
        every tick instead of caching it).
        """
        self._pose_mode = mode
        self._pose_baseline_y = baseline_y
        self._pose_offset_anim.start(start_offset, end_offset, duration_ms, easing)
        self._pose_opacity_anim.start(start_opacity, end_opacity, duration_ms, easing)
        if not self._pose_anim_timer.isActive():
            self._pose_anim_timer.start()

    def _tick_pose_anim(self):
        offset, finished_offset = self._pose_offset_anim.value()
        opacity, finished_opacity = self._pose_opacity_anim.value()
        self.move(self.x(), self._pose_baseline_y + int(round(offset)))
        self.setWindowOpacity(max(0.0, min(1.0, opacity)))
        if finished_offset and finished_opacity:
            self._pose_anim_timer.stop()
            if self._pose_mode == "out":
                self.hide()
            self._pose_mode = None

    # ------------------------------------------------------------------
    # Native platform setup
    # ------------------------------------------------------------------

    def _setup_native_macos(self):
        """Configure native macOS window to float above everything without stealing focus."""
        if sys.platform != "darwin":
            return
        try:
            ns_view = objc.objc_object(c_void_p=c_void_p(self.winId().__int__()))
            ns_window = ns_view.window()
            ns_window.setLevel_(AppKit.NSFloatingWindowLevel)
            ns_window.setStyleMask_(
                ns_window.styleMask() | AppKit.NSWindowStyleMaskNonactivatingPanel
            )
            ns_window.setHidesOnDeactivate_(False)
            ns_window.setCollectionBehavior_(
                AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
                | AppKit.NSWindowCollectionBehaviorStationary
                | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary
            )
        except Exception as e:
            print(f"Warning: native macOS setup failed: {e}")

    def _setup_native_windows(self):
        """Force HWND_TOPMOST + WS_EX_NOACTIVATE/TOOLWINDOW via core.win_topmost.

        Called from _ensure_native_handle() (once, at construction, while
        the pill is still hidden) AND from showEvent() (now on EVERY reveal,
        since the pill hides/shows per dictation as of 2026-08-24 — this no
        longer fires "once in the process lifetime" as an older version of
        this comment claimed). Both call sites are cheap to repeat:
        ensure_topmost() is a single SetWindowPos with SWP_NOMOVE|NOSIZE
        (same call _reassert_topmost() already makes once a second while
        recording), and apply_noactivate_style() only issues the
        SetWindowLongPtrW write when the bits aren't already set — see its
        own docstring. _reassert_topmost() (below) additionally re-applies
        ensure_topmost() every second while recording/processing so the
        pill keeps winning the z-order fight against windows opened while
        it's on screen.
        """
        if sys.platform != "win32":
            return
        try:
            hwnd = int(self.winId())
            ensure_topmost(hwnd)
            apply_noactivate_style(hwnd)
        except Exception as e:
            print(f"Warning: native Windows setup failed: {e}")

    def _ensure_native_handle(self):
        """Force the native window (HWND on Windows, NSWindow on macOS) to
        exist and the always-on-top / no-activate styles to be applied
        immediately — WITHOUT making the window visible.

        Called once from __init__, before the pill has ever been shown.
        QWidget.winId() forces creation of the underlying platform window as
        a side effect (CreateWindowEx on Windows) but does NOT map it
        visible — that only happens on show(). This is what lets the app
        start with the pill's native styles already correct while the user
        never sees it, instead of relying on the first real show() (i.e.
        the first dictation) to be the only place this ever ran.
        showEvent() below re-applies the same setup on every subsequent
        reveal — idempotent and cheap, see _setup_native_windows()'s
        docstring.
        """
        self.winId()
        try:
            self._setup_native_macos()
        except Exception as e:
            print(f"Warning: native macOS setup failed: {e}")
        self._setup_native_windows()

    def _reassert_topmost(self):
        """Re-pin the pill above other windows — except over a fullscreen
        game/app, where it must actually be demoted to NOTOPMOST.

        WS_EX_TOPMOST, once set on a window (showEvent / any prior
        ensure_topmost call), stays set until something explicitly demotes
        it — merely not re-asserting topmost does NOT stop Windows from
        still painting the pill above a plain (non-topmost) fullscreen
        window. So while fullscreen persists we call demote_topmost(), and
        while it's gone we call ensure_topmost() as before.

        self._topmost_demoted gates the demote call so it only fires on the
        idle→fullscreen TRANSITION, not on every 1s tick while a game stays
        fullscreen — SetWindowPos on a window that's already in the right
        z-order state is unnecessary API traffic and can flicker. The
        restore path still calls ensure_topmost() every tick while not
        fullscreen (not gated) — that's the pre-existing defense against
        some other app grabbing HWND_TOPMOST in between ticks, which this
        fix must not weaken.
        """
        if sys.platform != "win32":
            return
        try:
            hwnd = int(self.winId())
            if fullscreen_app_active(exclude_hwnd=hwnd):
                if not self._topmost_demoted:
                    demote_topmost(hwnd)
                    self._topmost_demoted = True
                return
            if self._topmost_demoted:
                self._topmost_demoted = False
            ensure_topmost(hwnd)
        except Exception as e:
            print(f"Warning: reassert topmost failed: {e}")

    def showEvent(self, event):
        """Re-applies native setup on every show() — as of 2026-08-24 this
        fires once per dictation (the pill hides itself at rest), not once
        per process lifetime. Safe to repeat: see _setup_native_windows()'s
        docstring for why this is idempotent and cheap.
        """
        super().showEvent(event)
        try:
            self._setup_native_macos()
        except Exception as e:
            print(f"Warning: native macOS setup failed: {e}")
        self._setup_native_windows()

    # ------------------------------------------------------------------
    # State machine
    # ------------------------------------------------------------------

    def set_state(self, state: str):
        prev = self._state
        self._state = state
        self._prev_state = prev
        self.state_changed.emit(state)

        # Stop previous icon timers
        self._checkmark_timer.stop()
        self._error_timer.stop()
        self._show_checkmark = False
        self._show_spinner = False
        self._show_error = False
        self._spinner_timer.stop()
        # Stopped unconditionally, same as the timers above (not only in the
        # IDLE branch): a still-pending _done_timer from the PREVIOUS
        # dictation (started at DONE/ERROR, 1400/2200ms out) would otherwise
        # survive into a new RECORDING/PROCESSING that started in the
        # meantime and fire set_state(IDLE) mid-dictation — which stops
        # _topmost_timer and hides the visualizer while the user is still
        # actively recording the NEXT dictation. DONE/ERROR below restart it
        # themselves right after, so stopping it here is a no-op for them;
        # for IDLE it's just a clean stop.
        self._done_timer.stop()
        if state != self.STATE_RECORDING:
            self._elapsed_timer.stop()
            self._elapsed_text = ""

        # Determine target width
        if state == self.STATE_IDLE:
            self._target_width = float(PILL_WIDTH_IDLE)
            self.visualizer.setVisible(False)
            self.visualizer.stop()
            # At rest we don't fight anyone for the front.
            self._topmost_timer.stop()
            self._topmost_demoted = False
        elif state == self.STATE_RECORDING:
            self._target_width = float(PILL_WIDTH_RECORDING)
            self.visualizer.setVisible(True)
            self.visualizer.start()
            self._recording_start = time.monotonic()
            self._elapsed_text = "0:00"
            self._elapsed_timer.start()
            if not self._topmost_timer.isActive():
                self._topmost_timer.start()
            self._reassert_topmost()
        elif state == self.STATE_PROCESSING:
            self._target_width = float(PILL_WIDTH_STATUS)
            self._show_spinner = True
            self._spinner_timer.start()
            self.visualizer.setVisible(False)
            self.visualizer.stop()
            self._start_icon_fadein()
            if not self._topmost_timer.isActive():
                self._topmost_timer.start()
            self._reassert_topmost()
        elif state == self.STATE_DONE:
            self._target_width = float(PILL_WIDTH_STATUS)
            self._show_checkmark = True
            self.visualizer.setVisible(False)
            self.visualizer.stop()
            # Start checkmark draw animation
            self._checkmark_progress = 0.0
            self._checkmark_timer.start()
            self._done_timer.start(1400)
        elif state == self.STATE_ERROR:
            self._target_width = float(PILL_WIDTH_STATUS)
            self._show_error = True
            self.visualizer.setVisible(False)
            self.visualizer.stop()
            # Start error fade-in
            self._error_opacity = 0.0
            self._error_timer.start()
            self._done_timer.start(2200)

        # Show-on-demand (2026-08-24 hide-at-rest redesign): reveal BEFORE
        # arming the width animator below, so a pill that was hidden snaps
        # straight to this state's width/position instead of animating
        # 52px -> N with the window already visible (would read as a tug —
        # see _reveal()'s docstring). Only handles the SHOW side; hiding a
        # fresh IDLE happens later, in _animate_frame(), once its shrink
        # animation actually settles.
        self._apply_visibility(state)

        # Kick off width + logo-tint animation
        duration_ms, easing_fn = _get_transition(prev, state)
        self._width_animator.start(
            self._current_width, self._target_width, duration_ms, easing_fn
        )
        self._logo_tint_anim.animate_to(_STATE_LOGO_TINT[state])

        if not self._anim_timer.isActive():
            self._anim_timer.start()
        self.update()

    # ------------------------------------------------------------------
    # Animation ticks
    # ------------------------------------------------------------------

    def _animate_spinner(self):
        self._spinner_angle = (self._spinner_angle + 30) % 360
        self.update()

    def _tick_elapsed(self):
        if self._recording_start is None:
            return
        elapsed = int(time.monotonic() - self._recording_start)
        self._elapsed_text = f"{elapsed // 60}:{elapsed % 60:02d}"
        self.update()

    def _tick_checkmark(self):
        self._checkmark_progress += 0.1  # 10 steps x 8ms = 80ms
        if self._checkmark_progress >= 1.0:
            self._checkmark_progress = 1.0
            self._checkmark_timer.stop()
        self.update()

    def _tick_error(self):
        self._error_opacity += 0.067  # ~15 steps x 8ms = 120ms
        if self._error_opacity >= 1.0:
            self._error_opacity = 1.0
            self._error_timer.stop()
        self.update()

    def _start_icon_fadein(self):
        """Simplified cross-fade: fade in the incoming icon over 120ms."""
        self._icon_opacity = 0.0
        self._icon_fadein_timer.start()

    def _tick_icon_fadein(self):
        self._icon_opacity += 0.067  # ~15 steps x 8ms = 120ms
        if self._icon_opacity >= 1.0:
            self._icon_opacity = 1.0
            self._icon_fadein_timer.stop()
        self.update()

    def _tick_hover_alpha(self):
        target = 22 if (self._hover or self._dragging) else 0
        delta = target - self._hover_extra_alpha
        step = max(1, abs(delta) // 3)
        if abs(delta) <= step:
            self._hover_extra_alpha = target
            self._hover_anim_timer.stop()
        else:
            self._hover_extra_alpha += step if delta > 0 else -step
        self.update()

    def _animate_frame(self):
        """Single timer drives width animation and logo tint animation."""
        # Width
        value, finished = self._width_animator.value()
        self._current_width = value

        # Logo tint
        new_tint = self._logo_tint_anim.tick()
        self._rebuild_logo_pixmap(new_tint)

        if finished and not self._logo_tint_anim.is_active():
            self._anim_timer.stop()
            # Start the dismiss pose only once the shrink-back-to-idle
            # animation has fully settled — never mid-flight (no "parpadeo
            # feo", nothing left half-done). _start_dismiss() hides for
            # real once ITS animation settles (~180ms later, see
            # _tick_pose_anim()). The `not self._always_visible` check
            # keeps PILL_ALWAYS_VISIBLE=true pills on screen at rest,
            # matching the pre-2026-08-24 behavior (shrunk but visible).
            if (
                self._state == self.STATE_IDLE
                and not self._always_visible
                and self.isVisible()
                and self._pose_mode != "out"
            ):
                self._start_dismiss()

        if self._anchor_center_x is None:
            self._anchor_center_x = self.geometry().center().x()
        self.setFixedWidth(int(self._current_width))
        self.move(self._anchor_center_x - int(self._current_width) // 2, self.y())
        self._layout_children()
        self.update()

    # Reserved width for the "M:SS" elapsed-time text, right of the visualizer.
    _ELAPSED_TEXT_WIDTH = 34

    def _layout_children(self):
        w = int(self._current_width)
        h = PILL_HEIGHT
        logo_pad = 6
        logo_area = logo_pad + (LOGO_SIZE + 4) + 4
        timer_area = self._ELAPSED_TEXT_WIDTH if self._state == self.STATE_RECORDING else 0
        content_w = w - logo_area - timer_area - 4
        if content_w > 0 and self.visualizer.isVisible():
            self.visualizer.setGeometry(logo_area, 5, content_w, h - 10)

    # ------------------------------------------------------------------
    # Paint
    # ------------------------------------------------------------------

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width()
        h = self.height()
        r = float(PILL_CORNER_RADIUS)

        # --- Effective colors modulated by hover/drag ---
        bg = _STATE_BG.get(self._state, _STATE_BG["idle"])
        border_color, border_width = _STATE_BORDER.get(
            self._state, _STATE_BORDER["idle"]
        )

        # Hover: border alpha +22; drag: bg alpha→200, border alpha +20 more
        if self._dragging:
            bg = QColor(bg.red(), bg.green(), bg.blue(), 200)
            extra = self._hover_extra_alpha + 20
        else:
            extra = self._hover_extra_alpha

        if extra > 0:
            ba = min(255, border_color.alpha() + extra)
            border_color = QColor(
                border_color.red(), border_color.green(), border_color.blue(), ba
            )

        # --- Shadow halo (1px outer rect, painted before main bg) ---
        shadow_rect_f = (
            -1.0, -1.0, float(w) + 2.0, float(h) + 2.0
        )
        shadow_path = QPainterPath()
        shadow_path.addRoundedRect(*shadow_rect_f, r + 1, r + 1)
        painter.fillPath(shadow_path, QColor(0, 0, 0, 80))

        # --- Background ---
        path = QPainterPath()
        path.addRoundedRect(0.0, 0.0, float(w), float(h), r, r)
        painter.fillPath(path, bg)

        # --- Border ring (1px, interior) ---
        pen = QPen(border_color, border_width)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        # Inset by 0.5px so the 1px stroke is fully inside the pill shape.
        # PyQt6 requires QRectF when x/y/w/h are floats — the 6-arg overload
        # only accepts ints for the first four.
        painter.drawRoundedRect(
            QRectF(0.5, 0.5, float(w) - 1.0, float(h) - 1.0),
            r - 0.5, r - 0.5,
        )

        # --- Logo (centered in IDLE, anchored left in any other state where
        # the rest of the pill is occupied by visualizer/icons) ---
        if self._logo and not self._logo.isNull():
            logo_rendered = LOGO_SIZE + 4  # the QPixmap was scaled to this size
            if self._state == self.STATE_IDLE:
                lx = (w - logo_rendered) // 2
            else:
                lx = 5
            ly = (h - logo_rendered) // 2
            painter.drawPixmap(lx, ly, self._logo)

        # --- Recording elapsed-time counter (M:SS), right-aligned ---
        if self._state == self.STATE_RECORDING and self._elapsed_text:
            font = QFont(painter.font())
            font.setPointSizeF(9.5)
            painter.setFont(font)
            painter.setPen(QColor(255, 255, 255, 200))
            text_rect = QRectF(float(w - self._ELAPSED_TEXT_WIDTH - 8), 0.0, float(self._ELAPSED_TEXT_WIDTH), float(h))
            painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, self._elapsed_text)

        # --- Status icons ---
        # Icon center: right of logo, centered in the remaining pill area
        logo_right = 6 + LOGO_SIZE + 4
        icon_cx = logo_right + (w - logo_right - 4) // 2
        icon_cy = h // 2

        if self._show_checkmark:
            self._paint_checkmark(painter, icon_cx, icon_cy)

        elif self._show_spinner:
            self._paint_spinner(painter, icon_cx, icon_cy)

        elif self._show_error:
            self._paint_error(painter, icon_cx, icon_cy)

        painter.end()

    def _paint_spinner(self, painter: QPainter, cx: int, cy: int):
        """6 radial ellipses with decaying alpha, modulated by icon_opacity."""
        painter.setPen(Qt.PenStyle.NoPen)
        alphas = [240, 195, 145, 95, 55, 20]
        for i in range(6):
            angle = math.radians(self._spinner_angle + i * 60)
            dx = 5 * math.cos(angle)
            dy = 5 * math.sin(angle)
            alpha = int(alphas[i] * self._icon_opacity)
            painter.setBrush(QColor(255, 255, 255, max(alpha, 0)))
            painter.drawEllipse(int(cx + dx) - 1, int(cy + dy) - 1, 2, 2)

    def _paint_checkmark(self, painter: QPainter, cx: int, cy: int):
        """Draws checkmark progressively over 80ms using ease_out_expo."""
        t = self._checkmark_progress
        # ease_out_expo
        t_e = ease_out_expo(min(t, 1.0))

        # Checkmark bounding: 10x7, centered at (cx, cy)
        bx = cx - 5
        by = cy - 3
        # Segment points relative to bounding box top-left
        p0 = (bx + 1,  by + 5)
        p1 = (bx + 4,  by + 8)
        p2 = (bx + 10, by + 2)

        pen = QPen(QColor(255, 255, 255, 240), 1.5)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)

        if t_e < 0.5:
            # Draw first segment partially: p0 → p1
            frac = t_e * 2.0
            ix = p0[0] + (p1[0] - p0[0]) * frac
            iy = p0[1] + (p1[1] - p0[1]) * frac
            painter.drawLine(
                int(round(p0[0])), int(round(p0[1])),
                int(round(ix)),    int(round(iy))
            )
        else:
            # First segment complete
            painter.drawLine(p0[0], p0[1], p1[0], p1[1])
            # Second segment partially: p1 → p2
            frac = (t_e - 0.5) * 2.0
            ix = p1[0] + (p2[0] - p1[0]) * frac
            iy = p1[1] + (p2[1] - p1[1]) * frac
            painter.drawLine(
                p1[0], p1[1],
                int(round(ix)), int(round(iy))
            )

    def _paint_error(self, painter: QPainter, cx: int, cy: int):
        """Draws X cross with fade-in. Color: danger red."""
        alpha = int(230 * self._error_opacity)
        pen = QPen(QColor(248, 113, 113, alpha), 1.5)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        # 9x9 centered cross
        x0 = cx - 4
        y0 = cy - 4
        x1 = cx + 4
        y1 = cy + 4
        painter.drawLine(x0, y0, x1, y1)
        painter.drawLine(x1, y0, x0, y1)

    # ------------------------------------------------------------------
    # Mouse events (drag + hover)
    # ------------------------------------------------------------------

    def enterEvent(self, event):
        self._hover = True
        if not self._hover_anim_timer.isActive():
            self._hover_anim_timer.start()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover = False
        if not self._hover_anim_timer.isActive():
            self._hover_anim_timer.start()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            self._drag_start_global = event.globalPosition().toPoint()
            self._dragging = True
            if not self._hover_anim_timer.isActive():
                self._hover_anim_timer.start()
            event.accept()

    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.MouseButton.LeftButton and self._drag_pos:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            self._anchor_center_x = self.x() + self.width() // 2
            event.accept()

    def mouseReleaseEvent(self, event):
        # 2026-08-25: dragging is still allowed while the pill is on screen
        # during a dictation (moving a window that's visible is reasonable),
        # but the position is deliberately NOT persisted anymore — the user
        # asked for the pill to always reappear at the canonical spot (see
        # _canonical_position()), not wherever a previous drag left it. So
        # this just resets drag state; nothing is written to disk.
        self._drag_pos = None
        self._drag_start_global = None
        self._dragging = False
        if not self._hover_anim_timer.isActive():
            self._hover_anim_timer.start()

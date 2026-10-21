"""System tray icon with context menu for fluentpill (formerly Howl)."""

import webbrowser
from PyQt6.QtWidgets import QSystemTrayIcon, QMenu
from PyQt6.QtGui import QIcon, QAction
from PyQt6.QtCore import pyqtSignal
from config import (
    LOGO_PATH,
    CLEANER_ENABLED,
    TRAY_ICON_DARK_TASKBAR,
    TRAY_ICON_LIGHT_TASKBAR,
)
from core.branding import APP_NAME


def taskbar_is_light() -> bool:
    """True when Windows draws the taskbar/tray in light mode.

    Reads SystemUsesLightTheme (the taskbar setting, not the app theme).
    Any failure (non-Windows, key missing) means "dark", the default, so the
    cyan icon is used.
    """
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as key:
            value, _ = winreg.QueryValueEx(key, "SystemUsesLightTheme")
        return bool(value)
    except Exception:
        return False


def load_tray_icon() -> QIcon:
    """The brand icon for the current taskbar theme, falling back to the
    plain logo if the .ico is missing."""
    import os
    path = TRAY_ICON_LIGHT_TASKBAR if taskbar_is_light() else TRAY_ICON_DARK_TASKBAR
    if os.path.exists(path):
        return QIcon(path)
    return QIcon(LOGO_PATH)


class TrayIcon(QSystemTrayIcon):
    """System tray icon — shows fluentpill is running, provides quick actions."""

    toggle_recording = pyqtSignal()

    def __init__(self, app_controller, parent=None):
        super().__init__(parent)
        self._app = app_controller
        self._is_recording = False
        # In-memory cleaner toggle — starts from config value
        self._cleaner_enabled = CLEANER_ENABLED
        # Watchdog health (2026-09-05) — see set_watchdog_stale(). Tracked
        # separately from the state label so a pill-state change (idle ->
        # recording -> ...) never wipes out a standing watchdog warning.
        self._watchdog_stale = False
        self._current_state_label = f"{APP_NAME} — Voice to Text"

        self.setIcon(load_tray_icon())
        self.setToolTip(self._current_state_label)

        # Left-click and double-click both toggle
        self.activated.connect(self._on_activated)

        # Build context menu (right-click)
        menu = QMenu()

        self._status_action = QAction(f"{APP_NAME} — Idle")
        self._status_action.setEnabled(False)
        menu.addAction(self._status_action)
        menu.addSeparator()

        # Toggle recording button in menu too
        self._record_action = QAction("Grabar (manos libres)")
        self._record_action.triggered.connect(self._on_record_clicked)
        menu.addAction(self._record_action)

        dashboard_action = QAction("Abrir Dashboard")
        dashboard_action.triggered.connect(lambda: webbrowser.open("http://localhost:5000"))
        menu.addAction(dashboard_action)

        menu.addSeparator()

        self._shortcuts_action = QAction(
            "Ctrl+Shift (hold) | Ctrl+Space (toggle) | Ctrl+Shift+R (raw repaste)"
        )
        self._shortcuts_action.setEnabled(False)
        menu.addAction(self._shortcuts_action)

        menu.addSeparator()

        # Cleaner toggle
        self._cleaner_action = QAction(self._cleaner_label())
        self._cleaner_action.triggered.connect(self._on_cleaner_toggle)
        menu.addAction(self._cleaner_action)

        menu.addSeparator()

        quit_action = QAction("Salir")
        quit_action.triggered.connect(self._quit)
        menu.addAction(quit_action)

        self.setContextMenu(menu)

    def _on_activated(self, reason):
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.toggle_recording.emit()

    def _on_record_clicked(self):
        self.toggle_recording.emit()

    def update_state(self, state: str):
        """Update tray tooltip, status text, and record button label."""
        self._is_recording = state == "recording"
        labels = {
            "idle": f"{APP_NAME} — Idle",
            "recording": f"{APP_NAME} — Grabando...",
            "processing": f"{APP_NAME} — Transcribiendo...",
            "done": f"{APP_NAME} — Listo",
            "error": f"{APP_NAME} — Error",
        }
        label = labels.get(state, APP_NAME)
        self._status_action.setText(label)
        self._current_state_label = label
        self._refresh_tooltip()

        # Update record button text
        if self._is_recording:
            self._record_action.setText("Parar grabación")
        else:
            self._record_action.setText("Grabar (manos libres)")

    def set_watchdog_stale(self, is_stale: bool) -> None:
        """Reflect watchdog health in the tray tooltip — the tray icon is
        Howl's permanent on-screen presence (tooltip/click state survive
        the pill hiding itself at rest), so it's where a
        "nobody is watching the watchdog" warning has to surface; a
        howl.log line nobody reads is not an alert on its own. Called from
        main.py's existing heartbeat timer (HowlApp._check_watchdog_health),
        never a new timer of its own. No-op when the value doesn't change,
        so repeated heartbeat ticks don't keep re-setting the same tooltip.
        """
        if is_stale == self._watchdog_stale:
            return
        self._watchdog_stale = is_stale
        self._refresh_tooltip()

    def _refresh_tooltip(self) -> None:
        """Single place that renders the tooltip, so the watchdog warning
        (see set_watchdog_stale) survives every ordinary state change
        instead of being overwritten by the next update_state() call."""
        label = self._current_state_label
        if self._watchdog_stale:
            label = f"{label} — watchdog sin pasar"
        self.setToolTip(label)

    def _cleaner_label(self) -> str:
        state = "ON" if self._cleaner_enabled else "OFF"
        return f"Limpieza con IA: {state}"

    def _on_cleaner_toggle(self):
        """Toggle the LLM cleaner on/off at runtime (in-memory only)."""
        import core.cleaner as cleaner_module
        import config as cfg
        self._cleaner_enabled = not self._cleaner_enabled
        # Patch the config value so cleaner.clean_dictation() reads the new state
        cfg.CLEANER_ENABLED = self._cleaner_enabled
        self._cleaner_action.setText(self._cleaner_label())
        state_str = "activada" if self._cleaner_enabled else "desactivada"
        print(f"[tray] Limpieza con IA {state_str}")

    def _quit(self):
        # User-initiated stop — mark it so howl-watchdog.ps1 doesn't
        # relaunch Howl a few minutes later (mirrors what howl-stop.bat does
        # for the command-line stop path), and record the shutdown reason so
        # main.py's atexit line says "tray" instead of "unknown". Never
        # raises.
        from core.lifecycle import mark_stopped_by_user, mark_shutdown_reason
        mark_shutdown_reason("tray")
        mark_stopped_by_user()
        from PyQt6.QtWidgets import QApplication
        QApplication.quit()

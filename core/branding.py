r"""Brand names in one place.

The app was called "Howl" and is now "fluentpill" everywhere: visible name,
data folder (%LOCALAPPDATA%\fluentpill), fluentpill.log, mutex, scheduled
tasks and scripts. core/lifecycle.py migrates the old Howl folder once.

"Howl" survives only as LEGACY_APP_NAME: a window title or process name that
still says "Howl" (an old build still running during the switch, an old
shortcut) must keep counting as our own app.

Anything that decides "is this window/process my own app?" by name must go
through is_own_app_name(), which knows both names, so renaming a visible
title never silently breaks that check.
"""

APP_NAME = "fluentpill"
LEGACY_APP_NAME = "Howl"
OWN_APP_NAMES = (APP_NAME, LEGACY_APP_NAME)


def is_own_app_name(name: "str | None") -> bool:
    """True if `name` (a window title or process name) is this app, under
    either its current or its legacy name. Case-insensitive; surrounding
    whitespace is ignored."""
    if not name:
        return False
    return name.strip().casefold() in {n.casefold() for n in OWN_APP_NAMES}

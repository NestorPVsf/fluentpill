r"""Brand names in one place.

The app was called "Howl" and is now shown to the user as "fluentpill".
Internal identifiers (%LOCALAPPDATA%\Howl, howl.log, mutex names, the
watchdog, file and module names) keep the old name on purpose, so history,
autostart and the watchdog keep working.

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

"""Verificacion de la maquina de estados del silenciador de juegos.

No arranca micro, ni red, ni ventanas: sustituye el detector de pantalla
completa por una constante y alimenta eventos de tecla a mano.

    venv\\Scripts\\python.exe tools\\verify_game_mute.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtCore import QCoreApplication  # noqa: E402
from pynput import keyboard  # noqa: E402

import config  # noqa: E402
import core.hotkey as hk  # noqa: E402

_app = QCoreApplication.instance() or QCoreApplication(sys.argv)

VK_Z = 90
VK_R = 82


class _Key:
    """Tecla de letra falsa: hotkey.py mira .vk cuando hay modificadores."""

    def __init__(self, vk):
        self.vk = vk


class Probe:
    """Un HotkeyListener aislado, con detector y Ctrl fisico simulados."""

    def __init__(self, muted, flag_enabled=True):
        config.GAME_MUTE_ENABLED = flag_enabled
        hk.fullscreen_app_active = lambda *a, **k: muted
        self.listener = hk.HotkeyListener()
        # _ctrl_active() consulta al SO por el Ctrl fisico; aqui basta el flag.
        self.listener._ctrl_active = lambda: self.listener._ctrl_held
        # _key_down() (GetAsyncKeyState) tambien consulta al SO: el hold exige
        # estado fisico real de Ctrl y Shift (e06b8ab). Aqui, las banderas.
        self.listener._key_down = lambda vk: (
            self.listener._ctrl_held if vk == 0x11 else self.listener._shift_held
        )
        self.events = []
        self.listener.pressed.connect(lambda: self.events.append("pressed"))
        self.listener.released.connect(lambda: self.events.append("released"))
        self.listener.repaste.connect(lambda: self.events.append("repaste"))
        self.listener.raw_repaste.connect(lambda: self.events.append("raw_repaste"))

    def set_muted(self, muted):
        hk.fullscreen_app_active = lambda *a, **k: muted

    def press(self, key):
        self.listener._on_press(key)

    def release(self, key):
        self.listener._on_release(key)


CTRL = keyboard.Key.ctrl_l
SHIFT = keyboard.Key.shift
ALT = keyboard.Key.alt_l
SPACE = keyboard.Key.space

_fallos = []


def check(nombre, condicion, detalle=""):
    if condicion:
        print(f"  OK   {nombre}")
    else:
        print(f"  FALLO {nombre}  {detalle}")
        _fallos.append(nombre)


def _tray_toggle_source():
    """Texto del metodo _on_tray_toggle de main.py, leido como fichero.

    No se importa main.py a proposito: al importarlo redirige stdout a un
    fichero de log y arranca cosas que este arnes no quiere.
    """
    ruta = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py"
    )
    src = open(ruta, encoding="utf-8").read()
    ini = src.find("def _on_tray_toggle")
    if ini == -1:
        return ""
    fin = src.find("\n    @", ini)
    return src[ini:fin] if fin != -1 else src[ini:]


def main():
    print("Silenciador de juegos - verificacion de la maquina de estados")
    print()

    print("[silenciado] los cinco disparadores no deben disparar")
    p = Probe(muted=True)
    p.press(CTRL)
    p.press(SHIFT)
    check("1. hold (Ctrl+Shift) no arranca", p.events == [], p.events)

    p = Probe(muted=True)
    p.press(CTRL)
    p.press(SPACE)
    check("2. manos libres (Ctrl+Space) no arranca", p.events == [], p.events)

    p = Probe(muted=True)
    p.press(ALT)
    p.press(SHIFT)
    p.press(_Key(VK_Z))
    check("3. repegado limpio (Alt+Shift+Z) no dispara", p.events == [], p.events)

    p = Probe(muted=True)
    p.press(CTRL)
    p.press(SHIFT)
    p.press(_Key(VK_R))
    check("4. repegado crudo (Ctrl+Shift+R) no dispara", p.events == [], p.events)

    # El quinto: Space convierte un hold en manos libres, y entonces soltar
    # las teclas ya NO para. Es el bug que encontraron Sol y Kimi.
    p = Probe(muted=False)
    p.press(CTRL)
    p.press(SHIFT)
    check("   (preparacion) el hold arranca sin juego", p.events == ["pressed"], p.events)
    p.set_muted(True)
    p.press(SPACE)
    check(
        "5. Space no enclava el hold en manos libres",
        p.listener._hands_free is False and p.events == ["pressed", "released"],
        f"_hands_free={p.listener._hands_free} events={p.events}",
    )

    print()
    print("[regla de oro] salir de una grabacion nunca se bloquea")
    # Listener LIMPIO a proposito: la comprobacion 5 ya paro esa grabacion al
    # caer Space en la rama de parada, asi que reutilizarla daria un verde
    # falso - veria el "released" de la 5 en vez del que produce el release.
    p = Probe(muted=False)
    p.press(CTRL)
    p.press(SHIFT)
    p.set_muted(True)
    p.release(CTRL)
    check(
        "6. soltar Ctrl para el hold con juego delante",
        p.events == ["pressed", "released"],
        p.events,
    )

    p = Probe(muted=False)
    p.press(CTRL)
    p.press(SPACE)
    p.release(SPACE)
    p.set_muted(True)
    p.press(SPACE)
    check(
        "7. Ctrl+Space para manos libres con juego delante",
        p.events == ["pressed", "released"],
        p.events,
    )

    print()
    print("[sin juego] nada cambia")
    p = Probe(muted=False)
    p.press(CTRL)
    p.press(SHIFT)
    p.release(CTRL)
    check("8. hold normal arranca y para", p.events == ["pressed", "released"], p.events)

    p = Probe(muted=False)
    p.press(ALT)
    p.press(SHIFT)
    p.press(_Key(VK_Z))
    check("9. repegado normal dispara", p.events == ["repaste"], p.events)

    print()
    print("[bandera apagada] GAME_MUTE_ENABLED=false ignora el detector")
    p = Probe(muted=True, flag_enabled=False)
    p.press(CTRL)
    p.press(SHIFT)
    check("10. con la bandera apagada, el hold arranca", p.events == ["pressed"], p.events)
    config.GAME_MUTE_ENABLED = True

    print()
    print("[bandeja] una grabacion externa se puede parar con el teclado")
    p = Probe(muted=True)
    p.listener.sync_external_start()
    p.press(CTRL)
    p.press(SPACE)
    check(
        "11. Ctrl+Space para una grabacion de bandeja con juego delante",
        p.events == ["released"],
        p.events,
    )

    p = Probe(muted=True)
    p.listener.sync_external_start()
    p.press(CTRL)
    p.release(CTRL)
    check(
        "12. un toque suelto de Ctrl no para una grabacion de bandeja",
        p.events == [],
        p.events,
    )

    print()
    print("[sin juego] el ascenso a manos libres debe seguir funcionando")
    p = Probe(muted=False)
    p.press(CTRL)
    p.press(SHIFT)
    p.press(SPACE)
    check(
        "13. Space asciende el hold a manos libres",
        p.listener._hands_free is True and p.events == ["pressed"],
        f"_hands_free={p.listener._hands_free} events={p.events}",
    )
    p.release(CTRL)
    check(
        "14. tras el ascenso, soltar Ctrl ya no para",
        p.events == ["pressed"],
        p.events,
    )

    print()
    print("[cableado] main.py debe sincronizar el estado del listener")
    src = _tray_toggle_source()
    # Fuera las lineas comentadas: una llamada comentada no es cableado.
    vivo = "\n".join(
        l for l in src.splitlines() if not l.lstrip().startswith("#")
    )
    orden_ok = (
        "sync_external_stop()" in vivo
        and "sync_external_start()" in vivo
        and "else:" in vivo
        and vivo.index("sync_external_stop()")
        < vivo.index("else:")
        < vivo.index("sync_external_start()")
    )
    check(
        "15. _on_tray_toggle sincroniza, y cada llamada en su rama",
        orden_ok,
        "el cableado de la bandeja falta, esta comentado o tiene las ramas cambiadas",
    )

    print()
    print("[RegisterHotKey] la ruta WM_HOTKEY hace lo mismo que el hook")
    reenviados = []
    hk.HotkeyListener._replay_space = staticmethod(lambda: reenviados.append(1))

    def rh(muted):
        q = Probe(muted=muted)
        q.listener._rh_active = True  # como tras un registro correcto
        return q

    q = rh(False)
    q.listener._on_win_hotkey(0)
    q.listener._on_win_hotkey(0)
    check(
        "16. Ctrl+Space alterna: arranca y para",
        q.events == ["pressed", "released"] and not reenviados,
        f"{q.events} reenviados={reenviados}",
    )

    q = rh(True)
    q.listener._on_win_hotkey(0)
    check(
        "17. con juego delante no arranca y devuelve Space a la app",
        q.events == [] and reenviados == [1],
        f"{q.events} reenviados={reenviados}",
    )

    del reenviados[:]
    q = rh(False)
    q.listener._on_win_hotkey(0)
    q.set_muted(True)
    q.listener._on_win_hotkey(0)
    check(
        "18. con juego delante PARAR no se bloquea nunca",
        q.events == ["pressed", "released"] and not reenviados,
        f"{q.events} reenviados={reenviados}",
    )

    q = rh(False)
    q.press(CTRL)
    q.press(SHIFT)
    q.listener._on_win_hotkey(0)
    q.release(CTRL)
    check(
        "19. Ctrl+Shift+Space asciende el hold y soltar ya no para",
        q.events == ["pressed"] and q.listener._hands_free is True,
        f"{q.events} hands_free={q.listener._hands_free}",
    )

    q = rh(False)
    q.press(CTRL)
    q.press(SPACE)
    check(
        "20. con RegisterHotKey activo el hook no duplica el toggle",
        q.events == [],
        q.events,
    )

    q = rh(False)
    q.listener.suspend()
    q.listener._on_win_hotkey(0)
    check(
        "21. suspendido (pegando) no arranca y devuelve Space",
        q.events == [] and reenviados == [1],
        f"{q.events} reenviados={reenviados}",
    )

    print()
    if _fallos:
        print(f"FALLARON {len(_fallos)}: {', '.join(_fallos)}")
        return 1
    print("Todo correcto.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

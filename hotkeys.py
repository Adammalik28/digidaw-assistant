"""Global hotkeys that work from any app (talk / interrupt Digidaw, show the HUD).

The implementation is per platform (hotkeys_win.py: RegisterHotKey / hotkeys_linux.py: Hyprland binds);
`import hotkeys` gives the right one.
"""
import os
import sys

if os.name == "nt":
    import hotkeys_win as _impl
else:
    import hotkeys_linux as _impl

sys.modules[__name__] = _impl

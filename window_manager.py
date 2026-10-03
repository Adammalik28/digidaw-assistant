"""Window management skills: switch to, minimize, maximize, snap and gracefully close windows.

The implementation is per platform (window_manager_win.py: user32 / window_manager_linux.py: Hyprland);
`import window_manager` gives the right one, private helpers included.
"""
import os
import sys

if os.name == "nt":
    import window_manager_win as _impl
else:
    import window_manager_linux as _impl

sys.modules[__name__] = _impl

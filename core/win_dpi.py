"""Per-monitor DPI awareness, set before any window or screenshot exists.

Windows has two ideas of a pixel. A process that is not per-monitor aware is
handed logical coordinates, so on a desktop scaled to 125% or 150% a click
aimed at a physical pixel lands somewhere else. The screen loop in
``actions/screen_drive.py`` captures physical pixels and injects input against
that same grid. This has to be switched on before Qt, pyautogui or a screen
DC is touched, because the process locks its awareness on the first one.
"""
from __future__ import annotations

import sys

# DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
_PER_MONITOR_V2 = -4
_PROCESS_PER_MONITOR_DPI_AWARE = 2


def enable() -> bool:
    """Ask for per-monitor V2. Safe to call more than once. Never raises.

    Returns True when this process is per-monitor aware afterwards, including
    the case where something earlier (Qt, on a normal launch) already was.
    """
    if sys.platform != "win32":
        return False
    import ctypes

    user32 = ctypes.windll.user32
    try:
        fn = user32.SetProcessDpiAwarenessContext
        fn.restype = ctypes.c_bool
        fn.argtypes = [ctypes.c_void_p]
        fn(ctypes.c_void_p(_PER_MONITOR_V2))
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(_PROCESS_PER_MONITOR_DPI_AWARE)
        except Exception:
            pass
    return is_per_monitor()


def is_per_monitor() -> bool:
    """True when this thread reports per-monitor awareness (V1 or V2)."""
    if sys.platform != "win32":
        return False
    import ctypes

    try:
        user32 = ctypes.windll.user32
        user32.GetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        user32.GetAwarenessFromDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.GetAwarenessFromDpiAwarenessContext.restype = ctypes.c_int
        ctx = user32.GetThreadDpiAwarenessContext()
        # DPI_AWARENESS_PER_MONITOR_AWARE == 2. V2 reports the same value.
        return int(user32.GetAwarenessFromDpiAwarenessContext(ctx)) >= 2
    except Exception:
        return False

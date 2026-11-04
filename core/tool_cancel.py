"""A running tool can see that the user pressed interrupt.

The voice loop waits inside the tool. The interrupt button used to drain the
speaker and stop a screen task, and a scan that was already pinging kept the
microphone unanswered until it finished and the model called it again.
"""

from __future__ import annotations

import threading

_cancel = threading.Event()


def request() -> None:
    _cancel.set()


def clear() -> None:
    _cancel.clear()


def cancelled() -> bool:
    return _cancel.is_set()

"""
Small async helpers shared by GUI code.

The DICOM Sync dialogs run blocking C-FIND queries on a daemon
thread and route the result back to the main thread.  This module
captures that recurring pattern so it isn't reinvented per call
site.  Currently used by
:mod:`gui.filter_groups_dialog` for the institution discovery
query; other background flows (``main_window._test_echo``) still
spawn their own thread + Qt-signal pair for historical reasons.
"""

import logging
import threading
import weakref
from typing import Any, Callable

from PySide6.QtCore import QObject, Signal

# ``Any`` covers the result type — the helper doesn't try to be
# generic across job/result types since Qt's signal-based hop carries
# its payload as ``object`` anyway.

logger = logging.getLogger("dicom_sync")


class _CallbackRelay(QObject):
    """Hidden QObject owned by ``run_in_background`` whose
    ``delivered`` signal is emitted from the worker thread.

    Because the relay is parented to *owner* (which lives on the GUI
    thread), Qt.AutoConnection turns the cross-thread emit into a
    QueuedConnection — Qt dispatches the slot back on the relay's
    owning thread.  This is how the helper guarantees the callback
    runs on the GUI thread regardless of what kind of callable the
    caller passes (plain method, lambda, or Signal.emit).
    """

    delivered = Signal(object)

    def __init__(self, on_done: Callable[[Any], None], parent: QObject) -> None:
        super().__init__(parent)
        # Connect explicitly to be sure the slot runs on the relay's
        # owning thread even when the connection is established from
        # the worker.
        self.delivered.connect(self._invoke)
        self._on_done = on_done

    def _invoke(self, result: object) -> None:
        try:
            self._on_done(result)
        except Exception as e:
            logger.error(
                f"async_helpers: callback raised: {e}")
        finally:
            # One relay per ``run_in_background`` call; tear it down
            # after the slot has run so they don't accumulate as
            # zombie children on long-lived owners (MainWindow et al).
            self.deleteLater()


def run_in_background(owner: QObject,
                      job: Callable[[], Any],
                      on_done: Callable[[Any], None],
                      *,
                      label: str = "") -> threading.Thread:
    """Run ``job()`` on a daemon thread and call ``on_done(result)``
    back on *owner*'s thread once the job finishes.

    The cross-thread hop is performed by emitting an internal
    ``Signal`` from the worker thread; Qt then queues the slot on
    *owner*'s event loop.  This means *on_done* may be any callable
    (a method that touches widgets, a lambda, or a Signal.emit) and
    it will execute on *owner*'s thread without the caller needing
    to know about Qt threading semantics.

    *owner* is held via a weak reference (in addition to the
    QObject parenting) so a dialog that gets closed mid-run does
    not prevent garbage collection (which historically caused
    crashes when the QDialog destructor ran on the worker thread).

    Exceptions inside *job* are logged and dropped; ``on_done`` is
    only called on success.  Callers needing failure feedback should
    return a tagged result themselves (e.g. ``(ok, payload)``).
    """
    weak_owner = weakref.ref(owner)
    relay = _CallbackRelay(on_done, owner)

    def run() -> None:
        try:
            result = job()
        except Exception as e:
            logger.error(
                f"async_helpers.run_in_background"
                f"{(' (' + label + ')') if label else ''} failed: {e}")
            return
        if weak_owner() is None:
            # Owner was destroyed while the job was running — drop.
            # The relay is parented to owner so Qt already reaped it.
            return
        # Signal.emit from a worker thread to a slot on the GUI
        # thread is automatically marshalled by Qt (queued).
        # ``RuntimeError`` covers the narrow TOCTOU window where the
        # owner (and therefore the relay) gets destroyed between the
        # check above and this emit; PySide raises it as
        # "Signal source has been deleted".
        try:
            relay.delivered.emit(result)
        except RuntimeError:
            return

    thread = threading.Thread(target=run, daemon=True,
                              name=f"async-{label or 'job'}")
    thread.start()
    return thread

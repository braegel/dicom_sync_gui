"""
Built-in Storage SCP for receiving DICOM images.
Used as fallback when no external DICOM server is available.
"""

import inspect
import logging
import os
import threading
from typing import Optional

from PySide6.QtCore import QObject, Signal

from pydicom import Dataset
from pynetdicom import AE, evt, StoragePresentationContexts
from pynetdicom.sop_class import Verification

logger = logging.getLogger("dicom_sync")

# pydicom 3.x renamed Dataset.save_as(write_like_original=False) to
# save_as(enforce_file_format=True); the old keyword is deprecated and
# slated for removal.  Both spellings mean the same thing here: write a
# standards-conformant Part 10 file (128-byte preamble + "DICM" magic +
# File Meta Information).  Detect which keyword the installed pydicom
# accepts ONCE at import time -- handle_store runs on the pynetdicom
# reactor thread for every received image, so a per-call try/except
# would add avoidable overhead to the hot path.
_SAVE_AS_KWARGS = (
    {"enforce_file_format": True}
    if "enforce_file_format" in inspect.signature(Dataset.save_as).parameters
    else {"write_like_original": False}
)

# handle_store runs on the pynetdicom reactor thread for EVERY received
# image.  Emitting a queued cross-thread Qt signal per image floods the
# GUI thread's event queue during a large fallback transfer and pins it
# at 100% CPU (the UI freezes).  Emit only the first image and then every
# Nth, which is all the throttled progress-log handler needs anyway.
_IMAGE_SIGNAL_EVERY = 25


class StorageSCP(QObject):
    """Built-in DICOM Storage SCP.

    `image_received` is emitted from the pynetdicom reactor thread
    after a successful C-STORE — THROTTLED to the first image and then
    every ``_IMAGE_SIGNAL_EVERY``th (a per-image emit floods the GUI
    event queue on a large transfer and freezes the UI).  Its payload is
    the running received-count, not the dataset: marshalling a full
    Dataset across threads per image was both wasteful and unused by the
    GUI (the handler only reports the count).  Qt marshals the signal to
    the main thread under the default Qt.AutoConnection, so slots can
    safely touch widgets."""

    image_received = Signal(int)

    def __init__(self, ae_title: str, port: int, storage_path: str,
                 bind_address: str = "0.0.0.0"):
        super().__init__()
        self.ae_title = ae_title
        self.port = port
        self.bind_address = bind_address
        self.storage_path = storage_path
        self.ae = None
        # ``_running_flag`` is read/written from both the reactor thread
        # and the caller (GUI) thread; access goes through ``_run_lock``
        # so ``stop()`` cannot observe a stale True after the reactor
        # has already set it False and torn the AE down.
        self._running_flag = False
        self._run_lock = threading.Lock()
        self._lock = threading.Lock()  # protects _images_received
        self._images_received = 0
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        os.makedirs(storage_path, exist_ok=True)

    @property
    def running(self) -> bool:
        with self._run_lock:
            return self._running_flag

    @running.setter
    def running(self, value: bool):
        # Setter kept so existing test mocks that assign ``.running``
        # continue to work; thread-safety only matters on real instances.
        with self._run_lock:
            self._running_flag = bool(value)

    @property
    def images_received(self) -> int:
        with self._lock:
            return self._images_received

    def handle_store(self, event):
        ds = event.dataset
        ds.file_meta = event.file_meta
        try:
            filepath = os.path.join(self.storage_path, f"{ds.SOPInstanceUID}.dcm")
            # Conformant Part 10 write; keyword chosen at import time
            # (see _SAVE_AS_KWARGS) to stay compatible with pydicom 2.x
            # ("write_like_original") and 3.x ("enforce_file_format").
            ds.save_as(filepath, **_SAVE_AS_KWARGS)
            with self._lock:
                self._images_received += 1
                count = self._images_received
            # Throttle the cross-thread emit: first image + every Nth.
            # The reactor thread can store images far faster than the GUI
            # thread drains queued signals, so an unthrottled per-image
            # emit floods the event queue and freezes the UI.
            if count == 1 or count % _IMAGE_SIGNAL_EVERY == 0:
                self.image_received.emit(count)
            return 0x0000
        except Exception as e:
            logger.error(f"Store failed: {e}")
            return 0xC000

    def start(self):
        # Compare-and-set under the lock so two callers can't both start.
        with self._run_lock:
            if self._running_flag:
                return
            self._running_flag = True
        self.ae = AE(ae_title=self.ae_title)
        self.ae.supported_contexts = StoragePresentationContexts
        self.ae.add_supported_context(Verification)
        self._ready.clear()

        def run():
            try:
                self._ready.set()
                self.ae.start_server(
                    (self.bind_address, self.port), block=True,
                    evt_handlers=[(evt.EVT_C_STORE, self.handle_store)])
            except Exception as e:
                logger.error(f"SCP error: {e}")
            finally:
                with self._run_lock:
                    self._running_flag = False

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5.0)
        # ``_ready`` is set immediately before ``start_server``.  On
        # success that call blocks forever; on failure it raises
        # synchronously and the worker exits.  Give the worker a beat
        # to either settle into start_server (success path) or finish
        # raising + run its ``finally`` (failure path) before we trust
        # ``_running_flag``.  The previous pure-flag check raced under
        # heavy suite load.
        self._thread.join(timeout=0.2)
        if not self.running:
            raise RuntimeError(
                f"Storage SCP failed to bind on port {self.port}")
        logger.info(f"Storage SCP started on port {self.port}")

    def stop(self):
        # Atomically claim the shutdown so a concurrent stop() or the
        # reactor's own finally clause cannot race us into calling
        # ae.shutdown() twice on the same (already-torn-down) AE.
        with self._run_lock:
            if not self._running_flag or self.ae is None:
                return
            self._running_flag = False
        try:
            self.ae.shutdown()
        except Exception as e:
            logger.warning(f"SCP shutdown failed: {e}")
        if self._thread:
            self._thread.join(timeout=5.0)
        logger.info("Storage SCP stopped")

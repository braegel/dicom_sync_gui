"""
Built-in Storage SCP for receiving DICOM images.
Used as fallback when no external DICOM server is available.
"""

import logging
import os
import threading
from typing import Optional

from PySide6.QtCore import QObject, Signal

from pydicom import Dataset
from pynetdicom import AE, evt, StoragePresentationContexts
from pynetdicom.sop_class import Verification

logger = logging.getLogger("dicom_sync")


class StorageSCP(QObject):
    """Built-in DICOM Storage SCP.

    `image_received` is emitted from the pynetdicom reactor thread
    after each successful C-STORE.  Qt marshals the signal to the
    main thread when the connection uses the default
    Qt.AutoConnection, so slots can safely touch widgets."""

    image_received = Signal(Dataset)

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
            ds.save_as(filepath, write_like_original=False)
            with self._lock:
                self._images_received += 1
            self.image_received.emit(ds)
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

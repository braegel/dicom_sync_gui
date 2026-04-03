"""
Built-in Storage SCP for receiving DICOM images.
Used as fallback when no external DICOM server is available.
"""

import logging
import os
import threading
import time
from typing import Callable, Optional

from pynetdicom import AE, evt, StoragePresentationContexts
from pynetdicom.sop_class import Verification

logger = logging.getLogger("dicom_sync")


class StorageSCP:
    """Built-in DICOM Storage SCP."""

    def __init__(self, ae_title: str, port: int, storage_path: str,
                 on_image_received: Optional[Callable] = None):
        self.ae_title = ae_title
        self.port = port
        self.storage_path = storage_path
        self.on_image_received = on_image_received
        self.ae = None
        self.running = False
        self._lock = threading.Lock()
        self._images_received = 0
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        os.makedirs(storage_path, exist_ok=True)

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
            if self.on_image_received:
                self.on_image_received(ds)
            return 0x0000
        except Exception as e:
            logger.error(f"Store failed: {e}")
            return 0xC000

    def start(self):
        if self.running:
            return
        self.ae = AE(ae_title=self.ae_title)
        self.ae.supported_contexts = StoragePresentationContexts
        self.ae.add_supported_context(Verification)
        self._ready.clear()

        def run():
            try:
                self.running = True
                self._ready.set()
                self.ae.start_server(
                    ('0.0.0.0', self.port), block=True,
                    evt_handlers=[(evt.EVT_C_STORE, self.handle_store)])
            except Exception as e:
                logger.error(f"SCP error: {e}")
            finally:
                self.running = False

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5.0)
        logger.info(f"Storage SCP started on port {self.port}")

    def stop(self):
        if self.ae and self.running:
            self.ae.shutdown()
            if self._thread:
                self._thread.join(timeout=5.0)
            self.running = False
            logger.info("Storage SCP stopped")

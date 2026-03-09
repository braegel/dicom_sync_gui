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
        self.images_received = 0
        os.makedirs(storage_path, exist_ok=True)

    def handle_store(self, event):
        ds = event.dataset
        ds.file_meta = event.file_meta
        try:
            filepath = os.path.join(self.storage_path, f"{ds.SOPInstanceUID}.dcm")
            ds.save_as(filepath, write_like_original=False)
            self.images_received += 1
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

        def run():
            try:
                self.ae.start_server(
                    ('0.0.0.0', self.port), block=True,
                    evt_handlers=[(evt.EVT_C_STORE, self.handle_store)])
            except Exception as e:
                logger.error(f"SCP error: {e}")

        threading.Thread(target=run, daemon=True).start()
        self.running = True
        logger.info(f"Storage SCP started on port {self.port}")

    def stop(self):
        if self.ae and self.running:
            self.ae.shutdown()
            self.running = False
            logger.info("Storage SCP stopped")

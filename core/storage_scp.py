"""
Built-in Storage SCP for receiving DICOM images.
Used as fallback when no external DICOM server is available.
"""

import inspect
import logging
import os
import threading
from typing import List, Optional

from PySide6.QtCore import QObject, Signal

from pydicom import Dataset
from pynetdicom import AE, evt, StoragePresentationContexts
from pynetdicom.sop_class import Verification
from pydicom.uid import (
    DeflatedExplicitVRLittleEndian,
    ExplicitVRBigEndian,
    ExplicitVRLittleEndian,
    ImplicitVRLittleEndian,
    JPEG2000,
    JPEG2000Lossless,
    JPEGBaseline8Bit,
    JPEGExtended12Bit,
    JPEGLosslessSV1,
    JPEGLSLossless,
    JPEGLSNearLossless,
    RLELossless,
)

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

# Transfer syntaxes this SCP will accept, in OUR order of preference --
# and the order matters, because as the association ACCEPTOR we are the
# side that picks.  pynetdicom walks this list and takes the first entry
# the sender also proposed; the sender's own ordering is ignored.
#
# Lossless compressed comes FIRST, ahead of the uncompressed syntaxes.
# That is not a size optimization, it works around a real and otherwise
# silent data-corruption bug:
#
#   Some PACS (confirmed with a "1.5.0/WIN32" implementation) send the
#   pixel data JPEG 2000 compressed NO MATTER which transfer syntax was
#   negotiated.  Propose them ``[JPEG2000Lossless, ImplicitVRLittleEndian]``
#   and accept Implicit VR LE, and they still put an encapsulated J2K
#   codestream in (7FE0,0010) with an undefined length -- which is only
#   legal in a COMPRESSED syntax.  The object is then mislabelled: 60 KB
#   of J2K bytes claiming to be 512 KB of raw pixels.  pydicom parses it
#   anyway, so the corruption is invisible here, but a DCMTK-based
#   receiver downstream (OsiriX, dcmtk itself) rejects the file.
#
# Accepting the compressed syntax whenever the sender offers one makes
# the label match the bytes, so the stored file is conformant either way.
#
# Lossy syntaxes stay LAST, behind the uncompressed ones: if a sender
# offers both lossy-compressed and uncompressed it can serve either, and
# picking lossy would throw away image data for nothing.
STORAGE_TRANSFER_SYNTAXES = [
    # 1. lossless compressed -- preferred (see above)
    JPEG2000Lossless,
    JPEGLSLossless,
    JPEGLosslessSV1,
    RLELossless,
    # 2. uncompressed
    ExplicitVRLittleEndian,
    ImplicitVRLittleEndian,
    DeflatedExplicitVRLittleEndian,
    ExplicitVRBigEndian,
    # 3. lossy -- accepted only when nothing better is on offer
    JPEG2000,
    JPEGLSNearLossless,
    JPEGBaseline8Bit,
    JPEGExtended12Bit,
]


def _syntaxes_preferring(preferred: Optional[str]) -> List[str]:
    """Return the accepted transfer syntaxes with *preferred* moved to
    the front, or ``STORAGE_TRANSFER_SYNTAXES`` unchanged when no
    preference is given.

    Front position is what has an effect: as the acceptor we pick the
    first entry the sender also proposed.  Everything else is kept
    behind it as a fallback rather than dropped, so choosing a syntax a
    particular sender does not offer degrades to the default order
    instead of failing the association.  A syntax that is not in the
    default list at all is still honoured -- the UI offers a few that
    the default order does not enumerate.
    """
    if not preferred:
        return STORAGE_TRANSFER_SYNTAXES
    return [preferred] + [ts for ts in STORAGE_TRANSFER_SYNTAXES
                          if ts != preferred]


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
                 bind_address: str = "0.0.0.0",
                 preferred_syntax: Optional[str] = None):
        super().__init__()
        self.ae_title = ae_title
        self.port = port
        self.bind_address = bind_address
        self.storage_path = storage_path
        # Transfer syntax UID the node's "Preferred Syntax" asks for, or
        # None to use STORAGE_TRANSFER_SYNTAXES unchanged.
        self.transfer_syntaxes = _syntaxes_preferring(preferred_syntax)
        self.ae = None
        # ``_running_flag`` is read/written from both the reactor thread
        # and the caller (GUI) thread; access goes through ``_run_lock``
        # so ``stop()`` cannot observe a stale True after the reactor
        # has already set it False and torn the AE down.
        self._running_flag = False
        self._run_lock = threading.Lock()
        self._lock = threading.Lock()  # protects _images_received
        self._images_received = 0
        os.makedirs(storage_path, exist_ok=True)

    @property
    def running(self) -> bool:
        with self._run_lock:
            return self._running_flag

    @running.setter
    def running(self, value: bool) -> None:
        # Setter kept so existing test mocks that assign ``.running``
        # continue to work; thread-safety only matters on real instances.
        with self._run_lock:
            self._running_flag = bool(value)

    @property
    def images_received(self) -> int:
        with self._lock:
            return self._images_received

    def handle_store(self, event: evt.Event) -> int:
        ds = event.dataset
        ds.file_meta = event.file_meta
        try:
            filepath = os.path.join(self.storage_path, f"{ds.SOPInstanceUID}.dcm")
            # Conformant Part 10 write; keyword chosen at import time
            # (see _SAVE_AS_KWARGS) to stay compatible with pydicom 2.x
            # ("write_like_original") and 3.x ("enforce_file_format").
            try:
                ds.save_as(filepath, **_SAVE_AS_KWARGS)
            except FileNotFoundError:
                # The storage directory vanished under us.  That is not
                # an edge case here: the whole point of this folder is
                # that a local PACS watches it and imports what lands in
                # it -- OsiriX removes the (now empty) directory once it
                # has, and every following image would then fail with
                # ENOENT until the service is restarted.  Recreate and
                # retry once, off the hot path so the common case still
                # costs nothing.
                os.makedirs(self.storage_path, exist_ok=True)
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

    def start(self) -> None:
        """Bind the SCP's listening socket and serve in the background.

        ``block=False`` binds in the CALLING thread and raises
        synchronously if the port is unavailable, then hands serving to
        pynetdicom's own daemon thread.  That matters because the only
        caller (``MainWindow._ensure_fallback_scp``) runs on the GUI
        thread: the previous ``block=True``-in-a-worker design had no way
        to learn whether the bind had succeeded, so it slept a fixed
        200 ms on the GUI thread and then guessed from a flag — a wait
        that both froze the UI and raced under load.
        """
        # Compare-and-set under the lock so two callers can't both start.
        with self._run_lock:
            if self._running_flag:
                return
            self._running_flag = True
        ae = AE(ae_title=self.ae_title)
        # Rebuild the default storage contexts with our own transfer
        # syntax preference list instead of pynetdicom's (which offers
        # only the uncompressed syntaxes) -- see
        # STORAGE_TRANSFER_SYNTAXES for why the order is load-bearing.
        for cx in StoragePresentationContexts:
            ae.add_supported_context(
                cx.abstract_syntax, self.transfer_syntaxes)
        ae.add_supported_context(Verification, self.transfer_syntaxes)
        self.ae = ae
        try:
            ae.start_server(
                (self.bind_address, self.port), block=False,
                evt_handlers=[(evt.EVT_C_STORE, self.handle_store)])
        except Exception as e:
            # Port already in use, permission denied, bad bind address …
            # Release the claim so a later retry can start cleanly.
            logger.error(f"SCP error: {e}")
            with self._run_lock:
                self._running_flag = False
            self._shutdown_ae(ae)
            self.ae = None
            raise RuntimeError(
                f"Storage SCP failed to bind on port {self.port}: {e}"
            ) from e
        logger.info(f"Storage SCP started on port {self.port}")

    def stop(self) -> None:
        # Atomically claim the shutdown so a concurrent stop() cannot
        # race us into calling ae.shutdown() twice on the same
        # (already-torn-down) AE.
        with self._run_lock:
            if not self._running_flag or self.ae is None:
                return
            self._running_flag = False
            ae = self.ae
        self._shutdown_ae(ae)
        logger.info("Storage SCP stopped")

    @staticmethod
    def _shutdown_ae(ae: AE) -> None:
        """Shut an AE down without letting a teardown error escape —
        ``ae.shutdown()`` also stops the server threads it spawned."""
        try:
            ae.shutdown()
        except Exception as e:
            logger.warning(f"SCP shutdown failed: {e}")

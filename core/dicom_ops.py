"""
DICOM network operations: C-ECHO, C-FIND, C-MOVE.
Abstracted from the original CLI script for GUI use.
"""

import logging
import warnings
from typing import (
    Any, Callable, Dict, Iterator, List, Optional, Tuple,
)

from pydicom import Dataset
from pydicom.uid import (
    ExplicitVRLittleEndian, ImplicitVRLittleEndian, JPEG2000Lossless,
    JPEGLosslessSV1, JPEGLossless, DeflatedExplicitVRLittleEndian,
)
from pynetdicom import AE
from pynetdicom.association import Association
from pynetdicom.sop_class import (
    PatientRootQueryRetrieveInformationModelFind,
    PatientRootQueryRetrieveInformationModelMove,
    StudyRootQueryRetrieveInformationModelFind,
    StudyRootQueryRetrieveInformationModelMove,
    Verification,
)

logger = logging.getLogger("dicom_sync")

# Progress callback signature: called with running (completed, total)
# image counts during a C-MOVE.
ProgressCB = Callable[[int, int], None]


class PacsConnectionError(Exception):
    """Raised when an association with a PACS cannot be established —
    the host refused the connection, the TCP connect timed out, or the
    association was rejected.

    Distinct from a successful association that simply returns no
    results (a normal empty C-FIND) or a per-image C-MOVE sub-failure:
    those leave the connection intact.  The engine uses this type to
    tell "the PACS went away" apart from "nothing to do" / "one bad
    series", so it can surface an unreachable-PACS popup and stop
    hammering a dead host for the rest of the cycle.
    """

TRANSFER_SYNTAXES = {
    "JPEG2000Lossless": JPEG2000Lossless,
    "ExplicitVRLittleEndian": ExplicitVRLittleEndian,
    "ImplicitVRLittleEndian": ImplicitVRLittleEndian,
    "JPEGLossless": JPEGLossless,
    "JPEGLosslessSV1": JPEGLosslessSV1,
    "DeflatedExplicitVRLittleEndian": DeflatedExplicitVRLittleEndian,
}

# Association timeouts (seconds).  pynetdicom's default dimse_timeout
# is ``None`` — wait forever — so a wedged PACS would block the
# engine's service loop indefinitely (stop() only sets a flag the
# loop never reaches).  DIMSE gets 5 minutes because some C-MOVE SCPs
# send pending responses only sporadically during a large series;
# the network timeout sits above it so the DIMSE layer times out
# first with a clean error instead of the socket dropping mid-message.
ACSE_TIMEOUT_S = 30
DIMSE_TIMEOUT_S = 300
NETWORK_TIMEOUT_S = DIMSE_TIMEOUT_S + 30
# TCP connect timeout for ``AE.associate``.  pynetdicom's default is
# ``None`` (rely on the OS connect timeout, which is ~75 s on Linux
# and can be effectively unbounded when a firewall silently drops
# SYN packets).  Without it an unreachable PACS hangs the service
# loop and the C-ECHO reachability check.  10 s is well above any
# healthy LAN/VPN round-trip while still failing fast on a dead host.
CONNECTION_TIMEOUT_S = 10

# DUL reactor poll interval (seconds).  pynetdicom's default is 0.001
# (1 ms): during a busy C-MOVE the DUL reactor thread spins this tight,
# burning ~100% CPU on one core.  Because of the Python GIL that starves
# the Qt GUI thread, making the whole app unresponsive during large
# downloads (confirmed by py-spy: the reactor thread was the only active
# Python thread while the GUI main thread sat idle in app.exec()).
# Raising it to 20 ms cuts the reactor's idle CPU ~20x and frees the GIL
# for the GUI; the added per-PDU latency is negligible for image transfer.
DUL_RUN_LOOP_DELAY_S = 0.02


class DicomOperations:
    """Handles all DICOM network operations."""

    def __init__(self, local_config: Dict[str, Any], remote_config: Dict[str, Any],
                 remote_name: str = ""):
        self.local_config = local_config
        self.remote_config = remote_config
        self.remote_name = remote_name
        self.transfer_syntax = TRANSFER_SYNTAXES.get(
            remote_config.get('transfer_syntax', 'JPEG2000Lossless'), JPEG2000Lossless)
        # C-MOVE destination is always the per-source local config.
        # The local_config already contains the correct AE title, port, etc.
        # for this specific source PACS.
        self.move_dest_config = local_config

        self.ae = self._build_ae()
        self._register_contexts()

    def _build_ae(self) -> AE:
        """Construct the AE with its title and the four association
        timeouts.  See the module-level timeout constants for the
        rationale behind each value."""
        ae = AE(ae_title=self.local_config.get('ae_title', 'LOCAL_AE'))
        ae.acse_timeout = ACSE_TIMEOUT_S
        ae.dimse_timeout = DIMSE_TIMEOUT_S
        ae.network_timeout = NETWORK_TIMEOUT_S
        ae.connection_timeout = CONNECTION_TIMEOUT_S
        return ae

    def _register_contexts(self) -> None:
        """Register the query/retrieve and verification presentation
        contexts on ``self.ae``.

        Offer the configured per-node transfer syntax as the
        *preferred* syntax on the query/retrieve contexts.  This only
        affects negotiation of this query association (C-FIND/C-MOVE
        requests carry identifier datasets, never pixel data); the
        transfer syntax of the actual image transfer during a C-MOVE
        is negotiated between the source PACS and the destination
        Store SCP on a separate association this AE does not control.
        Explicit VR LE and Implicit VR LE stay in the list as
        fallbacks — Implicit VR LE is the DICOM baseline every SCP
        must support — so interoperability cannot regress.  Skip
        duplicates in case the configured syntax already is one of
        the two fallbacks.
        """
        query_syntaxes = [self.transfer_syntax]
        for fallback in (ExplicitVRLittleEndian, ImplicitVRLittleEndian):
            if fallback not in query_syntaxes:
                query_syntaxes.append(fallback)
        for ctx in [PatientRootQueryRetrieveInformationModelFind,
                    PatientRootQueryRetrieveInformationModelMove,
                    StudyRootQueryRetrieveInformationModelFind,
                    StudyRootQueryRetrieveInformationModelMove]:
            self.ae.add_requested_context(ctx, query_syntaxes)
        # Verification (C-ECHO) keeps pynetdicom's default syntaxes.
        self.ae.add_requested_context(Verification)

    def close(self) -> None:
        """Shut down the AE so any association threads exit before the
        object is dropped.

        Letting an AE be garbage-collected while its threads are still
        alive is the SIGSEGV the ``gc.freeze()`` workaround in main.py
        masks — every owner of a DicomOperations must call this when
        done with it (the engine does so in its service-loop teardown;
        short-lived owners should use ``try/finally``).
        """
        if self.ae is None:
            return
        try:
            self.ae.shutdown()
        except Exception as e:
            logger.warning(
                f"[{self.remote_name}] AE shutdown failed: {e}")

    def _associate(self, config: Dict[str, Any]) -> Association:
        """Open an association to *config*, raising
        :class:`PacsConnectionError` if it cannot be established.

        Centralizes the "is the PACS actually reachable" decision so
        ``c_echo`` (boolean probe), ``_execute_find`` and
        ``_execute_move`` all agree on what counts as unreachable.
        Returns an established ``Association``; the caller owns the
        ``release()``.
        """
        target = (f"{config.get('ae_title')}@{config.get('ip_address')}:"
                  f"{config.get('port')}")
        try:
            assoc = self.ae.associate(
                config['ip_address'], config['port'],
                ae_title=config['ae_title'])
        except Exception as e:
            raise PacsConnectionError(
                f"Could not connect to {target}: {e}") from e
        if not assoc.is_established:
            # Nothing to release on a non-established association.
            raise PacsConnectionError(
                f"Association with {target} was not established "
                f"(rejected or timed out)")
        # Slow the DUL reactor's poll loop (default 1 ms) so it doesn't
        # spin a core at ~100% and starve the Qt GUI thread of the GIL
        # during a busy C-MOVE.  Guarded with getattr/try so a future
        # pynetdicom that renames/removes the attribute can't break the
        # association.
        try:
            dul = getattr(assoc, "dul", None)
            if dul is not None and hasattr(dul, "_run_loop_delay"):
                dul._run_loop_delay = DUL_RUN_LOOP_DELAY_S
        except Exception as e:  # never let tuning abort a good association
            logger.debug(f"Could not set DUL run-loop delay: {e}")
        return assoc

    def c_echo(self, target: str = 'remote') -> bool:
        config = self.local_config if target == 'local' else self.remote_config
        try:
            assoc = self._associate(config)
        except PacsConnectionError as e:
            logger.debug(f"C-ECHO failed: {e}")
            return False
        try:
            status = assoc.send_c_echo()
            if status is None:
                return False
            return getattr(status, "Status", 0xFFFF) == 0x0000
        except Exception as e:
            logger.debug(f"C-ECHO failed: {e}")
            return False
        finally:
            assoc.release()

    def c_find_studies(self,
                       study_date: Optional[str] = None,
                       patient_id: Optional[str] = None,
                       study_uid: Optional[str] = None
                       ) -> List[Dataset]:
        ds = Dataset()
        ds.QueryRetrieveLevel = 'STUDY'
        # DICOM convention: an empty-string tag value means "please
        # include this tag in the response" (universal matching).
        ds.PatientID = patient_id or ''
        ds.PatientName = ''
        ds.StudyInstanceUID = study_uid or ''
        ds.StudyDate = study_date or ''
        ds.StudyTime = ''
        ds.StudyDescription = ''
        ds.NumberOfStudyRelatedInstances = ''
        ds.ModalitiesInStudy = ''
        ds.AccessionNumber = ''
        ds.InstitutionName = ''
        return self._execute_find(ds)

    def c_find_series(self, study_uid: str) -> List[Dataset]:
        return self.c_find_series_checked(study_uid)[1]

    def c_find_series_checked(self,
                              study_uid: str) -> Tuple[bool, List[Dataset]]:
        """Series-level C-FIND that also reports whether the query ran
        to completion — ``(complete, series)``.

        Same query as :py:meth:`c_find_series`, which stays list-only
        for the GUI call sites.  The engine uses this variant because a
        truncated series list is indistinguishable from a genuinely
        short study once the flag is dropped, and it would then declare
        a study "fully complete" from series it never even enumerated.
        """
        ds = Dataset()
        ds.QueryRetrieveLevel = 'SERIES'
        ds.StudyInstanceUID = study_uid
        ds.SeriesInstanceUID = ''
        ds.SeriesNumber = ''
        ds.Modality = ''
        ds.SeriesDescription = ''
        ds.SeriesDate = ''
        ds.SeriesTime = ''
        ds.NumberOfSeriesRelatedInstances = ''
        ds.InstitutionName = ''
        return self._execute_find_checked(ds)

    def c_find_institution_names(self, study_date: Optional[str] = None
                                  ) -> List[str]:
        """Discover unique InstitutionName values via series-level query.

        InstitutionName is typically not returned at STUDY level by most
        PACS. We query at SERIES level where it is reliably available.
        """
        # First: find all study UIDs in the date range
        studies = self.c_find_studies(study_date=study_date)
        institution_names: set = set()

        for study_ds in studies:
            study_uid = getattr(study_ds, 'StudyInstanceUID', '')
            if not study_uid:
                continue

            # Check if study-level already has InstitutionName
            inst = str(getattr(study_ds, 'InstitutionName', '')).strip()
            if inst:
                institution_names.add(inst)
                continue

            # Fallback: query one series of this study for InstitutionName
            ds = Dataset()
            ds.QueryRetrieveLevel = 'SERIES'
            ds.StudyInstanceUID = study_uid
            ds.SeriesInstanceUID = ''
            ds.InstitutionName = ''
            series_results = self._execute_find(ds)
            for ser in series_results:
                inst = str(getattr(ser, 'InstitutionName', '')).strip()
                if inst:
                    institution_names.add(inst)
                    break  # one hit per study is enough

        return sorted(institution_names)

    def c_find_images(self, study_uid: str, series_uid: str) -> List[Dataset]:
        ds = Dataset()
        ds.QueryRetrieveLevel = 'IMAGE'
        ds.StudyInstanceUID = study_uid
        ds.SeriesInstanceUID = series_uid
        ds.SOPInstanceUID = ''
        return self._execute_find(ds)

    def c_find_local_series(self, study_uid: str) -> List[Dataset]:
        ds = Dataset()
        ds.QueryRetrieveLevel = 'SERIES'
        ds.StudyInstanceUID = study_uid
        ds.SeriesInstanceUID = ''
        ds.NumberOfSeriesRelatedInstances = ''
        return self._execute_find(ds, target='local')

    def c_find_local_images(self, study_uid: str, series_uid: str) -> List[Dataset]:
        ds = Dataset()
        ds.QueryRetrieveLevel = 'IMAGE'
        ds.StudyInstanceUID = study_uid
        ds.SeriesInstanceUID = series_uid
        ds.SOPInstanceUID = ''
        return self._execute_find(ds, target='local')

    def _execute_find(self, query_ds: Dataset,
                      target: str = 'remote') -> List[Dataset]:
        """Run a C-FIND and return its result datasets.

        Drops the completeness flag of
        :py:meth:`_execute_find_checked` — use that one wherever a
        truncated result list would be mistaken for an authoritative
        one (see its docstring)."""
        return self._execute_find_checked(query_ds, target)[1]

    @staticmethod
    def _iter_find_results(assoc: Association,
                           query_ds: Dataset) -> Iterator[Dataset]:
        """Yield the identifier of each PENDING C-FIND response.

        Split out of :py:meth:`_execute_find_checked` so that method
        reads as "associate, collect, release" instead of nesting the
        warning filter, the response loop and two status guards four
        levels deep inside its own ``try``.

        A GENERATOR, deliberately, not a list: the caller must keep the
        datasets that arrived before an association break, which is the
        whole point of its ``complete`` flag.  Building a list here and
        returning it would discard exactly those partial results when
        the stream raises partway through.

        Only PENDING responses (0xFF00 / 0xFF01) carry an identifier;
        the final SUCCESS response has none and must not be collected.
        """
        # Suppress pydicom's "VR UI value length exceeds maximum"
        # warning that some PACS implementations trigger; scoped to the
        # call site, not process-wide.
        with warnings.catch_warnings():
            warnings.filterwarnings(
                'ignore',
                message='.*value length.*exceeds the maximum length.*VR UI.*')
            for status, dataset in assoc.send_c_find(
                    query_ds, StudyRootQueryRetrieveInformationModelFind):
                if status is None or dataset is None:
                    continue
                if getattr(status, "Status", 0) in (0xFF00, 0xFF01):
                    yield dataset

    def _execute_find_checked(self, query_ds: Dataset, target: str = 'remote'
                              ) -> Tuple[bool, List[Dataset]]:
        """Run a C-FIND and return ``(complete, results)``.

        *complete* is False when the iteration aborted mid-stream, i.e.
        *results* holds only the datasets that arrived before the
        association broke.  Callers that treat the result list as the
        full truth (the engine's study-completion decision) must check
        it; callers that only aggregate what they got (institution
        discovery) can ignore it via :py:meth:`_execute_find`.
        """
        config = self.local_config if target == 'local' else self.remote_config
        results = []
        # ``_associate`` raises PacsConnectionError when the PACS is
        # unreachable; that propagates to the engine (which surfaces
        # the "not reachable" popup).  A DIMSE error mid-iteration is a
        # different beast — the association WAS established — so it's
        # caught below and yields whatever partial results arrived,
        # flagged as incomplete.
        complete = True
        assoc = self._associate(config)
        try:
            # Append as they arrive: a break mid-stream must leave the
            # datasets received so far in ``results`` (see the
            # ``complete`` flag above).
            for dataset in self._iter_find_results(assoc, query_ds):
                results.append(dataset)
        except Exception as e:
            logger.error(f"C-FIND error: {e}")
            complete = False
        finally:
            assoc.release()
        return complete, results

    def c_move_series(self, study_uid: str, series_uid: str,
                      progress_cb: Optional[ProgressCB] = None) -> Tuple[bool, int]:
        ds = Dataset()
        ds.QueryRetrieveLevel = 'SERIES'
        ds.StudyInstanceUID = study_uid
        ds.SeriesInstanceUID = series_uid
        return self._execute_move(ds, progress_cb=progress_cb)

    def c_move_image(self, study_uid: str, series_uid: str, sop_uid: str) -> Tuple[bool, int]:
        ds = Dataset()
        ds.QueryRetrieveLevel = 'IMAGE'
        ds.StudyInstanceUID = study_uid
        ds.SeriesInstanceUID = series_uid
        ds.SOPInstanceUID = sop_uid
        return self._execute_move(ds)

    def _execute_move(self, query_ds: Dataset,
                      progress_cb: Optional[ProgressCB] = None) -> Tuple[bool, int]:
        """Run a C-MOVE.  *progress_cb*, if given, is called with the
        running ``(completed, total)`` image counts each time the remote
        sends a sub-operation status update — used by the engine's
        per-image stall watchdog so a slow-but-alive transfer is not
        mistaken for a wedged one."""
        move_dest = self.move_dest_config.get('ae_title', self.local_config.get('ae_title'))
        success, images = False, 0
        # See _execute_find: PacsConnectionError (PACS unreachable)
        # propagates to the engine; a mid-stream DIMSE error is caught
        # below and reported as a normal (False, images) failure.
        #
        # ``success`` is deliberately NOT cleared by that except clause.
        # 0x0000 is the FINAL C-MOVE status: if it arrived, the remote
        # finished the operation, and an exception raised afterwards
        # (tearing the generator down) does not undo that.  Clearing it
        # would make the engine record a failure for a series that did
        # transfer, and re-fetch the whole thing next cycle.
        assoc = self._associate(self.remote_config)
        try:
            for status, _ in assoc.send_c_move(
                    query_ds, move_dest, StudyRootQueryRetrieveInformationModelMove):
                done, images = self._read_move_status(
                    status, images, progress_cb)
                success = success or done
        except Exception as e:
            logger.error(f"C-MOVE error: {e}")
        finally:
            assoc.release()
        return success, images

    @staticmethod
    def _read_move_status(status: Optional[Dataset], images: int,
                          progress_cb: Optional[ProgressCB]
                          ) -> Tuple[bool, int]:
        """Interpret one C-MOVE status message.

        Returns ``(is_final_success, images)`` — *images* is carried
        through unchanged when this particular status carries no
        sub-operation count, so the caller keeps the last known value.

        Split out of the send loop purely for depth: with the callback
        guard inline the loop nested five levels, which is where the
        "does this ``if`` belong to the status or to the callback?"
        reading errors start.
        """
        if status is None:
            return False, images
        is_final_success = getattr(status, "Status", None) == 0x0000
        completed = getattr(
            status, 'NumberOfCompletedSuboperations', None)
        if completed is None:
            return is_final_success, images
        if progress_cb is not None:
            remaining = getattr(
                status, 'NumberOfRemainingSuboperations', 0) or 0
            try:
                progress_cb(completed, completed + remaining)
            except Exception:
                # A misbehaving callback must never abort an in-flight
                # transfer.
                pass
        return is_final_success, completed

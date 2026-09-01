"""
DICOM network operations: C-ECHO, C-FIND, C-MOVE.
Abstracted from the original CLI script for GUI use.
"""

import contextlib
import logging
import re
import threading
import warnings
from typing import (
    Any, Callable, Dict, Iterator, List, Optional, Set, Tuple,
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

# ---------------------------------------------------------------------
# Non-conformant values from a PACS, and why one filter is not enough.
#
# pydicom 3.0.2 reports a value-representation violation on TWO
# channels: it raises a Python warning AND logs a record through the
# ``pydicom`` logger, which propagates to the root logger and so into
# the app's log file.  The ``warnings.catch_warnings`` block in
# _iter_find_results covers the first channel only, which is why the
# very message that filter exists to remove kept being written anyway.
#
# Two producers are known in the field, and neither is anything this
# application can fix:
#
# * OsiriX.  The SR series it generates ("OsiriX ROI SR", "OsiriX
#   Annotations SR", "WindowsState SR", "Report SR") carry a
#   SeriesInstanceUID that is a COMMA-JOINED LIST of the series the
#   annotation refers to -- 95 to 287 characters where VR UI allows 64.
#   Both the source PACS and the local PACS hand that same value back,
#   so the two sides still agree and series matching is unaffected.
# * UIDs with a leading zero in a component
#   (``...20260825.082142.761``), which some modalities emit and every
#   PACS in the chain stores verbatim.
#
# The damage was purely to the log.  Measured on the reporting machine,
# these messages were 74% of the log's LINES and 92% of its BYTES,
# rotating the real transfer history out of the 8 MB of retained
# history in about five hours.
#
# The filter below therefore does NOT drop them outright: that a PACS
# emits non-conformant values is worth knowing, so the FIRST report of
# each distinct violation is kept and only the repeats are dropped.
# Anything pydicom says that is not a VR violation is untouched.
# ---------------------------------------------------------------------
_VR_VIOLATION_MARKER = "Invalid value for VR"


class _RepeatedVrViolationFilter(logging.Filter):
    """Keep the first report of each VR violation, drop every repeat.

    The signature strips the offending value and every number out of
    the message, so the thousands of DISTINCT malformed UIDs a single
    PACS produces collapse onto one key -- without that, remembering
    "already seen" would mean remembering every bad UID ever received
    and the set would grow without bound.
    """

    _QUOTED = re.compile(r"'[^']*'")
    _NUMBER = re.compile(r"\d+")

    def __init__(self) -> None:
        super().__init__()
        self._seen: Set[str] = set()
        self._lock = threading.Lock()

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            # A record we cannot even format is not the one we are
            # trying to suppress -- let it through rather than swallow
            # it.
            return True
        if _VR_VIOLATION_MARKER not in message:
            return True
        signature = self._NUMBER.sub("N", self._QUOTED.sub("'?'", message))
        with self._lock:
            if signature in self._seen:
                return False
            self._seen.add(signature)
        return True


_vr_filter_lock = threading.Lock()
_vr_filter_installed = False


def silence_repeated_vr_violations() -> None:
    """Install the log-channel half of the VR-violation suppression.

    Idempotent and thread-safe on purpose: one engine per source PACS
    builds its own DicomOperations, concurrently, and
    ``Logger.addFilter`` does not de-duplicate -- without the guard the
    same filter would stack up once per source, and each copy would
    keep its own "already seen" set.
    """
    global _vr_filter_installed
    with _vr_filter_lock:
        if _vr_filter_installed:
            return
        logging.getLogger("pydicom").addFilter(_RepeatedVrViolationFilter())
        _vr_filter_installed = True


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
                 remote_name: str = "",
                 local_query_config: Optional[Dict[str, Any]] = None):
        self.local_config = local_config
        self.remote_config = remote_config
        self.remote_name = remote_name
        self.transfer_syntax = TRANSFER_SYNTAXES.get(
            remote_config.get('transfer_syntax', 'JPEG2000Lossless'), JPEG2000Lossless)
        # C-MOVE destination is always the per-source local config.
        # The local_config already contains the correct AE title, port, etc.
        # for this specific source PACS.
        self.move_dest_config = local_config
        # Where ``target='local'`` queries go.  Normally the same box
        # that receives the images, but the two are separable: the
        # built-in Storage SCP can hold the receiving address while the
        # local PACS is the only one that can answer a C-FIND.  See
        # ``AppConfig.get_local_query_dict_for``.
        self.local_query_config = (local_query_config
                                   if local_query_config is not None
                                   else local_config)

        # Open associations shared by every C-FIND inside a
        # ``find_session``; empty dict = a session is running, None =
        # no session, associate per query.  See ``find_session``.
        self._find_pool: Optional[Dict[str, Association]] = None
        # Both halves of the VR-violation suppression belong together;
        # the warnings half sits in _iter_find_results.  Installed here
        # rather than at import time so that merely importing this
        # module never mutates global logging state.
        silence_repeated_vr_violations()
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
        # Deliberately the DESTINATION config, not the query one: the
        # only caller asks this to decide whether C-MOVE can deliver to
        # the local PACS or a built-in SCP has to stand in for it.
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
        return self.c_find_local_series_checked(study_uid)[1]

    def c_find_local_series_checked(
            self, study_uid: str) -> Tuple[bool, List[Dataset]]:
        """Local inventory query that also reports whether it ran to
        completion — ``(complete, series)``.

        The flag is load-bearing here, more so than for the remote
        series query.  An endpoint that cannot answer a C-FIND *at all*
        — the built-in Storage SCP, which offers no Find presentation
        context — produces an established association and then an empty
        result, which is indistinguishable from an authoritative "this
        study has not arrived yet".  Believe the latter and the engine
        re-downloads the entire time window on every cycle, forever.
        ``complete`` is False in that case and True for a genuine empty
        answer, so the caller can tell them apart.
        """
        ds = Dataset()
        ds.QueryRetrieveLevel = 'SERIES'
        ds.StudyInstanceUID = study_uid
        ds.SeriesInstanceUID = ''
        ds.NumberOfSeriesRelatedInstances = ''
        return self._execute_find_checked(ds, target='local')

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
        # Suppress pydicom's complaint about the non-conformant values
        # some PACS implementations return; scoped to the call site,
        # not process-wide.  This covers the ``warnings`` channel only
        # -- pydicom ALSO logs the same violation, which
        # ``silence_repeated_vr_violations`` handles.  Both are needed,
        # and the pattern must match every VR violation, not just the
        # over-long one: see the module-level note.
        with warnings.catch_warnings():
            warnings.filterwarnings(
                'ignore', message='.*Invalid value for VR.*')
            for status, dataset in assoc.send_c_find(
                    query_ds, StudyRootQueryRetrieveInformationModelFind):
                if status is None or dataset is None:
                    continue
                if getattr(status, "Status", 0) in (0xFF00, 0xFF01):
                    yield dataset

    @contextlib.contextmanager
    def find_session(self) -> Iterator[None]:
        """Reuse ONE association per endpoint for every C-FIND in the
        block, instead of opening and releasing one per query.

        A query cycle asks the source PACS for its studies and then, for
        EVERY study, a series list -- and asks the local PACS the same
        question again to see what has already arrived.  At one
        association per query that is ``1 + 2n`` connect / negotiate /
        release round trips per cycle, which on the reporting machine
        meant 33 associations every 69 seconds against one source and
        roughly 49 000 per day across two, plus as many again against
        the local PACS.  Inside a session the same cycle needs three.

        Scope it to the QUERY phase only.  Holding a pooled association
        open across a long C-MOVE would leave it idle for minutes and
        invite the peer's idle timeout, which is the one thing this is
        not trying to survive.

        Re-entrant: a nested ``with`` is a no-op and the outermost block
        keeps ownership, so a helper can open one defensively without
        having to know whether its caller already did.

        Failure is never fatal to the caller.  A pooled association that
        breaks mid-query is dropped from the pool and the next query
        reconnects, which is exactly what the unpooled code did anyway.
        """
        if self._find_pool is not None:
            # Already inside a session: the outer block owns the pool
            # and will release it.
            yield
            return
        self._find_pool = {}
        try:
            yield
        finally:
            pool, self._find_pool = self._find_pool, None
            for endpoint, assoc in pool.items():
                try:
                    assoc.release()
                except Exception as e:
                    # Releasing is best-effort: the peer may already be
                    # gone, and a failure here must not mask whatever
                    # the block itself was doing.
                    logger.warning(
                        f"[{self.remote_name}] releasing pooled "
                        f"{endpoint} association failed: {e}")

    def _find_association(self, target: str, config: Dict[str, Any]
                          ) -> Tuple[Association, bool]:
        """Return ``(association, pooled)`` for a C-FIND to *target*.

        *pooled* is True when the association belongs to an open
        ``find_session`` and the caller must NOT release it.  An entry
        that is no longer established (peer aborted, idle timeout) is
        replaced rather than handed out.
        """
        pool = self._find_pool
        if pool is None:
            return self._associate(config), False
        assoc = pool.get(target)
        if assoc is not None and assoc.is_established:
            return assoc, True
        # Either nothing pooled yet or the pooled one went stale.  A
        # stale entry is dropped first so a failing _associate below
        # cannot leave a dead association in the pool for the next
        # query to pick up.
        pool.pop(target, None)
        assoc = self._associate(config)
        pool[target] = assoc
        return assoc, True

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
        # ``local`` here means the inventory endpoint, which is not
        # necessarily the C-MOVE destination — see ``local_query_config``.
        config = (self.local_query_config if target == 'local'
                  else self.remote_config)
        results = []
        # ``_associate`` raises PacsConnectionError when the PACS is
        # unreachable; that propagates to the engine (which surfaces
        # the "not reachable" popup).  A DIMSE error mid-iteration is a
        # different beast — the association WAS established — so it's
        # caught below and yields whatever partial results arrived,
        # flagged as incomplete.
        complete = True
        assoc, pooled = self._find_association(target, config)
        released = False
        try:
            # Append as they arrive: a break mid-stream must leave the
            # datasets received so far in ``results`` (see the
            # ``complete`` flag above).
            for dataset in self._iter_find_results(assoc, query_ds):
                results.append(dataset)
        except Exception as e:
            logger.error(f"C-FIND error: {e}")
            complete = False
            # Whatever broke this query breaks the next one too if the
            # association is handed out again, so a pooled one is
            # evicted and released here; the next query in the session
            # reconnects.
            if pooled and self._find_pool is not None:
                self._find_pool.pop(target, None)
            assoc.release()
            released = True
        finally:
            if not pooled and not released:
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

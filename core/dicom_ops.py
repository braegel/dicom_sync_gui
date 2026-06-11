"""
DICOM network operations: C-ECHO, C-FIND, C-MOVE.
Abstracted from the original CLI script for GUI use.
"""

import logging
import warnings
from typing import Dict, List, Optional, Tuple

from pydicom import Dataset
from pydicom.uid import (
    ExplicitVRLittleEndian, ImplicitVRLittleEndian, JPEG2000Lossless,
    JPEGLosslessSV1, JPEGLossless, DeflatedExplicitVRLittleEndian,
)
from pynetdicom import AE
from pynetdicom.sop_class import (
    PatientRootQueryRetrieveInformationModelFind,
    PatientRootQueryRetrieveInformationModelMove,
    StudyRootQueryRetrieveInformationModelFind,
    StudyRootQueryRetrieveInformationModelMove,
    Verification,
)

logger = logging.getLogger("dicom_sync")

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


def parse_dicom_time(time_str: str) -> str:
    if not time_str:
        return ""
    # Strip the fractional part and pad to HHMMSS; slicing a too-short
    # string just yields a shorter string, so no error handling needed.
    time_str = time_str.split('.')[0].ljust(6, '0')
    return f"{time_str[0:2]}:{time_str[2:4]}"


def parse_dicom_date(date_str: str) -> str:
    if not date_str or len(date_str) != 8:
        return date_str or ""
    return f"{date_str[6:8]}.{date_str[4:6]}.{date_str[0:4]}"


class DicomOperations:
    """Handles all DICOM network operations."""

    def __init__(self, local_config: Dict, remote_config: Dict, remote_name: str = ""):
        self.local_config = local_config
        self.remote_config = remote_config
        self.remote_name = remote_name
        self.transfer_syntax = TRANSFER_SYNTAXES.get(
            remote_config.get('transfer_syntax', 'JPEG2000Lossless'), JPEG2000Lossless)
        # C-MOVE destination is always the per-source local config.
        # The local_config already contains the correct AE title, port, etc.
        # for this specific source PACS.
        self.move_dest_config = local_config

        self.ae = AE(ae_title=local_config.get('ae_title', 'LOCAL_AE'))
        self.ae.acse_timeout = ACSE_TIMEOUT_S
        self.ae.dimse_timeout = DIMSE_TIMEOUT_S
        self.ae.network_timeout = NETWORK_TIMEOUT_S
        # Offer the configured per-node transfer syntax as the
        # *preferred* syntax on the query/retrieve contexts.  This only
        # affects negotiation of this query association (C-FIND/C-MOVE
        # requests carry identifier datasets, never pixel data); the
        # transfer syntax of the actual image transfer during a C-MOVE
        # is negotiated between the source PACS and the destination
        # Store SCP on a separate association this AE does not control.
        # Explicit VR LE and Implicit VR LE stay in the list as
        # fallbacks — Implicit VR LE is the DICOM baseline every SCP
        # must support — so interoperability cannot regress.  Skip
        # duplicates in case the configured syntax already is one of
        # the two fallbacks.
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

    def close(self):
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

    def c_echo(self, target: str = 'remote') -> bool:
        config = self.local_config if target == 'local' else self.remote_config
        try:
            assoc = self.ae.associate(config['ip_address'], config['port'],
                                     ae_title=config['ae_title'])
            if assoc.is_established:
                try:
                    status = assoc.send_c_echo()
                    if status is None:
                        return False
                    return getattr(status, "Status", 0xFFFF) == 0x0000
                finally:
                    assoc.release()
        except Exception as e:
            logger.debug(f"C-ECHO failed: {e}")
        return False

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
        ds = Dataset()
        ds.QueryRetrieveLevel = 'SERIES'
        ds.StudyInstanceUID = study_uid
        ds.SeriesInstanceUID = ''
        ds.SeriesNumber = ''
        ds.Modality = ''
        ds.SeriesDescription = ''
        ds.NumberOfSeriesRelatedInstances = ''
        ds.InstitutionName = ''
        return self._execute_find(ds)

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

    def _execute_find(self, query_ds: Dataset, target: str = 'remote') -> List[Dataset]:
        config = self.local_config if target == 'local' else self.remote_config
        results = []
        try:
            assoc = self.ae.associate(config['ip_address'], config['port'],
                                     ae_title=config['ae_title'])
            if assoc.is_established:
                # ``try/finally`` so a DIMSE-level error mid-iteration
                # doesn't leave the TCP association dangling until the
                # OS socket timeout.
                try:
                    # Suppress pydicom's "VR UI value length exceeds
                    # maximum" warning that some PACS implementations
                    # trigger; scoped to the call site, not process-wide.
                    with warnings.catch_warnings():
                        warnings.filterwarnings(
                            'ignore',
                            message='.*value length.*exceeds the maximum length.*VR UI.*')
                        for status, dataset in assoc.send_c_find(
                                query_ds, StudyRootQueryRetrieveInformationModelFind):
                            if status is None or dataset is None:
                                continue
                            if getattr(status, "Status", 0) in (0xFF00, 0xFF01):
                                results.append(dataset)
                finally:
                    assoc.release()
        except Exception as e:
            logger.error(f"C-FIND error: {e}")
        return results

    def c_move_series(self, study_uid: str, series_uid: str) -> Tuple[bool, int]:
        ds = Dataset()
        ds.QueryRetrieveLevel = 'SERIES'
        ds.StudyInstanceUID = study_uid
        ds.SeriesInstanceUID = series_uid
        return self._execute_move(ds)

    def c_move_image(self, study_uid: str, series_uid: str, sop_uid: str) -> Tuple[bool, int]:
        ds = Dataset()
        ds.QueryRetrieveLevel = 'IMAGE'
        ds.StudyInstanceUID = study_uid
        ds.SeriesInstanceUID = series_uid
        ds.SOPInstanceUID = sop_uid
        return self._execute_move(ds)

    def _execute_move(self, query_ds: Dataset) -> Tuple[bool, int]:
        move_dest = self.move_dest_config.get('ae_title', self.local_config.get('ae_title'))
        success, images = False, 0
        try:
            assoc = self.ae.associate(
                self.remote_config['ip_address'], self.remote_config['port'],
                ae_title=self.remote_config['ae_title'])
            if assoc.is_established:
                # ``try/finally`` so a mid-stream DIMSE failure does not
                # leak the established TCP association.
                try:
                    for status, _ in assoc.send_c_move(
                            query_ds, move_dest, StudyRootQueryRetrieveInformationModelMove):
                        if status is None:
                            continue
                        if getattr(status, "Status", None) == 0x0000:
                            success = True
                        completed = getattr(
                            status, 'NumberOfCompletedSuboperations', None)
                        if completed is not None:
                            images = completed
                finally:
                    assoc.release()
        except Exception as e:
            logger.error(f"C-MOVE error: {e}")
        return success, images

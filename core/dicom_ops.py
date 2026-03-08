"""
DICOM network operations: C-ECHO, C-FIND, C-MOVE.
Abstracted from the original CLI script for GUI use.
"""

import logging
import warnings
from typing import Dict, List, Optional, Set, Tuple, Any

warnings.filterwarnings('ignore', message='.*value length.*exceeds the maximum length.*VR UI.*')

from pydicom import Dataset
from pydicom.uid import (
    ExplicitVRLittleEndian, ImplicitVRLittleEndian, JPEG2000Lossless,
    JPEGLosslessSV1, JPEGLossless, DeflatedExplicitVRLittleEndian,
)
from pynetdicom import AE, evt, StoragePresentationContexts
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


def parse_dicom_time(time_str: str) -> str:
    if not time_str:
        return ""
    time_str = time_str.split('.')[0].ljust(6, '0')
    try:
        return f"{time_str[0:2]}:{time_str[2:4]}"
    except (IndexError, ValueError):
        return time_str


def parse_dicom_date(date_str: str) -> str:
    if not date_str or len(date_str) != 8:
        return date_str or ""
    try:
        return f"{date_str[6:8]}.{date_str[4:6]}.{date_str[0:4]}"
    except (IndexError, ValueError):
        return date_str


class DicomOperations:
    """Handles all DICOM network operations."""

    def __init__(self, local_config: Dict, remote_config: Dict, remote_name: str = ""):
        self.local_config = local_config
        self.remote_config = remote_config
        self.remote_name = remote_name
        self.transfer_syntax = TRANSFER_SYNTAXES.get(
            remote_config.get('transfer_syntax', 'JPEG2000Lossless'), JPEG2000Lossless)
        self.move_dest_config = remote_config.get('local_config', local_config)

        self.ae = AE(ae_title=local_config.get('ae_title', 'LOCAL_AE'))
        for ctx in [PatientRootQueryRetrieveInformationModelFind,
                    PatientRootQueryRetrieveInformationModelMove,
                    StudyRootQueryRetrieveInformationModelFind,
                    StudyRootQueryRetrieveInformationModelMove, Verification]:
            self.ae.add_requested_context(ctx)

    def c_echo(self, target: str = 'remote') -> bool:
        config = self.local_config if target == 'local' else self.remote_config
        try:
            assoc = self.ae.associate(config['ip_address'], config['port'],
                                     ae_title=config['ae_title'])
            if assoc.is_established:
                status = assoc.send_c_echo()
                assoc.release()
                return status and status.Status == 0x0000
        except Exception as e:
            logger.debug(f"C-ECHO failed: {e}")
        return False

    def c_find_studies(self, study_date: str = None, patient_id: str = None,
                       study_uid: str = None) -> List[Dataset]:
        ds = Dataset()
        ds.QueryRetrieveLevel = 'STUDY'
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
        return self._execute_find(ds)

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
                for status, dataset in assoc.send_c_find(
                        query_ds, StudyRootQueryRetrieveInformationModelFind):
                    if status and status.Status in (0xFF00, 0xFF01) and dataset:
                        results.append(dataset)
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
                for status, _ in assoc.send_c_move(
                        query_ds, move_dest, StudyRootQueryRetrieveInformationModelMove):
                    if status:
                        if status.Status == 0x0000:
                            success = True
                        if hasattr(status, 'NumberOfCompletedSuboperations'):
                            images = status.NumberOfCompletedSuboperations
                assoc.release()
        except Exception as e:
            logger.error(f"C-MOVE error: {e}")
        return success, images

"""
Core package: PACS querying, transfer orchestration, persistence.

Layering contract — read this before assuming ``core`` is Qt-free:

* ``core.config``, ``core.dicom_ops``, ``core.transfer_log``,
  ``core.queue_planner``, ``core.stats_utils`` and ``core.i18n`` are
  genuinely framework-free and importable without a Qt runtime.
* ``core.transfer_engine`` and ``core.storage_scp`` import PySide6.
  They publish their progress through ``QObject``/``Signal`` because
  every consumer is a widget and Qt's queued connections are what
  marshals the engine's service-loop thread (and pynetdicom's reactor
  thread) onto the GUI thread.  Reimplementing that hand-off over a
  plain callback interface would mean rebuilding thread-affinity
  dispatch that Qt already provides correctly.

So the boundary is "no widgets in core", NOT "no Qt in core".  The
practical consequence: importing ``core.transfer_engine`` pulls in
PySide6, and its tests need a ``QApplication``.  ``core`` must never
import from ``gui``.
"""

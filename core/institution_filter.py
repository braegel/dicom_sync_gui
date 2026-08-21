"""
Pure institution-filter decision for the transfer engine.

The user groups institutions and picks which groups the dashboard
downloads.  The rule set is small but has three distinct outcomes that
were easy to conflate while it lived inline in the engine next to a Qt
signal emit:

* filtering off, or no group selected  → download (no filtering active)
* institution assigned to an ACTIVE group    → download
* institution assigned to an INACTIVE group  → skip
* institution not assigned to any group      → download AND report it
  as unknown, so the GUI can ask the user where it belongs

The last case is why this returns a verdict object rather than a bool:
"download it" and "tell the user about it" are two answers, and the
engine's caller owns the second (it emits the Qt signal and keeps the
already-notified set).  Keeping the decision pure means the truth table
can be tested without a signal spy.

Qt-free, no I/O — same contract as ``core.queue_planner``.
"""

from dataclasses import dataclass
from typing import Dict, Iterable

__all__ = ["InstitutionVerdict", "evaluate"]


@dataclass(frozen=True)
class InstitutionVerdict:
    """Outcome of the filter for one institution name."""

    #: Whether the study should be downloaded.
    allowed: bool
    #: Whether the institution is unassigned and worth reporting to the
    #: user.  Only ever True together with ``allowed`` — an unknown
    #: institution is downloaded, not skipped.  False for the empty
    #: name: a PACS that omits InstitutionName is not something the
    #: user can assign to a group.
    is_unknown: bool = False


def evaluate(institution_name: str, *,
             filtering_enabled: bool,
             active_groups: Iterable[str],
             assignments: Dict[str, str]) -> InstitutionVerdict:
    """Decide whether a study from *institution_name* is downloaded.

    *assignments* maps institution name → group name; an institution
    absent from it (or mapped to the empty string) counts as unassigned.
    An empty *active_groups* means the user enabled filtering but
    selected nothing, which is treated as "no filtering active" rather
    than "block everything" — the latter would silently stop all
    downloads on a half-finished configuration.
    """
    if not filtering_enabled:
        return InstitutionVerdict(allowed=True)

    active = set(active_groups)
    if not active:
        return InstitutionVerdict(allowed=True)

    assigned_group = assignments.get(institution_name, "")
    if not assigned_group:
        return InstitutionVerdict(
            allowed=True, is_unknown=bool(institution_name))

    return InstitutionVerdict(allowed=assigned_group in active)

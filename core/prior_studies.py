"""
Pure selection rules for prior studies (Voruntersuchungen).

``TransferEngine`` optionally pulls a patient's previous studies
alongside the current ones.  Deciding WHICH previous studies qualify —
newest first, optionally restricted to the modalities the current
studies use, capped at a configured count — is a self-contained rule
set with no I/O and no engine state.  It lives here for the same reason
``core.queue_planner`` does: so the rules can be tested against plain
datasets instead of a running service loop.

The PACS query that produces the candidates, and the job construction
that consumes the result, stay in the engine — those need an
association and the engine's job factory.

Qt-free and side-effect-free apart from an optional *log* callback,
which the engine wires to its user-visible log so the "why did I get
these three priors" trail keeps showing up in the log window.
"""

from typing import Any, Callable, List, Set

__all__ = [
    "split_modalities", "sort_newest_first",
    "filter_by_modality", "select_priors",
]

# A no-op sink, so callers that don't care about the trail can omit it.
LogFn = Callable[[str], None]


def _no_log(_message: str) -> None:
    pass


def split_modalities(study: Any) -> Set[str]:
    """The modality set of *study* from its ``ModalitiesInStudy``.

    Handles all three shapes the value actually arrives in:

    * a pydicom ``MultiValue`` — what a conformant ``CT\\SR`` becomes
      once parsed.  ``str()`` on it yields ``"['CT', 'SR']"``, so the
      obvious "stringify and split" produces the tokens ``['CT'`` and
      ``'SR']`` and the study then matches nothing.  That was a real
      bug: with the modality filter on, a current study with more than
      one modality discarded EVERY prior, because its target set was
      pure punctuation.
    * a plain ``str`` — a single-modality study, or a PACS that hands
      the multi-value over unparsed as ``"CT\\SR"``.
    * comma-separated ``"CT,SR"`` — not standard, but seen in the wild.

    Empty segments are dropped; a study without the tag yields the
    empty set.
    """
    raw = getattr(study, 'ModalitiesInStudy', '')
    if raw is None:
        return set()
    # MultiValue is a sequence, not a str — take its elements directly
    # rather than round-tripping through repr().
    if not isinstance(raw, (str, bytes)) and hasattr(raw, "__iter__"):
        parts = [str(m) for m in raw]
    else:
        parts = str(raw).replace("\\", ",").split(",")
    return {p.strip() for p in parts if p.strip()}


def sort_newest_first(studies: List[Any]) -> List[Any]:
    """Return *studies* ordered newest first by ``StudyDate``/``StudyTime``.

    A fresh list, so the caller's input is never reordered under it.
    """
    return sorted(
        studies,
        key=lambda s: (getattr(s, 'StudyDate', ''),
                       getattr(s, 'StudyTime', '')),
        reverse=True)


def filter_by_modality(prior_studies: List[Any],
                       current_studies: List[Any],
                       patient_id: str,
                       log: LogFn = _no_log) -> List[Any]:
    """Keep the priors whose modality set intersects the modalities of
    *patient_id*'s CURRENT studies.

    When the current studies carry no modality information at all the
    filter cannot say anything, so everything is kept — dropping
    every prior because a PACS omitted ``ModalitiesInStudy`` would
    silently disable the feature.
    """
    target: Set[str] = set()
    for cs in current_studies:
        if getattr(cs, 'PatientID', '') == patient_id:
            target |= split_modalities(cs)
    log(f"  [Prior] modality filter active, target: {target}")
    if not target:
        return prior_studies
    kept = [s for s in prior_studies if split_modalities(s) & target]
    log(f"  [Prior] {len(kept)} after modality filter")
    return kept


def select_priors(prior_studies: List[Any],
                  current_studies: List[Any],
                  patient_id: str,
                  *,
                  same_modality: bool,
                  count: int,
                  log: LogFn = _no_log) -> List[Any]:
    """Pick the priors to download: newest first, optionally
    modality-matched, truncated to *count*.

    *count* is the configured maximum; fewer are returned when fewer
    qualify.  A *count* of zero (feature disabled) returns nothing,
    and a negative one is treated as zero rather than slicing from the
    end — ``[:-1]`` would silently return all but the newest.
    """
    ordered = sort_newest_first(prior_studies)
    if same_modality:
        ordered = filter_by_modality(
            ordered, current_studies, patient_id, log)
    take = max(min(count, len(ordered)), 0)
    log(f"  [Prior] downloading {take} of {len(ordered)} "
        f"(configured max: {count})")
    return ordered[:take]

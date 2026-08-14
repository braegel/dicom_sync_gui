"""
DICOM date/time parsing and display formatting.

DICOM hands out dates as ``YYYYMMDD`` and times as ``HHMMSS`` (with an
optional fractional part), and every window in this app has to turn
those into something a radiologist can read.  That conversion used to be
hand-rolled in four places — the queue's "Series Created" column, the
completions window, the examination lookup, and a pair of helpers in
``core.dicom_ops`` that no production code called any more — each with
slightly different tolerance for malformed input.  One module now owns
the rules.

Pure, Qt-free, no I/O — same contract as ``core.stats_utils``.

Two conventions the whole app relies on:

* **Never raise on bad input.**  These strings come off the wire from
  PACS implementations that do not always follow the standard, and a
  malformed timestamp must degrade to "show it verbatim" rather than
  take down a table render.
* **Empty in, empty out.**  Callers decide what a missing value looks
  like on screen (usually an em dash), so nothing here invents a
  placeholder.
"""

from typing import Optional, Tuple

__all__ = [
    "parse_time", "format_date", "format_hhmm", "format_hhmmss",
    "format_date_time", "format_duration",
]


def parse_time(value: str) -> Optional[Tuple[int, int, int]]:
    """Parse ``HH:MM:SS`` or DICOM ``HHMMSS`` into ``(h, m, s)``.

    Accepts either presentation form because the value may come straight
    off the wire (``HHMMSS``) or back out of a widget that already
    formatted it (``HH:MM:SS``).  A four-digit ``HHMM`` is accepted with
    seconds defaulting to 0.  Returns ``None`` when *value* cannot be
    parsed; never raises.
    """
    value = (value or "").strip()
    if ":" in value:
        parts = value.split(":")
    elif len(value) >= 6:
        parts = [value[:2], value[2:4], value[4:6]]
    elif len(value) >= 4:
        parts = [value[:2], value[2:4], "0"]
    else:
        return None
    try:
        return (int(parts[0]), int(parts[1]),
                int(parts[2]) if len(parts) > 2 else 0)
    except (ValueError, IndexError):
        return None


def format_date(digits: str) -> str:
    """``YYYYMMDD`` → ``DD.MM.YYYY``.

    Anything that is not exactly eight digits is passed through verbatim
    — a PACS that sends a partial date is better shown as-is than
    silently blanked.
    """
    d = (digits or "").strip()
    if len(d) == 8 and d.isdigit():
        return f"{d[6:8]}.{d[4:6]}.{d[0:4]}"
    return d


def format_hhmm(digits: str) -> str:
    """DICOM ``HHMMSS`` → ``HH:MM``; ``""`` when the leading four
    characters are not digits (too short to mean anything)."""
    t = (digits or "").strip()
    if len(t) >= 4 and t[:4].isdigit():
        return f"{t[0:2]}:{t[2:4]}"
    return ""


def format_hhmmss(digits: str) -> str:
    """DICOM ``HHMMSS`` → ``HH:MM:SS``.

    Shorter values are passed through verbatim, matching
    :py:func:`format_date`'s "show what we got" rule.
    """
    t = (digits or "").strip()
    if len(t) >= 6:
        return f"{t[0:2]}:{t[2:4]}:{t[4:6]}"
    return t


def format_date_time(date_digits: str, time_digits: str,
                     empty: str = "") -> str:
    """``(YYYYMMDD, HHMMSS)`` → ``DD.MM.YYYY HH:MM``.

    Degrades one part at a time: a missing time yields the date alone, a
    missing date the time alone, and *empty* only when both are absent.
    """
    date_part = format_date(date_digits)
    time_part = format_hhmm(time_digits)
    if date_part and time_part:
        return f"{date_part} {time_part}"
    return date_part or time_part or empty


def format_duration(seconds: float) -> str:
    """Seconds → ``m:ss``, or ``h:mm:ss`` once it reaches an hour.

    Minutes are NOT zero-padded in the short form (``9:05``, not
    ``09:05``) — that is the format the ETE column and the completions
    countdown have always used.  Negative input is treated as zero.
    """
    total = max(int(seconds), 0)
    if total >= 3600:
        h, rest = divmod(total, 3600)
        m, s = divmod(rest, 60)
        return f"{h}:{m:02d}:{s:02d}"
    m, s = divmod(total, 60)
    return f"{m}:{s:02d}"

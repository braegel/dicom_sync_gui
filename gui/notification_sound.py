"""
Notification sound synthesis for download-completion chimes.

Generates the two-tone WAV files played by ``SourceDashboard`` when a
study finishes downloading: an ascending "cheerful" interval for the
normal case and a descending "somber" one when transfer speed is in
the red band.  The generated files live in the system temp dir for the
whole app run and are cached at module level, so the ~30k-sample DSP
cost is paid at most twice per process.

This module is the single owner of the cache state
(``_default_sound_path`` / ``_sad_sound_path``); ``gui.dashboard``
re-exports ``_generate_default_sound`` for backwards compatibility but
holds no state of its own.
"""

import atexit
import contextlib
import io
import math
import os
import struct
import tempfile
import wave

_default_sound_path: str | None = None
_sad_sound_path: str | None = None

_NORMAL_FREQ_2 = 1174  # D6 — ascending interval from A5
_SAD_FREQ_2 = 660      # E5 — descending interval from A5


def _remove_quietly(path: str):
    """Best-effort file removal for the atexit cleanup of generated
    notification WAVs.  ``atexit.register(os.remove, path)`` would
    raise (and print a traceback) at interpreter shutdown if the file
    was already gone — e.g. the OS purged the temp dir, or a test
    cleared the module-level cache and regenerated the file."""
    with contextlib.suppress(OSError):
        os.remove(path)


def _generate_default_sound(sad: bool = False) -> str:
    """Generate a two-tone notification WAV and return its path.

    Normal mode: A5 (880 Hz) → D6 (1174 Hz) — ascending, cheerful.
    Sad mode:    A5 (880 Hz) → E5 (660 Hz)  — descending, somber.
    """
    global _default_sound_path, _sad_sound_path
    cached = _sad_sound_path if sad else _default_sound_path
    if cached and os.path.exists(cached):
        return cached

    freq2 = _SAD_FREQ_2 if sad else _NORMAL_FREQ_2
    sample_rate = 44100
    duration = 0.68
    n_samples = int(sample_rate * duration)

    def _envelope(t: float, start: float, dur: float) -> float:
        rel = t - start
        if rel < 0.02:
            return 0.001 * (300.0) ** (rel / 0.02)
        remaining = dur - 0.02
        return 0.3 * (0.001 / 0.3) ** ((rel - 0.02) / remaining)

    raw = []
    for i in range(n_samples):
        t = i / sample_rate
        val = 0.0
        if t < 0.4:
            val += _envelope(t, 0, 0.4) * math.sin(2 * math.pi * 880 * t)
        if 0.18 <= t < 0.68:
            val += _envelope(t, 0.18, 0.5) * math.sin(
                2 * math.pi * freq2 * t)
        raw.append(int(max(-1.0, min(1.0, val)) * 32767))

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(struct.pack(f"<{len(raw)}h", *raw))

    fd, path = tempfile.mkstemp(suffix=".wav", prefix="dicom_notify_")
    os.write(fd, buf.getvalue())
    os.close(fd)
    # The WAV lives in the system temp dir for the whole app run (the
    # module-level cache above reuses it); without explicit cleanup we
    # leak up to two orphan files per run.  Quiet removal so a file
    # that vanished before exit doesn't raise during shutdown.
    atexit.register(_remove_quietly, path)
    if sad:
        _sad_sound_path = path
    else:
        _default_sound_path = path
    return path

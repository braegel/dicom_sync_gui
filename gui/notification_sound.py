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
_siren_sound_path: str | None = None

_NORMAL_FREQ_2 = 1174  # D6 — ascending interval from A5
_SAD_FREQ_2 = 660      # E5 — descending interval from A5

# Siren sweep bounds (Hz) for the PACS-unreachable alarm.  A wailing
# up/down glissando, deliberately distinct from the two-tone chimes so
# the user can tell "can't reach the PACS" apart from "slow download".
_SIREN_LOW = 600
_SIREN_HIGH = 1200


def _remove_quietly(path: str) -> None:
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


def _generate_siren_sound() -> str:
    """Generate a wailing two-cycle siren WAV and return its path.

    A continuous sine whose frequency sweeps _SIREN_LOW ↔ _SIREN_HIGH
    twice — used as the recurring PACS-unreachable alarm, distinct from
    the chime tones so the user recognises it instantly.  Cached at
    module level like the chimes (generated at most once per run)."""
    global _siren_sound_path
    if _siren_sound_path and os.path.exists(_siren_sound_path):
        return _siren_sound_path

    sample_rate = 44100
    duration = 1.4          # two ~0.7 s wails
    cycles = 2.0            # number of low→high→low sweeps
    n_samples = int(sample_rate * duration)
    mid = (_SIREN_LOW + _SIREN_HIGH) / 2.0
    half_span = (_SIREN_HIGH - _SIREN_LOW) / 2.0

    raw = []
    phase = 0.0
    for i in range(n_samples):
        t = i / sample_rate
        # Triangle-ish sweep via a sine LFO on the frequency.
        freq = mid + half_span * math.sin(
            2 * math.pi * cycles * t / duration)
        phase += 2 * math.pi * freq / sample_rate
        # Short fade in/out so the loop edges don't click.
        if t < 0.02:
            amp = t / 0.02
        elif t > duration - 0.02:
            amp = max(0.0, (duration - t) / 0.02)
        else:
            amp = 1.0
        val = 0.35 * amp * math.sin(phase)
        raw.append(int(max(-1.0, min(1.0, val)) * 32767))

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(struct.pack(f"<{len(raw)}h", *raw))

    fd, path = tempfile.mkstemp(suffix=".wav", prefix="dicom_siren_")
    os.write(fd, buf.getvalue())
    os.close(fd)
    atexit.register(_remove_quietly, path)
    _siren_sound_path = path
    return path

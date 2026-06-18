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

# Shared audio format for all generated WAVs (mono, 16-bit).
SAMPLE_RATE = 44100

_NORMAL_FREQ_2 = 1174  # D6 — ascending interval from A5
_SAD_FREQ_2 = 660      # E5 — descending interval from A5

# Two-tone chime envelope timing (seconds).
_CHIME_DURATION = 0.68          # total clip length
_CHIME_TONE1_END = 0.4          # first tone (A5) sustain end
_CHIME_TONE2_START = 0.18       # second tone fades in here
_CHIME_TONE2_DUR = 0.5          # second tone envelope duration
_ENV_ATTACK = 0.02              # exponential attack time per tone
_ENV_ATTACK_GAIN = 300.0        # attack curve steepness
_ENV_START_LEVEL = 0.001        # envelope floor (start of attack / end of decay)
_ENV_PEAK_LEVEL = 0.3           # envelope peak after attack

# Siren sweep bounds (Hz) for the PACS-unreachable alarm.  A wailing
# up/down glissando, deliberately distinct from the two-tone chimes so
# the user can tell "can't reach the PACS" apart from "slow download".
_SIREN_LOW = 600
_SIREN_HIGH = 1200

# Siren sweep timing/shape.
_SIREN_DURATION = 1.4           # two ~0.7 s wails
_SIREN_CYCLES = 2.0             # number of low→high→low sweeps
_SIREN_FADE = 0.02              # fade in/out so loop edges don't click
_SIREN_AMP = 0.35               # peak amplitude


def _remove_quietly(path: str) -> None:
    """Best-effort file removal for the atexit cleanup of generated
    notification WAVs.  ``atexit.register(os.remove, path)`` would
    raise (and print a traceback) at interpreter shutdown if the file
    was already gone — e.g. the OS purged the temp dir, or a test
    cleared the module-level cache and regenerated the file."""
    with contextlib.suppress(OSError):
        os.remove(path)


def _write_wav_tempfile(raw_samples: list[int], sample_rate: int,
                        prefix: str) -> str:
    """Write *raw_samples* (16-bit signed mono PCM) to a temp WAV file
    and return its path.

    Shared tail for the chime/siren synth functions: pack the samples,
    write a mono 16-bit WAV, persist it to the system temp dir, and
    register an atexit cleanup so the file doesn't leak for the run.
    """
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(struct.pack(f"<{len(raw_samples)}h", *raw_samples))

    fd, path = tempfile.mkstemp(suffix=".wav", prefix=prefix)
    os.write(fd, buf.getvalue())
    os.close(fd)
    # The WAV lives in the system temp dir for the whole app run (the
    # module-level cache reuses it); without explicit cleanup we leak an
    # orphan file per generated sound.  Quiet removal so a file that
    # vanished before exit doesn't raise during shutdown.
    atexit.register(_remove_quietly, path)
    return path


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
    n_samples = int(SAMPLE_RATE * _CHIME_DURATION)

    def _envelope(t: float, start: float, dur: float) -> float:
        rel = t - start
        if rel < _ENV_ATTACK:
            return _ENV_START_LEVEL * _ENV_ATTACK_GAIN ** (rel / _ENV_ATTACK)
        remaining = dur - _ENV_ATTACK
        return _ENV_PEAK_LEVEL * (_ENV_START_LEVEL / _ENV_PEAK_LEVEL) ** (
            (rel - _ENV_ATTACK) / remaining)

    raw = []
    for i in range(n_samples):
        t = i / SAMPLE_RATE
        val = 0.0
        if t < _CHIME_TONE1_END:
            val += _envelope(t, 0, _CHIME_TONE1_END) * math.sin(
                2 * math.pi * 880 * t)
        if _CHIME_TONE2_START <= t < _CHIME_DURATION:
            val += _envelope(t, _CHIME_TONE2_START, _CHIME_TONE2_DUR) * math.sin(
                2 * math.pi * freq2 * t)
        raw.append(int(max(-1.0, min(1.0, val)) * 32767))

    path = _write_wav_tempfile(raw, SAMPLE_RATE, "dicom_notify_")
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

    n_samples = int(SAMPLE_RATE * _SIREN_DURATION)
    mid = (_SIREN_LOW + _SIREN_HIGH) / 2.0
    half_span = (_SIREN_HIGH - _SIREN_LOW) / 2.0

    raw = []
    phase = 0.0
    for i in range(n_samples):
        t = i / SAMPLE_RATE
        # Triangle-ish sweep via a sine LFO on the frequency.
        freq = mid + half_span * math.sin(
            2 * math.pi * _SIREN_CYCLES * t / _SIREN_DURATION)
        phase += 2 * math.pi * freq / SAMPLE_RATE
        # Short fade in/out so the loop edges don't click.
        if t < _SIREN_FADE:
            amp = t / _SIREN_FADE
        elif t > _SIREN_DURATION - _SIREN_FADE:
            amp = max(0.0, (_SIREN_DURATION - t) / _SIREN_FADE)
        else:
            amp = 1.0
        val = _SIREN_AMP * amp * math.sin(phase)
        raw.append(int(max(-1.0, min(1.0, val)) * 32767))

    path = _write_wav_tempfile(raw, SAMPLE_RATE, "dicom_siren_")
    _siren_sound_path = path
    return path

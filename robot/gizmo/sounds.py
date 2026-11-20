"""Sound clips: short recordings a personality can play by itself, like a catchphrase or a sound effect.

A personality's clips are files in its folder (personalities.py). The model plays one by writing [sound:<name>]
at the start of a reply, the way it asks for a gesture, and the robot puts the recording on its speaker ahead
of the spoken words. That works with any voice, Nova Sonic's included, because nothing has to synthesise it.

Only WAV files are decoded here (Python's own `wave`: 8, 16, 24 or 32-bit PCM, any rate, any channel count),
so the robot needs no audio library. Only use recordings you have the right to use.
"""

from __future__ import annotations

import re
import wave
from pathlib import Path

import numpy as np

PEAK = 0.6  # every clip is brought to this peak, so a quiet recording and a hot one play about as loud as speech
FADE_S = 0.005  # at each end, so a clip cut mid-sound doesn't click
MAX_S = 20.0  # a clip is a moment, not a soundtrack


def sound_name(text: str) -> str:
    """The name a clip gets from its words, as the model writes it in a tag: "It's over there!" -> "its-over-there"."""
    words = re.sub(r"[^a-z0-9]+", "-", text.lower().replace("'", "").replace("’", "")).strip("-")
    return words[:32].rstrip("-") or "sound"


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    """A WAV file as mono float32 in -1..1, and its sample rate. Raises wave.Error, EOFError, OSError or ValueError."""
    with wave.open(str(path), "rb") as w:
        channels, width, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
        raw = w.readframes(min(w.getnframes(), int(rate * MAX_S)))
    if width == 1:  # 8-bit WAV is unsigned
        x = (np.frombuffer(raw, np.uint8).astype(np.float32) - 128) / 128
    elif width == 2:
        x = np.frombuffer(raw, "<i2").astype(np.float32) / 32768
    elif width == 3:
        b = np.frombuffer(raw, np.uint8).reshape(-1, 3).astype(np.int32)
        x = ((b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)) - ((b[:, 2] & 0x80) << 17)).astype(np.float32) / 8388608
    elif width == 4:
        x = np.frombuffer(raw, "<i4").astype(np.float32) / 2147483648
    else:
        raise ValueError(f"{width * 8}-bit audio isn't supported")
    if channels > 1:
        x = x[: x.size - x.size % channels].reshape(-1, channels).mean(axis=1)
    return x.astype(np.float32), rate


def to_rate(x: np.ndarray, src: int, dst: int) -> np.ndarray:
    """Resample, filtering first when going down (a 44.1 kHz clip on a 16 kHz speaker), so what's above the new
    rate's range doesn't fold back into the sound."""
    if src == dst or x.size == 0:
        return x
    if dst < src:
        taps = 2 * int(4 * src / dst) + 1  # a windowed-sinc low-pass just under the new Nyquist
        n = np.arange(taps) - taps // 2
        kernel = np.sinc(n * 0.9 * dst / src) * np.hanning(taps)
        x = np.convolve(x, kernel / kernel.sum(), mode="same")
    count = int(round(x.size * dst / src))
    return np.interp(np.linspace(0, x.size - 1, count), np.arange(x.size), x).astype(np.float32)


def load_clip(path: Path, rate: int) -> np.ndarray:
    """A clip ready for the speaker: mono float32 at `rate`, at a steady level, with its ends faded."""
    if path.suffix.lower() != ".wav":
        raise ValueError("only WAV recordings can be played from the robot itself")
    x, src = read_wav(path)
    x = to_rate(x, src, rate)
    peak = float(np.max(np.abs(x))) if x.size else 0.0
    if peak < 1e-4:
        raise ValueError("the recording is silent")
    x = x * (PEAK / peak)
    fade = min(int(rate * FADE_S), x.size // 2)
    if fade:
        ramp = np.linspace(0.0, 1.0, fade, dtype=np.float32)
        x[:fade] *= ramp
        x[-fade:] *= ramp[::-1]
    return x.astype(np.float32)


class Clips:
    """Decoded clips, kept until their file changes."""

    def __init__(self) -> None:
        self._kept: dict[tuple[str, int], tuple[float, np.ndarray]] = {}

    def get(self, path: Path, rate: int) -> np.ndarray:
        stamp = path.stat().st_mtime
        kept = self._kept.get((str(path), rate))
        if kept is None or kept[0] != stamp:
            kept = self._kept[(str(path), rate)] = (stamp, load_clip(path, rate))
        return kept[1]

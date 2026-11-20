"""Sound clips: a personality's recordings, by name, and decoding them for the speaker."""
import struct
import wave

import numpy as np
import pytest

from gizmo.personalities import Personalities
from gizmo.sounds import PEAK, Clips, load_clip, read_wav, sound_name, to_rate


def wav_bytes(path, rate=44100, seconds=0.5, hz=440.0, width=2, channels=1, level=0.2):
    t = np.arange(int(rate * seconds)) / rate
    x = level * np.sin(2 * np.pi * hz * t)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        if width == 2:
            frames = (x * 32767).astype("<i2")
            w.writeframes(np.repeat(frames, channels).tobytes())
        elif width == 3:
            w.writeframes(b"".join(struct.pack("<i", int(v * 8388607))[:3] * channels for v in x))
        else:
            w.writeframes(np.repeat(((x * 127) + 128).astype(np.uint8), channels).tobytes())
    return path.read_bytes()


def test_a_clips_name_comes_from_its_words():
    assert sound_name("It's over there!") == "its-over-there" and sound_name("Ta-da!") == "ta-da"
    assert sound_name("  A drum roll, please…  ") == "a-drum-roll-please" and sound_name("!!!") == "sound"
    assert len(sound_name("word " * 30)) <= 32 and not sound_name("word " * 30).endswith("-")


def test_a_clip_is_decoded_for_the_speaker(tmp_path):
    wav_bytes(tmp_path / "a.wav", rate=44100, seconds=0.5)
    x, rate = read_wav(tmp_path / "a.wav")
    assert rate == 44100 and x.size == 22050 and x.dtype == np.float32 and 0.19 < np.max(np.abs(x)) < 0.21
    clip = load_clip(tmp_path / "a.wav", 16000)
    assert clip.size == 8000 and abs(float(np.max(np.abs(clip))) - PEAK) < 0.02  # at the speaker's rate, at a steady level
    assert abs(clip[0]) < 1e-3 and abs(clip[-1]) < 1e-3  # faded at both ends
    # Other WAV shapes: stereo, 24-bit, 8-bit.
    for name, kw in (("stereo.wav", {"channels": 2}), ("deep.wav", {"width": 3}), ("old.wav", {"width": 1})):
        wav_bytes(tmp_path / name, **kw)
        y, _ = read_wav(tmp_path / name)
        assert y.size == 22050 and 0.17 < np.max(np.abs(y)) < 0.23, name
    # Going down in rate, what the new rate can't hold is filtered out instead of folding back as a false tone.
    high = 0.5 * np.sin(2 * np.pi * 15000 * np.arange(44100) / 44100).astype(np.float32)
    assert np.max(np.abs(to_rate(high, 44100, 16000)[100:-100])) < 0.01 and to_rate(high, 16000, 16000) is high
    low = 0.5 * np.sin(2 * np.pi * 440 * np.arange(44100) / 44100).astype(np.float32)
    assert 0.48 < np.max(np.abs(to_rate(low, 44100, 16000)[100:-100])) < 0.52  # and what it can hold comes through
    # What can't be played says why.
    (tmp_path / "song.mp3").write_bytes(b"ID3")
    with pytest.raises(ValueError, match="only WAV"):
        load_clip(tmp_path / "song.mp3", 16000)
    wav_bytes(tmp_path / "quiet.wav", level=0.0)
    with pytest.raises(ValueError, match="silent"):
        load_clip(tmp_path / "quiet.wav", 16000)
    clips = Clips()
    assert clips.get(tmp_path / "a.wav", 16000) is clips.get(tmp_path / "a.wav", 16000)  # decoded once


def test_a_personality_plays_its_clips_by_name(tmp_path):
    people = Personalities(tmp_path)
    data = wav_bytes(tmp_path / "x.wav")
    people.add_catchphrase("gizmo", "Ta-da!", "x.wav", data)
    people.add_catchphrase("gizmo", "A drum roll", "x.wav", data, name="Drums")
    people.add_catchphrase("gizmo", "Ta-da!", "x.wav", data)  # the same words again: its name gets a number
    assert people.sound_tags("gizmo") == {"ta-da": "Ta-da!", "drums": "A drum roll", "ta-da-2": "Ta-da!"}
    assert [c["name"] for c in people.summary("gizmo")["catchphrases"]] == ["ta-da", "drums", "ta-da-2"]
    assert all(c["plays_here"] for c in people.summary("gizmo")["catchphrases"])
    # A model's tag finds the clip by its name, its words, or a near miss.
    for tag in ("ta-da", "Ta-da!", "tada", "TA-DA"):
        assert people.sound("gizmo", tag)["text"] == "Ta-da!", tag
    assert people.sound("gizmo", "drums")["path"].name == "catch2.wav" and people.sound("gizmo", "a drum roll")["name"] == "drums"
    assert people.sound("gizmo", "fanfare") is None and people.sound("explorer", "ta-da") is None  # each has its own
    people.delete_catchphrase("gizmo", 0)
    assert people.sound_tags("gizmo") == {"drums": "A drum roll", "ta-da": "Ta-da!"}
    with pytest.raises(ValueError):
        people.add_catchphrase("gizmo", "  ", "x.wav", data)

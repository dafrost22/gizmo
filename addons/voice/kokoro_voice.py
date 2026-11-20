"""Built-in voices for Gizmo's voice server: Kokoro-82M, no cloning.

A personality picks one of Kokoro's English voices and tunes it (all optional):
    {"voice": "am_puck", "mix": "af_heart", "mix_amount": 0.3, "speed": 1.05, "pitch": 2.0, "robot": 0.2}
- mix, mix_amount: blend in a second voice's style (0..1).
- pitch: semitones. Kokoro speaks slower by the same ratio and the audio is then resampled faster, so the
  pace stays at `speed` with no phase-vocoder smear; formants move with the pitch (up sounds smaller).
- robot: 0..1, a short metallic comb under the voice.

Weights: the ONNX export (kokoro-onnx's model-files-v1.0 release) and its voice table (an .npz of plain
arrays), never Kokoro's .pth: downloaded pickles can run code. Phonemes come from misaki, Kokoro's own
G2P (its lexicon first, espeak-ng for unknown words). Runs on the CPU (about 0.5 s for a short sentence
on an 8-core desktop CPU), so it never queues behind F5 or the LLM on the GPU.

    models/kokoro/{kokoro-v1.0.onnx, voices-v1.0.bin, config.json}   (see README.md)
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path

import numpy as np

log = logging.getLogger("kokoro")

RATE = 24000
MAX_TOKENS = 500  # the model's context is 510 phonemes, with a pad at each end
ROBOT_DELAY_S = 0.0045
ENGLISH = re.compile(r"^[ab][fm]_[a-z]+$")  # American (a) and British (b) voices: misaki's English G2P


class Kokoro:
    def __init__(self, folder: Path, threads: int = 6) -> None:
        import onnxruntime as ort

        self.vocab: dict[str, int] = json.loads((folder / "config.json").read_text())["vocab"]
        with np.load(folder / "voices-v1.0.bin", allow_pickle=False) as table:
            self.styles = {name: table[name].astype(np.float32) for name in table.files if ENGLISH.match(name)}
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = threads
        self.session = ort.InferenceSession(str(folder / "kokoro-v1.0.onnx"), opts, providers=["CPUExecutionProvider"])
        self.input_names = {i.name: i.type for i in self.session.get_inputs()}
        self._g2p: dict[bool, object] = {}
        self.lock = threading.Lock()

    def voices(self) -> list[str]:
        return sorted(self.styles)

    def _phonemes(self, text: str, british: bool) -> str:
        if british not in self._g2p:
            from misaki import en, espeak

            self._g2p[british] = en.G2P(trf=False, british=british, fallback=espeak.EspeakFallback(british=british))
        phonemes, _ = self._g2p[british](text)
        return phonemes

    def check(self, params: dict) -> dict:
        """The tuning, clamped, with unknown voices refused (ValueError)."""
        voice = str(params.get("voice") or "")
        if voice not in self.styles:
            raise ValueError(f"unknown voice {voice!r}: one of {', '.join(self.voices())}")
        mix = params.get("mix") or None
        if mix is not None and mix not in self.styles:
            raise ValueError(f"unknown voice to mix {mix!r}")

        def num(key: str, lo: float, hi: float, default: float) -> float:
            try:
                return max(lo, min(hi, float(params.get(key, default))))
            except (TypeError, ValueError):
                return default

        return {"voice": voice, "mix": mix, "mix_amount": num("mix_amount", 0.0, 1.0, 0.0),
                "speed": num("speed", 0.6, 1.6, 1.0), "pitch": num("pitch", -6.0, 6.0, 0.0),
                "robot": num("robot", 0.0, 1.0, 0.0)}

    def _style(self, p: dict, n_tokens: int) -> np.ndarray:
        style = self.styles[p["voice"]][n_tokens]
        if p["mix"] and p["mix_amount"] > 0:
            style = (1 - p["mix_amount"]) * style + p["mix_amount"] * self.styles[p["mix"]][n_tokens]
        return style.reshape(1, -1).astype(np.float32)

    def _run(self, tokens: list[int], style: np.ndarray, speed: float) -> np.ndarray:
        ids = np.array([[0, *tokens, 0]], dtype=np.int64)
        if "input_ids" in self.input_names:  # newer exports
            feed = {"input_ids": ids, "style": style, "speed": np.array([speed], dtype=np.float32)}
            if "int32" in self.input_names.get("speed", ""):
                feed["speed"] = np.array([round(speed)], dtype=np.int32)
        else:
            feed = {"tokens": ids, "style": style, "speed": np.array([speed], dtype=np.float32)}
        return self.session.run(None, feed)[0].reshape(-1)

    def say(self, text: str, params: dict) -> tuple[np.ndarray, int, float]:
        """One sentence -> (float32 audio, its sample rate, ms taken). The rate already carries the pitch:
        resampling it to the speaker's rate makes the pitch shift."""
        p = self.check(params)
        ratio = 2 ** (p["pitch"] / 12)
        with self.lock:
            t0 = time.perf_counter()
            phonemes = self._phonemes(text, british=p["voice"].startswith("b"))
            tokens = [self.vocab[c] for c in phonemes if c in self.vocab]
            pieces = [tokens[i: i + MAX_TOKENS] for i in range(0, len(tokens), MAX_TOKENS)] or [[]]
            audio = np.concatenate([self._run(t, self._style(p, len(t)), p["speed"] / ratio) for t in pieces if t]
                                   or [np.zeros(1, np.float32)])
            ms = (time.perf_counter() - t0) * 1000
        if p["robot"] > 0:
            audio = robot(audio, RATE, p["robot"])
        return audio.astype(np.float32), round(RATE * ratio), ms


def robot(x: np.ndarray, rate: int, amount: float) -> np.ndarray:
    """A short comb filter (a delayed copy a few ms behind) adds a metallic edge; 0..1."""
    d = int(rate * ROBOT_DELAY_S)
    y = x.copy()
    y[d:] += 0.6 * amount * x[:-d]
    return y / (1 + 0.6 * amount)

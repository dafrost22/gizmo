"""Gizmo's voice server: F5-TTS speaking as any personality from a short clip and its transcript, plus
Kokoro's built-in voices for personalities that don't clone (kokoro_voice.py). Either half is optional.

    .venv/bin/python voice_server.py                       (or the gizmo-voice service: install-services.sh)
    .venv/bin/python voice_server.py --robot http://reachy-mini.local:8042   (a laptop on any network)

Gizmo owns the personalities (on the robot) and teaches this server a voice the first time it needs it;
voices are cached on disk, keyed by a hash of their files:
  PUT  /voices/{id}  {"hash", "ref_text", "audio": base64 file, "catchphrases": {"Hello there!": base64}}
       -> {"ok", "seconds"}; 400 if the audio can't be read or is too long, 501 without F5-TTS.
  POST /tts {"text": "<one sentence>", "voice", "hash", "nfe"} -> int16 mono PCM at 16 kHz (Gizmo's
       speaker rate), headers X-Source (clone|clip), X-Seconds, X-Gen-Ms. 409 when this server doesn't
       have that voice (or that version of it): Gizmo uploads it and asks again.
  POST /tts {"text", "builtin": {"voice": "am_puck", "mix", "mix_amount", "speed", "pitch", "robot"}}
       -> the same PCM from one of Kokoro's built-in voices, no cloning (X-Source builtin). 400 for an
       unknown voice, 501 where Kokoro isn't installed.
  GET  /health -> {"ok", "nfe", "device", "voices": {id: hash}, "builtin": [Kokoro voice names], "cloning"}
  POST /quit -> stops the server; only from Gizmo's announced address or this machine, and only with --robot.
One request at a time on the GPU; Gizmo sends sentence by sentence and asks for the next while the
current one plays. With --robot, the server tells Gizmo's app its address every 10 s
(POST /api/clone/announce), so Gizmo finds this server on any network.

Speed: the reference clip is processed with every sentence, so a shorter clip is faster. On an RTX 3090
(16 steps) a sentence takes ~0.7 s; on an Apple M4 Pro (half precision) a 4.9 s clip took ~2.4 s for a
short sentence and an 8.3 s clip ~4.8 s.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import logging
import os
import re
import signal
import socket
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import numpy as np
import requests
import soundfile as sf
import torch
import torchaudio.functional as AF
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
try:  # cloned voices: only where F5-TTS is installed
    from f5_tts.api import F5TTS
except ImportError:
    F5TTS = None
try:  # built-in voices: only where the Kokoro files are
    from kokoro_voice import Kokoro
except ImportError:
    Kokoro = None

log = logging.getLogger("voice_server")
OUT_RATE = 16000
REF_RATE = 24000  # F5's own rate
MAX_REF_S = 15.0  # F5 cuts references at ~12 s, possibly mid-word, which spoils the transcript match
VOICE_ID = re.compile(r"^[a-z0-9_]{1,30}$")
ROBOT_IPS: set[str] = set()  # Gizmo's address, from announce(): the only remote caller of /quit
HERE = Path(__file__).resolve().parent
# Defaults beside this file; GIZMO_F5_MODELS / GIZMO_KOKORO_MODELS or the flags point elsewhere.
F5_MODELS = Path(os.environ.get("GIZMO_F5_MODELS", HERE / "models" / "f5"))
KOKORO_MODELS = Path(os.environ.get("GIZMO_KOKORO_MODELS", HERE / "models" / "kokoro"))


def resample(x: np.ndarray, sr: int, rate: int) -> np.ndarray:
    return x if sr == rate else AF.resample(torch.from_numpy(x.astype(np.float32)), sr, rate).numpy()


def trimmed(x: np.ndarray, sr: int, pad_s: float = 0.05) -> np.ndarray:
    voiced = np.where(np.abs(x) > 0.01)[0]
    pad = int(sr * pad_s)
    return x[max(0, voiced[0] - pad): voiced[-1] + pad] if voiced.size else x


def decode(data: bytes) -> tuple[np.ndarray, int]:
    """Any file soundfile reads (wav, flac, ogg, mp3) -> mono float32."""
    try:
        x, sr = sf.read(io.BytesIO(data), dtype="float32", always_2d=True)
    except Exception as e:
        raise HTTPException(400, f"couldn't read that audio ({e}); use a WAV, FLAC, OGG or MP3 file") from e
    return x.mean(axis=1), sr


def phrase_key(text: str) -> str:
    """"Hello there!" and "hello there" match the same catchphrase."""
    return re.sub(r"[^a-z0-9]+", "", text.lower())


class Voice:
    """One personality's voice: the reference clip as F5 wants it, its transcript, its catchphrase clips."""

    def __init__(self, folder: Path) -> None:
        meta = json.loads((folder / "voice.json").read_text())
        self.hash = meta["hash"]
        self.ref_path = str(folder / "ref.wav")
        self.ref_text = meta["ref_text"]
        self.seconds = meta.get("seconds", 0.0)
        self.catchphrases = {}
        for key, name in meta.get("catchphrases", {}).items():
            x, sr = sf.read(folder / name, dtype="float32")
            self.catchphrases[key] = resample(x, sr, OUT_RATE)


class Voices:
    """The cloned voices. Without F5-TTS (or its weights) there are none, and asking for one gets a 501."""

    def __init__(self, cache: Path, nfe: int, device: str, models: Path | None) -> None:
        self.cache = cache
        self.nfe = nfe
        self.device = device
        self.tts = None
        ckpt = models / "F5TTS_v1_Base/F5TTS_v1_Base/model_1250000.safetensors" if models else None
        if F5TTS is not None and ckpt is not None and ckpt.exists():
            self.tts = F5TTS(model="F5TTS_v1_Base", ckpt_file=str(ckpt),
                             vocab_file=str(models / "F5TTS_v1_Base/F5TTS_v1_Base/vocab.txt"),
                             vocoder_local_path=str(models / "vocos"), device=device)
            if device == "mps":  # F5 only uses half precision on NVIDIA; on Apple silicon it's ~20% faster too
                self.tts.ema_model = self.tts.ema_model.half()
        elif F5TTS is None:
            log.info("F5-TTS isn't installed: no cloned voices, built-in voices only")
        else:
            log.info(f"no F5-TTS weights at {models}: no cloned voices, built-in voices only")
        self.loaded: dict[str, Voice] = {}
        for folder in sorted(cache.glob("*/voice.json")):
            try:
                self.loaded[folder.parent.name] = Voice(folder.parent)
            except Exception as e:
                log.warning(f"skipping cached voice {folder.parent.name}: {e!r}")
        self.lock = threading.Lock()

    def learn(self, voice_id: str, payload: dict) -> Voice:
        if self.tts is None:
            raise HTTPException(501, "no cloned voices on this server (F5-TTS isn't installed here)")
        ref_text = str(payload.get("ref_text", "")).strip()
        if not ref_text:
            raise HTTPException(400, "the voice clip needs its transcript")
        x, sr = decode(base64.b64decode(payload.get("audio", "")))
        x = trimmed(x, sr, 0.12)
        seconds = len(x) / sr
        if seconds < 2.0 or seconds > MAX_REF_S:
            raise HTTPException(400, f"the voice clip is {seconds:.1f} s: use 3-12 s of clear speech "
                                     "(shorter is faster, especially without an NVIDIA GPU)")
        folder = self.cache / voice_id
        folder.mkdir(parents=True, exist_ok=True)
        sf.write(folder / "ref.wav", resample(x, sr, REF_RATE), REF_RATE)
        catch = {}
        for i, (text, data) in enumerate((payload.get("catchphrases") or {}).items()):
            y, ysr = decode(base64.b64decode(data))
            sf.write(folder / f"catch{i}.wav", trimmed(y, ysr), ysr)
            catch[phrase_key(text)] = f"catch{i}.wav"
        (folder / "voice.json").write_text(json.dumps(
            {"hash": payload.get("hash", ""), "ref_text": ref_text, "seconds": round(seconds, 2), "catchphrases": catch}))
        voice = Voice(folder)
        with self.lock:
            self.loaded[voice_id] = voice
        log.info(f"learned voice {voice_id}: {seconds:.1f} s clip, {len(catch)} catchphrase(s)")
        return voice

    def say(self, voice: Voice, text: str, nfe: int | None = None) -> tuple[np.ndarray, str, float]:
        clip = voice.catchphrases.get(phrase_key(text))
        if clip is not None:
            return clip, "clip", 0.0
        if self.tts is None:
            raise HTTPException(501, "no cloned voices on this server (F5-TTS isn't installed here)")
        with self.lock:
            t0 = time.perf_counter()
            wav, sr, _ = self.tts.infer(voice.ref_path, voice.ref_text, text, nfe_step=nfe or self.nfe,
                                        show_info=lambda *a: None, progress=None)
            ms = (time.perf_counter() - t0) * 1000
        return resample(np.asarray(wav), sr, OUT_RATE), "clone", ms


async def announce(robot: str, port: int) -> None:
    """Tell Gizmo's app our address every 10 s (it may have restarted, or the network changed)."""
    target = urlparse(robot)
    while True:
        try:
            ip = socket.gethostbyname(target.hostname)  # IPv4: the daemon and the app don't listen on IPv6
            ROBOT_IPS.clear()
            ROBOT_IPS.add(ip)
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect((ip, 80))  # no packet sent: just picks the interface that reaches Gizmo
                mine = s.getsockname()[0]
            await asyncio.to_thread(requests.post, f"{target.scheme}://{ip}:{target.port or 8042}/api/clone/announce",
                                    json={"url": f"http://{mine}:{port}"}, timeout=3)
        except (OSError, requests.RequestException) as e:
            log.debug(f"couldn't reach Gizmo yet: {e}")
        await asyncio.sleep(10)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", type=int, default=8770)
    # 16 steps: ~0.7 s a sentence on a 3090, with no audible difference from 32 (~1.1 s) in our listening tests.
    p.add_argument("--nfe", type=int, default=16, help="F5 denoising steps when the request doesn't say")
    p.add_argument("--device", default="cuda" if torch.cuda.is_available() else
                   "mps" if torch.backends.mps.is_available() else "cpu")
    p.add_argument("--f5-models", type=Path, default=F5_MODELS,
                   help="F5-TTS weights (F5TTS_v1_Base/F5TTS_v1_Base/model_1250000.safetensors, vocab.txt, vocos/); "
                        "without them there are no cloned voices")
    p.add_argument("--cache", type=Path, default=Path.home() / ".cache" / "gizmo-voices")
    p.add_argument("--kokoro", type=Path, default=KOKORO_MODELS,
                   help="Kokoro's ONNX files (kokoro-v1.0.onnx, voices-v1.0.bin, config.json), for the built-in voices")
    p.add_argument("--robot", help="Gizmo's app, e.g. http://reachy-mini.local:8042: announce ourselves to it")
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    a.cache.mkdir(parents=True, exist_ok=True)
    voices = Voices(a.cache, a.nfe, a.device, a.f5_models)
    if voices.tts is not None and voices.loaded:  # warm up the GPU kernels with a cached voice
        first = next(iter(voices.loaded.values()))
        for line in ("Hello there.", "This is a quick warm-up sentence."):
            voices.say(first, line)
    builtin = None
    if Kokoro is not None and (a.kokoro / "kokoro-v1.0.onnx").exists():
        try:
            builtin = Kokoro(a.kokoro)
            builtin.say("Warming up.", {"voice": builtin.voices()[0]})
        except Exception as e:  # the cloned voices still work
            log.warning(f"built-in voices unavailable: {e!r}")
            builtin = None
    cloning = f"F5-TTS on {a.device}, {a.nfe} steps, voices {sorted(voices.loaded)}" if voices.tts else "no cloning"
    log.info(f"ready: {cloning}, built-in voices: {len(builtin.voices()) if builtin else 'none'}")

    app = FastAPI()

    @app.on_event("startup")
    async def start_announcing() -> None:
        if a.robot:
            asyncio.create_task(announce(a.robot, a.port))

    @app.get("/health")
    def health() -> dict:
        return {"ok": True, "nfe": voices.nfe, "device": a.device,
                "voices": {k: v.hash for k, v in voices.loaded.items()} if voices.tts else {},
                "builtin": builtin.voices() if builtin else [], "cloning": voices.tts is not None}

    @app.post("/quit")
    def quit_server(request: Request) -> dict:
        """Gizmo's console "Shut down": a laptop's copy (started with --robot) exits. Only Gizmo (or this
        machine) may ask; without --robot there's no Gizmo to trust, so the box's service copy (stopped
        by gizmo-ctl.sh under systemd) always refuses."""
        if not a.robot or request.client.host not in ROBOT_IPS | {"127.0.0.1", "::1"}:
            raise HTTPException(403, "only Gizmo can stop this server")
        log.info(f"{request.client.host} asked us to quit: shutting down")
        threading.Timer(0.3, os.kill, (os.getpid(), signal.SIGTERM)).start()  # after this reply
        return {"ok": True}

    @app.put("/voices/{voice_id}")
    def learn(voice_id: str, payload: dict) -> dict:
        if not VOICE_ID.match(voice_id):
            raise HTTPException(400, "voice ids are lowercase letters, digits and _")
        voice = voices.learn(voice_id, payload)
        return {"ok": True, "seconds": voice.seconds}

    @app.post("/tts")
    def tts(payload: dict) -> Response:
        text = str(payload.get("text", "")).strip()
        if not text or len(text) > 600:
            raise HTTPException(400, "send one sentence of text")
        if isinstance(payload.get("builtin"), dict):
            return say_builtin(text, payload["builtin"])
        voice = voices.loaded.get(str(payload.get("voice", "")))
        if voice is None or (payload.get("hash") and payload["hash"] != voice.hash):
            raise HTTPException(409, "upload this voice first: PUT /voices/{id}")
        nfe = payload.get("nfe")
        nfe = max(8, min(64, int(nfe))) if isinstance(nfe, (int, float)) else None
        audio, source, ms = voices.say(voice, text, nfe)
        pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes()
        seconds = len(audio) / OUT_RATE
        log.info(f"{payload['voice']} {source} {seconds:.1f}s in {ms:.0f} ms: {text[:60]}")
        return Response(pcm, media_type="audio/L16", headers={
            "X-Source": source, "X-Seconds": f"{seconds:.2f}", "X-Gen-Ms": f"{ms:.0f}", "X-Rate": str(OUT_RATE)})

    def say_builtin(text: str, params: dict) -> Response:
        if builtin is None:
            raise HTTPException(501, "no built-in voices on this server (Kokoro isn't installed here)")
        try:
            audio, rate, ms = builtin.say(text, params)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        audio = resample(audio, rate, OUT_RATE)  # the rate carries the pitch shift
        pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes()
        seconds = len(audio) / OUT_RATE
        log.info(f"builtin {params.get('voice')} {seconds:.1f}s in {ms:.0f} ms: {text[:60]}")
        return Response(pcm, media_type="audio/L16", headers={
            "X-Source": "builtin", "X-Seconds": f"{seconds:.2f}", "X-Gen-Ms": f"{ms:.0f}", "X-Rate": str(OUT_RATE)})

    uvicorn.run(app, host="0.0.0.0", port=a.port, log_level="warning")


if __name__ == "__main__":
    main()

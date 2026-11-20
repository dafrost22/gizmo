"""Gizmo's local ears: voice activity detection plus Whisper on a GPU box, instead of Nova Sonic's listening.

Gizmo streams its mic here; we find where someone starts and stops talking and send back what they
said. The box is optional: when this is unreachable Gizmo falls back to Nova Sonic on Bedrock. Nova
waits ~1.5 s of silence to end a turn; we default to 0.7 s (the robot sends its own setting).

    .venv/bin/python ears_server.py            (or the gizmo-ears service: install-services.sh)

WebSocket /listen. In: binary int16 mono PCM at 16 kHz (any chunk size); text {"type": "config",
"end_silence_ms": 700} or {"type": "reset"} (drop a turn in progress, e.g. while Gizmo talks).
Also {"type": "playback", "on": true|false} while Gizmo talks (barge-in needs stronger evidence then).
Out: {"type": "speech_start"} as soon as someone talks (Gizmo stops talking: barge-in), then
{"type": "turn", "text", "speech_ms", "asr_ms", "end_silence_ms"} when they've finished.
GET /health.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import time

import numpy as np
import torch
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from silero_vad import load_silero_vad
from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor

log = logging.getLogger("ears")
RATE = 16000
FRAME = 512  # samples per VAD frame (32 ms), what Silero expects at 16 kHz
START_FRAMES = 3  # ~100 ms of speech before we call it speech
# While Gizmo is talking its own voice leaks into the mic despite the chip's echo cancelling, so a
# barge-in needs more: ~250 ms of confident speech.
START_FRAMES_PLAYBACK, START_PROB_PLAYBACK = 8, 0.7
PREROLL_S = 0.3  # keep the start of the first word
MIN_SPEECH_S = 0.25
MAX_TURN_S = 30.0
# Whisper invents these on breaths and room noise; drop them when the clip is short.
HALLUCINATIONS = re.compile(r"^\W*(thank you|thanks for watching|you|bye|okay|so|\.+)\W*$", re.IGNORECASE)


class Ears:
    def __init__(self, model: str, device: str) -> None:
        self.vad = load_silero_vad()
        self.device = device
        self.dtype = torch.float16 if device == "cuda" else torch.float32
        # Model and processor directly: transformers 5's ASR pipeline imports torchcodec, which needs the
        # FFmpeg libraries installed too.
        self.proc = AutoProcessor.from_pretrained(model)
        self.model = AutoModelForSpeechSeq2Seq.from_pretrained(model, dtype=self.dtype).to(device).eval()
        self.lock = asyncio.Lock()  # one transcription at a time on the GPU

    def transcribe(self, audio: np.ndarray) -> str:
        feats = self.proc(audio.astype(np.float32), sampling_rate=RATE, return_tensors="pt").input_features
        with torch.inference_mode():
            ids = self.model.generate(feats.to(self.device, self.dtype), language="en", task="transcribe",
                                      max_new_tokens=200)
        return self.proc.batch_decode(ids, skip_special_tokens=True)[0].strip()


class Turn:
    """One client's stream: frames in, speech_start / turn decisions out."""

    def __init__(self, ears: Ears, end_silence_ms: int) -> None:
        self.ears = ears
        self.end_frames = max(1, int(end_silence_ms / 1000 * RATE / FRAME))
        self.pending = np.zeros(0, np.float32)
        self.preroll: list[np.ndarray] = []
        self.playback = False  # Gizmo is talking
        self.reset()

    def reset(self) -> None:
        self.speaking = False
        self.voiced_run = 0
        self.silent_run = 0
        self.last_voiced = 0  # index in frames of the last frame that sounded like speech
        self.frames: list[np.ndarray] = []
        self.ears.vad.reset_states()

    def feed(self, pcm: np.ndarray) -> list[dict]:
        """Returns events: {"type": "speech_start"} and/or {"type": "_finish", "audio": ...}."""
        events = []
        self.pending = np.concatenate([self.pending, pcm])
        while self.pending.size >= FRAME:
            frame, self.pending = self.pending[:FRAME], self.pending[FRAME:]
            prob = float(self.ears.vad(torch.from_numpy(frame), RATE))
            if not self.speaking:
                self.preroll = (self.preroll + [frame])[-int(PREROLL_S * RATE / FRAME):]
                need, threshold = (START_FRAMES_PLAYBACK, START_PROB_PLAYBACK) if self.playback else (START_FRAMES, 0.5)
                self.voiced_run = self.voiced_run + 1 if prob >= threshold else 0
                if self.voiced_run >= need:
                    self.speaking, self.silent_run = True, 0
                    self.frames = list(self.preroll)
                    events.append({"type": "speech_start"})
                continue
            self.frames.append(frame)
            if prob >= 0.5:
                self.last_voiced = len(self.frames)
            self.silent_run = self.silent_run + 1 if prob < 0.35 else 0
            too_long = len(self.frames) * FRAME / RATE > MAX_TURN_S
            if self.silent_run >= self.end_frames or too_long:
                audio = np.concatenate(self.frames)
                # How long the person had really been quiet: Silero can hover between 0.35 and 0.5 on
                # room noise (Gizmo's mic gain is high), which delays the end of turn past the setting.
                quiet_ms = (len(self.frames) - self.last_voiced) * FRAME / RATE * 1000
                self.reset()
                events.append({"type": "_finish", "audio": audio, "quiet_ms": quiet_ms})
        return events


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", type=int, default=8771)
    p.add_argument("--model", default="openai/whisper-large-v3-turbo")
    p.add_argument("--end-silence-ms", type=int, default=700)
    p.add_argument("--device", default="cuda" if torch.cuda.is_available() else "cpu",
                   help="cuda (fast) or cpu (works, but a turn takes seconds)")
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ears = Ears(a.model, a.device)
    t0 = time.perf_counter()
    ears.transcribe(np.zeros(RATE, np.float32))  # warm up the GPU
    where = torch.cuda.get_device_name(0) if a.device == "cuda" else a.device
    log.info(f"ready: {a.model} on {where} (warm-up {time.perf_counter() - t0:.1f}s), "
             f"end of turn after {a.end_silence_ms} ms of silence")
    app = FastAPI()

    @app.get("/health")
    def health() -> dict:
        return {"ok": True, "model": a.model, "end_silence_ms": a.end_silence_ms}

    @app.websocket("/listen")
    async def listen(ws: WebSocket) -> None:
        await ws.accept()
        end_ms = a.end_silence_ms
        turn = Turn(ears, end_ms)
        log.info(f"client connected from {ws.client.host if ws.client else '?'}")
        try:
            while True:
                msg = await ws.receive()
                if msg.get("type") == "websocket.disconnect":
                    break
                if msg.get("text"):
                    cmd = json.loads(msg["text"])
                    if cmd.get("type") == "config" and isinstance(cmd.get("end_silence_ms"), (int, float)):
                        end_ms = int(max(200, min(3000, cmd["end_silence_ms"])))
                        turn.end_frames = max(1, int(end_ms / 1000 * RATE / FRAME))
                    elif cmd.get("type") == "reset":
                        turn.reset()
                    elif cmd.get("type") == "playback":
                        turn.playback = bool(cmd.get("on"))
                    continue
                pcm = np.frombuffer(msg["bytes"], np.int16).astype(np.float32) / 32768
                for ev in turn.feed(pcm):
                    if ev["type"] == "speech_start":
                        await ws.send_text(json.dumps(ev))
                        continue
                    audio = ev["audio"]
                    speech_s = audio.size / RATE - end_ms / 1000
                    if speech_s < MIN_SPEECH_S:
                        continue
                    t1 = time.perf_counter()
                    async with ears.lock:
                        text = await asyncio.to_thread(ears.transcribe, audio)
                    asr_ms = (time.perf_counter() - t1) * 1000
                    if not text or (speech_s < 1.5 and HALLUCINATIONS.match(text)):
                        log.info(f"dropped {speech_s:.1f}s: {text!r}")
                        continue
                    log.info(f"turn {speech_s:.1f}s, quiet {ev['quiet_ms']:.0f} ms before the end, asr {asr_ms:.0f} ms: {text}")
                    await ws.send_text(json.dumps({"type": "turn", "text": text, "speech_ms": round(speech_s * 1000),
                                                   "asr_ms": round(asr_ms), "end_silence_ms": end_ms,
                                                   "quiet_ms": round(ev["quiet_ms"])}))
        except WebSocketDisconnect:
            pass
        log.info("client disconnected")

    uvicorn.run(app, host="0.0.0.0", port=a.port, log_level="warning")


if __name__ == "__main__":
    main()

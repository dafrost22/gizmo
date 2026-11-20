"""The local pipeline: the home box's ears, the Strands brain, the box's voice server. No Nova.

The home box works like a private cloud on home internet, with the cloud as its backup. Gizmo streams
its mic to addons/home-box/ears_server.py; each finished turn goes to the Strands brain (the box's local model in
LM Studio, Haiku as backup) and the reply plays in the personality's voice from addons/voice/voice_server.py. If
the ears or the voice stop answering, run() returns and the app falls back to the Nova pipeline.

Barge-in (on by default): the mic keeps streaming while Gizmo talks, the ears are told so (they then
need ~250 ms of confident speech, so Gizmo's own leaked voice doesn't count), and when they report
someone starting to talk Gizmo stops mid-sentence and listens. With barge-in off it's half-duplex:
silence goes to the ears while Gizmo speaks.

In wake-word mode (listening.py) a turn without Gizmo's name is dropped unless it's a follow-up, and
starting to talk doesn't stop Gizmo: only a finished turn with its name does.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time

import numpy as np
import requests
import websockets

from gizmo.config import box_urls

logger = logging.getLogger("gizmo.local")

# The box's addresses in config.py's order.
EARS_URLS = box_urls(8771, "ws", "/listen")
# A turn that's only a reaction is not a question: "Oh" and "Yeah." right after a punchline each got a
# gesture-only reply (2026-10-26), and could as well be Whisper hearing Gizmo's own voice.
REACTION = re.compile(r"^\W*(oh+|ah+|uh+|um+|hmm+|mm+|mhm|huh|wow|yeah|yep|okay|ok)\W*$", re.IGNORECASE)
SILENCE_20MS = bytes(640)  # 16 kHz int16 mono


def ears_url() -> str | None:
    """The first ears service whose /health answers (1.5 s each), or None."""
    for url in EARS_URLS:
        health = url.replace("ws://", "http://").replace("/listen", "/health")
        try:
            if requests.get(health, timeout=1.5).ok:
                return url
        except requests.RequestException:
            continue
    return None


class LocalSession:
    """Plays the role BidiAgent plays in the Nova pipeline: run(), stop(), and send() for typed lines."""

    def __init__(self, url: str, mic, speaker, log, reply, end_silence_ms: int = 600, on_heard=None,
                 barge_in: bool = True, listening=None) -> None:
        self.url = url
        self.mic = mic
        self.speaker = speaker
        self.log = log
        self.reply = reply  # async (text) -> bool: think and speak; False if the box voice failed
        self.turns: asyncio.Queue[str] = asyncio.Queue()
        self.failed: str | None = None  # why the session ended, if the box went away
        self.end_silence_ms = end_silence_ms  # how long a pause ends the person's turn
        self.on_heard = on_heard  # () -> None when a turn ends (an antenna flick while the brain works)
        self.barge_in = barge_in  # let people talk over Gizmo
        self.listening = listening  # free talk or wake word (listening.Listening); None: free talk
        self._ws = None
        self._stop = asyncio.Event()

    async def send(self, text: str) -> None:
        """A typed line from the console, handled like a spoken turn."""
        self.log.last_user_at = time.monotonic()
        await self.turns.put(text)

    async def stop(self) -> None:
        self._stop.set()

    async def set_end_silence(self, ms: int) -> None:
        self.end_silence_ms = ms
        if self._ws is not None:
            await self._ws.send(json.dumps({"type": "config", "end_silence_ms": ms}))

    async def run(self) -> None:
        await self.mic.start(None)
        await self.speaker.start(None)
        try:
            async with websockets.connect(self.url, open_timeout=3, max_size=2**20) as ws:
                logger.info(f"Local ears connected: {self.url}")
                self._ws = ws
                await ws.send(json.dumps({"type": "config", "end_silence_ms": self.end_silence_ms}))
                tasks = [asyncio.create_task(self._send_mic(ws)), asyncio.create_task(self._receive(ws)),
                         asyncio.create_task(self._handle_turns()), asyncio.create_task(self._stop.wait())]
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for t in pending:
                    t.cancel()
                for t in done:
                    if t.exception() and not self.failed:
                        self.failed = f"{type(t.exception()).__name__}: {t.exception()}"
        except (OSError, websockets.WebSocketException, asyncio.TimeoutError) as e:
            self.failed = f"ears unreachable: {e!r}"
        finally:
            await self.mic.stop()
            await self.speaker.stop()
        if self.failed:
            logger.warning(f"Local session ended: {self.failed}")

    async def _send_mic(self, ws) -> None:
        playback = False
        while True:
            pcm = (await self.mic()).source["bytes"]  # a Strands AudioDelta, as Nova would get it
            speaking = self.speaker.speaking
            if speaking != playback:  # tell the ears, so they're stricter about starting a turn
                playback = speaking
                await ws.send(json.dumps({"type": "playback", "on": speaking}))
            # Muted, or half-duplex without barge-in: the ears get silence.
            await ws.send(bytes(len(pcm)) if self.mic.muted or (speaking and not self.barge_in) else pcm)

    async def _receive(self, ws) -> None:
        async for msg in ws:
            ev = json.loads(msg)
            wake = self.listening is not None and self.listening.mode == "wake"
            if ev.get("type") == "speech_start":
                if self.barge_in and self.speaker.speaking and not wake:
                    self.speaker.clear()  # stop mid-sentence; the rest of this reply is dropped
                    self.log.add("barge_in", text="you talked over Gizmo: it stopped to listen")
                    logger.info("Barge-in: stopped talking")
                continue
            if ev.get("type") != "turn" or (self.speaker.speaking and not self.barge_in):
                continue
            text = ev["text"]
            answer = self.listening is not None and self.listening.asked and not self.speaker.speaking
            if REACTION.match(text) and not answer:  # not a turn: no reply, and Gizmo keeps talking
                self.log.add("ignored", text=f'just a reaction: "{text.strip()}"')
                logger.info(f"Ignored a reaction: {text!r}")
                continue
            if self.listening is not None and not self.listening.addressed(text):
                kept = not self.log.private(text)  # someone else's conversation isn't shown or logged
                said = f'"{text.strip()}"' if kept else f"{len(text.split())} words"
                self.log.add("ignored", text=f'not for Gizmo (no "{self.listening.names[0]}"): {said}')
                logger.info(f"Not addressed: {said}")
                continue
            if self.speaker.speaking:  # a turn finished while Gizmo still talks: it gives way
                self.speaker.clear()
            if self.on_heard:
                self.on_heard()
            # When the person actually stopped talking: before our silence wait and the transcription.
            self.log.last_user_at = time.monotonic() - (ev["end_silence_ms"] + ev["asr_ms"]) / 1000
            self.log.add("user", text=text)
            logger.info(f"USER: {text}")
            await self.turns.put(text)
        raise ConnectionError("ears closed the connection")

    async def _handle_turns(self) -> None:
        while True:
            text = await self.turns.get()
            if not await self.reply(text):
                self.failed = "box voice unavailable"
                return

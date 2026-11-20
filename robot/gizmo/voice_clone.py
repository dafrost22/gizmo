"""Client for the personalities' own voices (addons/voice/voice_server.py, on the home box or a laptop).

The reply text goes to the voice server sentence by sentence, in the active personality's voice; Nova
stays silent for that turn (it still listens and detects turns). We fetch sentence n+1 while n plays.

Servers (the voice add-in, addons/voice): config.py's voice_servers (else the home box's), and any
laptop running voice_server.py --robot, which announces its address every 10 s (POST
/api/clone/announce). "auto" prefers the first configured address, then a laptop, then the other
configured addresses. A server learns a personality's cloned voice the first time
it's asked for it (409, then PUT /voices/{id}). If none answers, the caller lets Nova speak instead.

A personality with a built-in voice (Kokoro, no cloning) sends its tuning with each sentence instead;
only servers that have the built-in voices count for it.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import requests

from gizmo.personalities import VoiceRef
from gizmo.config import CONFIG, box_urls

logger = logging.getLogger("gizmo.voice_clone")

# The voice add-in's servers from config.py (voice_servers, else the home box's, first address first): the
# starting list, which the console can change (settings["voice_servers"]). Laptops running voice_server.py
# --robot announce themselves on top.
VOICE_URLS = list(CONFIG["voice_servers"]) or box_urls(8770)
ANNOUNCE_TTL_S = 35  # a Mac that stopped announcing (every 10 s) is dropped
PREFER = ("auto", "box", "mac")
SENTENCE = re.compile(r"[^.!?]+[.!?]+[\"')\]]*|[^.!?]+$")


def sentences(text: str) -> list[str]:
    """'Stand by. Here it comes!' -> ['Stand by.', 'Here it comes!']"""
    return [s.strip() for s in SENTENCE.findall(text) if s.strip()]


class VoiceError(Exception):
    """A server refused a voice (bad clip, too long): the console shows the reason."""


class CloneVoiceClient:
    def __init__(self) -> None:
        self.voice: VoiceRef | None = None  # the active personality's voice; set by the app
        self.prefer = "auto"
        self.servers: list[str] = list(VOICE_URLS)  # where the voice add-in's servers are (the console sets it)
        self.caps: dict[str, dict] = {}  # url -> {"builtin", "cloning"}, from its last /health
        self.macs: dict[str, float] = {}  # announced Mac servers -> monotonic time last heard
        self.url: str | None = None  # the server in use
        self.checked_at = 0.0
        self._probing = False
        self._choice = 0  # bumped by set_prefer: a check started before then is ignored
        self.http = requests.Session()
        self.last_gen_ms: float | None = None
        self.nfe = 16  # F5 steps per sentence (16 fast, 32 best); set from the console, sent with each request

    # --- which server ----------------------------------------------------------------------

    def announce(self, url: str) -> None:
        if url not in self.macs:
            logger.info(f"Mac voice server at {url}")
            self.checked_at = 0.0  # look again now
        self.macs[url] = time.monotonic()

    def set_servers(self, urls: list[str]) -> None:
        """The voice servers to use, from the console: look for one again now."""
        self.servers = list(urls)
        self._choice += 1
        self.url, self.checked_at = None, 0.0

    def info(self, url: str) -> dict:
        """What one server can do, for the console: {"ok", "cloning", "builtin", "voices"} or {"ok": False, "error"}."""
        try:
            h = self.http.get(f"{url.rstrip('/')}/health", timeout=3).json()
        except requests.Timeout:
            return {"ok": False, "error": "no answer in 3 s"}
        except requests.ConnectionError:
            return {"ok": False, "error": "can't connect (is voice_server.py running there?)"}
        except (requests.RequestException, ValueError) as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"[:120]}
        return {"ok": bool(h.get("ok")), "cloning": h.get("cloning", True), "builtin": len(h.get("builtin") or []),
                "voices": sorted(h.get("voices") or {})}

    def set_prefer(self, prefer: str) -> None:
        """Where the voice comes from (auto, box, mac). A check still running for the old choice would
        otherwise land after this and win (picking the laptop could leave Gizmo on the box)."""
        self.prefer = prefer
        self._choice += 1
        self.url, self.checked_at = None, 0.0

    def live_macs(self) -> list[str]:
        now = time.monotonic()
        return [u for u, seen in self.macs.items() if now - seen < ANNOUNCE_TTL_S]

    def candidates(self) -> list[str]:
        macs = self.live_macs()
        return {"box": self.servers, "mac": macs}.get(self.prefer, [*self.servers[:1], *macs, *self.servers[1:]])

    def set_voice(self, voice: VoiceRef | None) -> None:
        """The active personality's voice. Switching between cloned and built-in voices looks for a server
        again: the Mac has no built-in voices."""
        builtin = bool(voice and voice.builtin)
        if builtin != bool(self.voice and self.voice.builtin):
            self.url, self.checked_at = None, 0.0
        self.voice = voice

    def where(self, url: str | None = None) -> str | None:
        url = url or self.url
        return None if url is None else "box" if url in self.servers else "mac"

    def _suits(self, url: str, voice: VoiceRef | None) -> bool:
        """Can this server speak this voice? A server without F5 weights can't clone, and one without the
        Kokoro files has no built-in voices (as its last /health said)."""
        caps = self.caps.get(url, {})
        return bool(caps.get("builtin")) if voice is not None and voice.builtin else caps.get("cloning", True)

    def _healthy(self, url: str, voice: VoiceRef | None = None) -> bool:
        try:
            r = self.http.get(f"{url}/health", timeout=1.5)
            h = r.json()
        except (requests.RequestException, ValueError):
            return False
        self.caps[url] = {"builtin": bool(h.get("builtin")), "cloning": h.get("cloning", True)}
        return r.ok and self._suits(url, voice)

    def _probe(self, voice: VoiceRef | None = None) -> str | None:
        """All candidates at once, best one first, and done as soon as the best healthy one that can speak
        this voice (the active personality's by default) answers: an address that only works away from home
        times out at home, which mustn't hold up every check."""
        voice = voice or self.voice
        urls = self.candidates()
        if not urls:
            return None
        pool = ThreadPoolExecutor(len(urls))
        try:
            futures = [pool.submit(self._healthy, u, voice) for u in urls]
            return next((u for u, f in zip(urls, futures) if f.result()), None)
        finally:
            pool.shutdown(wait=False)

    def url_for(self, voice: VoiceRef | None) -> str | None:
        """A server for this voice: the one in use if it can speak it, else the best that can (another
        personality's cloned voice while a built-in one is in, for the console's preview and teaching)."""
        if voice is None or voice is self.voice:
            return self.ensure_url()
        if self.url is not None and self._suits(self.url, voice):
            return self.url
        return self._probe(voice)

    def _settle(self, choice: int, url: str | None) -> None:
        if choice != self._choice:  # the choice changed while we were checking: that check is stale
            return
        if url != self.url:
            logger.info(f"Cloned voice now from {url or 'nowhere'}")
        self.url, self.checked_at = url, time.monotonic()

    def ensure_url(self) -> str | None:
        """For the app's threads: the server in use, probing now if none is known yet."""
        if self.url is None:
            choice = self._choice
            self._settle(choice, self._probe())
        return self.url

    async def _refresh(self) -> None:
        self._probing = True
        try:
            choice = self._choice
            self._settle(choice, await asyncio.to_thread(self._probe))
        finally:
            self._probing = False

    async def available(self) -> bool:
        """Re-checked every 10 s in the background; only waits when no server is known yet."""
        if time.monotonic() - self.checked_at > 10 and not self._probing:
            if self.url is None:
                await self._refresh()
            else:
                asyncio.create_task(self._refresh())
        return self.url is not None

    # --- speaking --------------------------------------------------------------------------

    def _learn(self, url: str, voice: VoiceRef) -> float:
        r = self.http.put(f"{url}/voices/{voice.id}", json=voice.payload(), timeout=60)
        if r.status_code == 400:
            raise VoiceError(r.json().get("detail", "the voice server refused this clip"))
        r.raise_for_status()
        logger.info(f"Taught {self.where(url)} the {voice.id} voice")
        return float(r.json().get("seconds", 0))

    def ensure_voice(self, voice: VoiceRef | None = None) -> float | None:
        """Make sure a server that can speak this voice has it (uploading it if not); its clip length in seconds."""
        voice = voice or self.voice
        url = self.url_for(voice) if voice is not None and not voice.builtin else None
        if voice is None or url is None or voice.builtin:
            return None
        have = self.http.get(f"{url}/health", timeout=3).json().get("voices", {})
        return None if have.get(voice.id) == voice.hash else self._learn(url, voice)

    def synth(self, text: str, voice: VoiceRef | None = None) -> np.ndarray:
        """One sentence in a voice (the active personality's by default), as float32 at 16 kHz."""
        voice = voice or self.voice
        url = self.url if voice is self.voice else self.url_for(voice)
        if url is None:
            raise requests.ConnectionError("no voice server")
        if voice is not None and voice.builtin:
            body = {"text": text, "builtin": voice.builtin}
        else:
            body = {"text": text, "nfe": self.nfe, **({"voice": voice.id, "hash": voice.hash} if voice else {})}
        r = self.http.post(f"{url}/tts", json=body, timeout=30)
        if r.status_code in (400, 501) and voice is not None and voice.builtin:
            raise VoiceError(r.json().get("detail", "the voice server refused this built-in voice"))
        if r.status_code == 409 and voice is not None and not voice.builtin:  # this server hasn't learned the voice yet
            self._learn(url, voice)
            r = self.http.post(f"{url}/tts", json=body, timeout=30)
        r.raise_for_status()
        self.last_gen_ms = float(r.headers.get("X-Gen-Ms", 0))
        return np.frombuffer(r.content, np.int16).astype(np.float32) / 32768

    def _fetch(self, text: str) -> np.ndarray:
        try:
            return self.synth(text)
        except VoiceError as e:  # a bad clip: let Nova speak rather than go quiet
            raise requests.RequestException(str(e)) from e

    async def first(self, text: str) -> tuple[np.ndarray | None, list[str]]:
        """The first sentence's audio (None if the server failed: let Nova speak) and the sentences left."""
        parts = sentences(text)
        if not parts:
            return None, []
        try:
            return await asyncio.to_thread(self._fetch, parts[0]), parts[1:]
        except requests.RequestException as e:
            logger.warning(f"Clone voice unavailable ({e}); Nova speaks this reply")
            self.checked_at = 0.0
            return None, []

    async def rest(self, parts: list[str], play, cancelled) -> None:
        """Fetch the remaining sentences in order, each handed to play(pcm, sentence) as it arrives.

        Fetching n+1 overlaps with n playing. Stops when cancelled() turns true (barge-in or a new
        turn). If the server fails mid-reply the rest is dropped: Nova can't take over mid-turn.
        """
        for sentence in parts:
            if cancelled():
                return
            try:
                pcm = await asyncio.to_thread(self._fetch, sentence)
            except requests.RequestException as e:
                logger.warning(f"Clone voice failed mid-reply on {sentence!r}: {e}")
                self.checked_at = 0.0
                return
            if cancelled():
                return
            play(pcm, sentence)


class CloneSpeech:
    """One reply spoken sentence by sentence while the brain is still writing it (the local pipeline).

    say() queues a sentence; a worker fetches each from the server in order and plays it, so sentence
    n+1 is being made while n plays. cancelled() (barge-in, a new turn) stops it.
    """

    def __init__(self, client: CloneVoiceClient, play, cancelled) -> None:
        self.client = client
        self.play = play
        self.cancelled = cancelled
        self.failed = False
        self.first_at: float | None = None  # monotonic time the first audio was queued
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._task = asyncio.create_task(self._run())

    def say(self, sentence: str) -> None:
        self._queue.put_nowait(sentence)

    async def close(self) -> None:
        """Wait until every queued sentence has been fetched and queued for playback."""
        self._queue.put_nowait(None)
        await self._task

    async def _run(self) -> None:
        while (sentence := await self._queue.get()) is not None:
            if self.cancelled() or self.failed:
                continue
            try:
                pcm = await asyncio.to_thread(self.client._fetch, sentence)
            except requests.RequestException as e:
                logger.warning(f"Clone voice failed on {sentence!r}: {e}")
                self.client.checked_at = 0.0
                self.failed = True
                continue
            if not self.cancelled():
                if self.first_at is None:
                    self.first_at = time.monotonic()
                self.play(pcm, sentence)

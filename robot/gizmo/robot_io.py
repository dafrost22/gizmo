"""Strands bidi I/O on Gizmo's own mic and speaker (through the Reachy Mini SDK).

- RobotMic: reads the mic array in a thread, sends 16 kHz PCM to Nova Sonic. When muted
  it sends silence instead, because Nova only takes its turn while audio keeps flowing.
- RobotSpeaker: plays Nova's audio, or the personality's own voice from a voice server, in 20 ms
  chunks kept ~150 ms ahead of real time, so a barge-in (someone talking over Gizmo) cuts it off
  almost immediately. The daemon's wobbler moves the head with whatever we push.
- Captions: the words being said and where they fall in the reply's audio, so the console can light each word
  up as Gizmo says it.
- ConversationLog: keeps transcripts, tool calls and timings for the web console and logs.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import threading
import time
from collections import deque

import numpy as np
from strands.bidi.types.events import (
    BidiAudioDeltaEvent,
    BidiBargeInEvent,
    BidiResponseStopEvent,
    BidiToolUseBlocksEvent,
    BidiTranscriptBlockEvent,
    BidiTranscriptDeltaEvent,
    BidiTranscriptStartEvent,
)
from strands.bidi.types.media import AudioDelta

from gizmo.voice_clone import CloneVoiceClient

logger = logging.getLogger("gizmo.robot_io")

NOVA_INPUT_RATE = 16000
VOICE_DBFS = -40.0  # mic louder than this counts as someone talking (idle room is about -50 to -60)
CHUNK_S = 0.02
LEAD_S = 0.15
# On the cloud pipeline: the personality's own voice from a voice server (Nova when none answers), or Nova's.


def resample(x: np.ndarray, src: int, dst: int) -> np.ndarray:
    if src == dst or x.size == 0:
        return x
    n = int(round(x.size * dst / src))
    return np.interp(np.linspace(0, x.size - 1, n), np.arange(x.size), x).astype(np.float32)


class RobotMic:
    def __init__(self, mini) -> None:
        self.mini = mini
        self.muted = False
        self.level_dbfs = -120.0
        self.last_voice_at = 0.0  # monotonic time the mic last heard a voice (for latency, both pipelines)
        # Monotonic time the agent last asked for audio. It asks many times a second, and stops when a send to
        # Nova Sonic never completes: the sign of a connection that has gone dead underneath it.
        self.pulled_at = 0.0
        self._queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=200)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        """True once the agent has started us (the agent then accepts typed lines too)."""
        return self._thread is not None and self._thread.is_alive() and not self._stop.is_set()

    async def start(self, agent) -> None:
        # A fresh stop flag per session: BidiAgent stops us when a session ends (brain or voice
        # switch), and the old flag would end the next session's reader at once, leaving Nova deaf.
        stop = self._stop = threading.Event()
        while not self._queue.empty():  # drop audio left over from the last session
            self._queue.get_nowait()
        loop = asyncio.get_running_loop()
        rate = self.mini.media.get_input_audio_samplerate()

        def read() -> None:
            while not stop.is_set():
                frame = self.mini.media.get_audio_sample()  # blocks up to ~20 ms
                if frame is None:
                    continue
                mono = frame.mean(axis=1) if frame.ndim == 2 else frame
                mono = resample(mono.astype(np.float32), rate, NOVA_INPUT_RATE)
                rms = float(np.sqrt(np.mean(mono**2))) if mono.size else 0.0
                self.level_dbfs = 20 * np.log10(max(rms, 1e-6))
                if self.level_dbfs > VOICE_DBFS:
                    self.last_voice_at = time.monotonic()
                if self.muted:
                    mono = np.zeros_like(mono)
                pcm = (np.clip(mono, -1, 1) * 32767).astype(np.int16).tobytes()
                loop.call_soon_threadsafe(self._put, pcm)

        self.pulled_at = time.monotonic()
        self._thread = threading.Thread(target=read, name="robot-mic", daemon=True)
        self._thread.start()
        logger.info(f"Mic streaming at {rate} Hz -> {NOVA_INPUT_RATE} Hz")

    def _put(self, pcm: bytes) -> None:
        if self._queue.full():  # never let a slow connection build up seconds of stale audio
            self._queue.get_nowait()
        self._queue.put_nowait(pcm)

    async def stop(self) -> None:
        self._stop.set()

    async def __call__(self) -> AudioDelta:
        self.pulled_at = time.monotonic()
        return AudioDelta(format="pcm", source={"bytes": await self._queue.get()})


# Voice boost: Nova speaks at about -22 dBFS average with -5 dBFS peaks, so with the hardware
# already at 100% the only way up is to squash the peaks and raise the rest. A 1 ms lookahead
# limiter does that: gain drops just before a peak and recovers over ~60 ms. After us the
# daemon's speaker EQ lifts 1-8 kHz by up to ~6 dB and hard-clamps at 0.9, so the ceiling
# leaves some room for it.
LIMIT_CEILING = 0.7  # -3 dBFS
RELEASE_DB_PER_MS = 0.1


def dbfs(x: float) -> float:
    return round(20 * np.log10(max(x, 1e-6)), 1)


class Captions:
    """The words Gizmo is saying and where they fall in the reply's audio, so the console can light each word up
    as it's spoken.

    A reply is a run of segments: some words, and the stretch of the reply's audio that says them. Nova Sonic
    sends each sentence's words just before that sentence's audio, and a voice server is asked for one sentence at
    a time, so a segment's audio is whatever is queued between its words and the next ones. That much is exact.
    Where each word falls inside its sentence is the console's estimate.
    """

    def __init__(self) -> None:
        self.reply = 0  # counts replies, so the console can tell a new one from more of the same one
        self.started = ""  # when this reply began, as the log writes times: it answers a turn no later than that
        self.segments: list[dict] = []  # {"text", "start", "end"}: seconds into the reply's audio
        self.queued_s = 0.0  # the reply's audio handed to the speaker so far
        self.played_s = 0.0  # and how much of that the speaker has sent on to the robot

    def new(self) -> None:
        """A new reply, or the last one was cut off: start again from nothing."""
        self.reply += 1
        self.started = time.strftime("%H:%M:%S")
        self.segments, self.queued_s, self.played_s = [], 0.0, 0.0

    def words(self, text: str) -> None:
        """The words that the audio queued from now on says."""
        text = " ".join(text.split())
        if not text:
            return
        last = self.segments[-1] if self.segments else None
        if last is not None and last["end"] <= last["start"]:  # no audio came for the last words: one stretch
            last["text"] = f"{last['text']} {text}"
        else:
            self.segments.append({"text": text, "start": self.queued_s, "end": self.queued_s})

    def audio(self, seconds: float) -> None:
        self.queued_s += seconds
        if self.segments:
            self.segments[-1]["end"] = self.queued_s

    def state(self, ahead_s: float = 0.0) -> dict:
        """For the console. ahead_s: how much of what was sent to the robot hasn't sounded yet."""
        at = max(0.0, min(self.queued_s, self.played_s - max(0.0, ahead_s)))
        return {"reply": self.reply, "started": self.started, "at": round(at, 2), "audio": round(self.queued_s, 2),
                "segments": [{"text": x["text"], "start": round(x["start"], 2), "end": round(x["end"], 2)}
                             for x in list(self.segments)]}


class RobotSpeaker:
    def __init__(self, mini, boost_db: float = 0.0) -> None:
        self.mini = mini
        self.rate = mini.media.get_output_audio_samplerate()
        self.own_voice = False  # the personality in use speaks with a voice server's voice, not Nova's; set by the app
        self._chunks: deque[np.ndarray] = deque()
        self._loop: asyncio.AbstractEventLoop | None = None
        self.clone = CloneVoiceClient()
        self.mute_nova = False  # this turn is spoken by the personality's own voice: drop Nova's audio
        # (transcript) -> bool: may what Nova heard cut off the personality's voice? Wake-word mode wants the name.
        self.interrupts = None
        self.generation = 0  # bumped by clear(): cancels speech still being fetched
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._probe_task: asyncio.Task | None = None
        self.speaking_until = 0.0  # monotonic time the last pushed audio finishes
        self.last_audio_at = 0.0  # monotonic time audio was last queued (a voice's, or a sound clip)
        self.captions = Captions()  # the words of the reply being spoken, timed against its audio
        # Same latency yardstick for every pipeline: from the mic last hearing the person to Gizmo's
        # first sound. Set by the app: mic (a RobotMic) and on_reply_start(seconds).
        self.mic = None
        self.on_reply_start = None
        self.boost_db = boost_db
        self._gain = 1.0
        # (input peak, input rms, out peak, out rms) per 20 ms chunk of speech, ~5 s worth
        self._levels: deque[tuple[float, float, float, float]] = deque(maxlen=250)

    def _queue_audio(self, pcm: np.ndarray, words: str | None = None) -> None:
        if words:
            self.captions.words(words)
        self.captions.audio(pcm.size / self.rate)
        step = int(self.rate * CHUNK_S)
        for i in range(0, pcm.size, step):
            self._chunks.append(pcm[i : i + step])
        self.last_audio_at = time.monotonic()
        self._wake.set()

    @property
    def clone_wanted(self) -> bool:
        """With Nova Sonic listening, speak with the personality's own voice when a voice server answers."""
        return self.own_voice

    def play_clone(self, pcm: np.ndarray, words: str | None = None) -> None:
        """Audio from a voice server (the personality's own voice), already at the speaker's rate. `words`:
        the sentence it says, for the console to light up; a sound clip has none."""
        self._queue_audio(pcm, words)

    def caption_state(self) -> dict:
        """The words being said and how far into them the voice is, for the console."""
        return self.captions.state(self.speaking_until - time.monotonic())

    def levels(self) -> dict:
        """The raw level and what we send to the speaker, over the last ~5 s of speech."""
        if not self._levels:
            return {"boost_db": self.boost_db}
        peak, rms, out_peak, out_rms = zip(*self._levels)
        mean_rms = lambda xs: float(np.sqrt(np.mean(np.square(xs))))  # noqa: E731
        return {"boost_db": self.boost_db,
                "in_peak_dbfs": dbfs(max(peak)), "in_rms_dbfs": dbfs(mean_rms(rms)),
                "out_peak_dbfs": dbfs(max(out_peak)), "out_rms_dbfs": dbfs(mean_rms(out_rms))}

    def _limit(self, chunk: np.ndarray, nxt: np.ndarray | None) -> np.ndarray:
        """Boost by boost_db with peaks held at LIMIT_CEILING. nxt (the queued chunk) is the lookahead."""
        block = max(1, self.rate // 1000)  # 1 ms: the attack time
        ahead = nxt[:block] if nxt is not None else np.zeros(0, np.float32)
        x = np.concatenate([chunk, ahead])
        nb = -(-x.size // block)
        peaks = np.abs(np.pad(x, (0, nb * block - x.size))).reshape(nb, block).max(axis=1)
        allowed = np.minimum(10 ** (self.boost_db / 20), LIMIT_CEILING / np.maximum(peaks, 1e-9))
        # Gain at each block edge: low enough for the blocks on both sides, and rising slowly.
        release = 10 ** (RELEASE_DB_PER_MS / 20)
        edges = [self._gain]
        for k in range(1, -(-chunk.size // block) + 1):
            edges.append(min(edges[-1] * release, allowed[k - 1], allowed[k] if k < nb else np.inf))
        self._gain = edges[-1]
        gain = np.interp(np.arange(chunk.size), np.arange(len(edges)) * block, edges)
        return np.clip(chunk * gain, -LIMIT_CEILING, LIMIT_CEILING)

    def _shape(self, chunk: np.ndarray, nxt: np.ndarray | None = None) -> np.ndarray:
        """The voice boost, and the levels for the console."""
        if chunk.size == 0:
            return chunk
        out = self._limit(chunk, nxt) if self.boost_db > 0 else chunk
        if self.boost_db <= 0:
            self._gain = 1.0
        rms = float(np.sqrt(np.mean(chunk**2)))
        if rms > 1e-4:  # only measure actual speech
            self._levels.append((float(np.max(np.abs(chunk))), rms,
                                 float(np.max(np.abs(out))), float(np.sqrt(np.mean(out**2)))))
        return out.astype(np.float32)

    @property
    def speaking(self) -> bool:
        return time.monotonic() < self.speaking_until or bool(self._chunks)

    async def start(self, agent) -> None:
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.create_task(self._play())
        if self._probe_task is None:
            self._probe_task = asyncio.create_task(self._probe_clone())
        logger.info(f"Speaker at {self.rate} Hz")

    async def _probe_clone(self) -> None:
        """Keep the console's voice-server status fresh."""
        while True:
            await self.clone.available()
            await asyncio.sleep(10)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    def clear(self) -> None:
        """Drop everything not yet pushed (barge-in or the 'stop talking' button)."""
        self.generation += 1
        self._chunks.clear()
        self.captions.new()

    async def __call__(self, event) -> None:
        if (isinstance(event, BidiTranscriptBlockEvent) and event.role == "user" and self.mute_nova and self.speaking
                and (self.interrupts is None or self.interrupts(event.transcript))):
            self.clear()  # Nova doesn't know the personality's voice is talking, so it won't barge in for us
        if isinstance(event, (BidiTranscriptStartEvent, BidiTranscriptDeltaEvent)):
            # Nova Sonic's own words, a sentence at a time, each just ahead of its audio.
            if event.role == "assistant" and not self.mute_nova:
                if isinstance(event, BidiTranscriptDeltaEvent):
                    self.captions.words(event.delta)
                elif not self.speaking:  # a reply starting in silence (after a sound clip, it carries on from it)
                    self.captions.new()
        elif isinstance(event, BidiAudioDeltaEvent):
            if self.mute_nova:
                return
            pcm = np.frombuffer(base64.b64decode(event["audio"]), dtype=np.int16).astype(np.float32) / 32768
            self._queue_audio(resample(pcm, int(event.get("sample_rate") or self.rate), self.rate))
        elif isinstance(event, BidiBargeInEvent):
            self.clear()

    async def _play(self) -> None:
        clock = time.monotonic()
        while True:
            if not self._chunks:
                self._wake.clear()
                await self._wake.wait()
                continue
            now = time.monotonic()
            if clock < now - 0.1:  # we ran dry: this is the start of a new reply
                heard = getattr(self.mic, "last_voice_at", 0.0)
                if self.on_reply_start and 0 < now - heard < 15 and heard > self.speaking_until:
                    self.on_reply_start(now - heard)
            clock = max(clock, now)
            if clock - now > LEAD_S:
                await asyncio.sleep(clock - now - LEAD_S)
                continue
            try:
                chunk = self._shape(self._chunks.popleft(), self._chunks[0] if self._chunks else None)
            except IndexError:  # emptied from another thread since the check above (the console's Stop talking)
                continue
            self.mini.media.push_audio_sample(chunk)
            self.captions.played_s += chunk.size / self.rate
            clock += chunk.size / self.rate
            self.speaking_until = clock


class ConversationLog:
    """Collects what was said and done for the console page and the journal."""

    def __init__(self, maxlen: int = 80) -> None:
        self.events: deque[dict] = deque(maxlen=maxlen)
        self.turn_started: float | None = None
        self.last_user_at: float | None = None
        self.nova_muted = lambda: False  # Nova's words aren't being heard (another voice took the turn); set by the app
        # (transcript) -> bool: speech that wasn't for Gizmo (wake-word mode) and so isn't kept: not shown on
        # the console, which has no login, nor written to the journal. Set by the app.
        self.private = lambda text: False

    def add(self, kind: str, **fields) -> None:
        self.events.appendleft({"time": time.strftime("%H:%M:%S"), "kind": kind, **fields})

    async def start(self, agent) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def __call__(self, event) -> None:
        if isinstance(event, BidiTranscriptBlockEvent):
            if event.role == "user":
                self.last_user_at = time.monotonic()
                if self.private(event.transcript):  # someone else's conversation: note that it happened, no more
                    words = len(event.transcript.split())
                    logger.info(f"USER: ({words} words that weren't for Gizmo)")
                    self.add("user", text="", private=True, words=words)
                    return
            logger.info(f"{event.role.upper()}: {event.transcript}")
            # Muted, Nova's words were never heard (the cloned or built-in voice said the reply, or Nova answered a
            # turn that wasn't for Gizmo): only the journal keeps them.
            if not (event.role == "assistant" and self.nova_muted()):
                self.add(event.role, text=event.transcript)
        elif isinstance(event, BidiToolUseBlocksEvent):
            for use in event.tool_uses:
                if use["name"] == "think":  # Nova handing the turn to the brain: plumbing, not news
                    heard = str((use.get("input") or {}).get("heard", ""))
                    logger.info("TOOL think(speech that wasn't for Gizmo)" if self.private(heard) else f"TOOL think({use.get('input')})")
                    continue
                self.add("tool", text=f"{use['name']}({use.get('input') or ''})")
                logger.info(f"TOOL {use['name']}({use.get('input')})")
        elif isinstance(event, BidiAudioDeltaEvent):
            # Measured from Nova's transcript of your turn (or the typed line), so Nova's ~1.5 s
            # end-of-turn pause comes on top of this for spoken turns.
            if self.last_user_at is not None:
                self.add("timing", text=f"voice started {time.monotonic() - self.last_user_at:.1f}s after your turn")
                self.last_user_at = None
        elif isinstance(event, BidiBargeInEvent):
            self.add("barge_in", text="interrupted")
        elif isinstance(event, BidiResponseStopEvent):
            pass

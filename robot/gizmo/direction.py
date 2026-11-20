"""Where a voice came from, and how far to turn toward it, so Gizmo looks at whoever is talking to it.

The daemon's Direction of Arrival (DoA) comes from the mic array in the head, as an angle in radians in the head's
frame: 0 = left, pi/2 = straight ahead, pi = right. A linear array can't tell front from back, so pi/2 also means
"directly behind"; we assume people are in front of Gizmo. It's rough (10 to 15 degrees either way), which is
enough to face someone; "Follow faces" can take over from there.

By the time a turn is known to be for Gizmo, the words are over: with Nova Sonic listening, the transcript arrives
a second or two after the person stops. So a tracker keeps the last several seconds of readings, and the bearing
is the median of the frames where the array heard speech. Frames taken while Gizmo itself is speaking don't count:
that's its own voice.

If Gizmo turns the wrong way on your robot, flip the sign on the console (Settings, The robot).
"""

from __future__ import annotations

import logging
import math
import statistics
import threading
import time
from collections import deque

import requests

logger = logging.getLogger("gizmo.direction")

RAIL_DEG = 3.0  # 0 and pi are the array's end stops, not real bearings
MIN_TURN_DEG = 8.0  # don't twitch for small offsets
HEAD_LEAD_DEG = 40.0  # the head leads the body by up to this much before the body turns
BODY_MAX_DEG = 120.0  # a voice alone never swings the body past this
HEAD_BODY_MAX_DEG = 60.0  # the SDK's limit is 65 degrees between head and body yaw
WINDOW_S = 6.0  # how far back a turn's speech can be: the question itself, plus the pause that ended it
MIN_FLAGGED = 2  # readings the mic chip itself marked as speech: one stray frame isn't a voice
MIN_LOUD = 3  # or, failing that, readings taken while the mic heard something voice-loud


class DoATracker:
    """Polls the daemon's DoA in a background thread and keeps the last few seconds.

    The daemon answers about ten times a second, and the chip's own speech flag is sparse (a few readings in a
    short phrase), so a reading also counts, second-best, when loud() says the mic is hearing something as loud
    as a voice.

    quiet() -> bool: True while Gizmo itself is making sound, when a reading would be of its own voice.
    loud() -> bool: True while the mic hears something voice-loud."""

    def __init__(self, daemon_url: str, quiet=None, loud=None, hz: float = 10.0, keep_s: float = 8.0) -> None:
        self._url = f"{daemon_url}/api/state/doa"
        self._quiet = quiet or (lambda: False)
        self._loud = loud or (lambda: False)
        self._period = 1.0 / hz
        self._frames: deque[tuple[float, float, bool, bool]] = deque(maxlen=int(hz * keep_s))
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._poll, name="doa", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)

    def _poll(self) -> None:
        session = requests.Session()
        errors = 0
        while not self._stop.is_set():
            t = time.monotonic()
            try:
                data = session.get(self._url, timeout=1.0).json()
                if data is not None and not self._quiet():
                    self.add(t, float(data["angle"]), bool(data["speech_detected"]), bool(self._loud()))
                errors = 0
            except (requests.RequestException, ValueError, KeyError, TypeError) as e:
                errors += 1
                if errors in (5, 200):  # the odd slow answer while the daemon plays a move is normal
                    logger.warning(f"DoA poll failing ({errors} in a row): {e}")
            self._stop.wait(max(0.0, self._period - (time.monotonic() - t)))

    def add(self, t: float, angle: float, speech: bool, loud: bool = False) -> None:
        with self._lock:
            self._frames.append((t, angle, speech, loud))

    def heard(self, t_end: float, window_s: float = WINDOW_S) -> tuple[list[float], list[float]]:
        """The angles (radians) in the window before t_end: those the chip flagged as speech, and those taken
        while the mic was voice-loud."""
        rail = math.radians(RAIL_DEG)
        with self._lock:
            frames = [f for f in self._frames if t_end - window_s <= f[0] <= t_end and rail < f[1] < math.pi - rail]
        return [a for _, a, speech, _ in frames if speech], [a for _, a, speech, loud in frames if speech or loud]

    def bearing(self, t_end: float, window_s: float = WINDOW_S) -> float | None:
        """Where the voice in the window before t_end came from: the median DoA angle (radians) of its speech
        readings, or None when there weren't enough to call it a voice."""
        flagged, loud = self.heard(t_end, window_s)
        if len(flagged) >= MIN_FLAGGED:
            return statistics.median(flagged)
        return statistics.median(loud) if len(loud) >= MIN_LOUD else None


def offset_deg(angle_rad: float, sign: float = 1.0) -> float:
    """Yaw offset from where the head points now; positive turns left (the SDK's convention)."""
    return sign * math.degrees(math.pi / 2 - angle_rad)


def aim(target_deg: float) -> tuple[float, float]:
    """Head and body yaw (degrees, world frame) that point the head at `target`: the head alone for a small
    turn, the body following for a larger one, within the robot's limits."""
    body = 0.0
    if abs(target_deg) > HEAD_LEAD_DEG:
        body = math.copysign(min(abs(target_deg) - HEAD_LEAD_DEG, BODY_MAX_DEG), target_deg)
    head = max(body - HEAD_BODY_MAX_DEG, min(body + HEAD_BODY_MAX_DEG, target_deg))
    return head, body


def plan_turn(head_yaw_deg: float, offset: float) -> tuple[float, float] | None:
    """Head and body yaw (degrees, world frame) that face a voice at `offset` from where the head points now.
    None when the voice is already roughly ahead."""
    if abs(offset) < MIN_TURN_DEG:
        return None
    return aim(head_yaw_deg + offset)

"""Gizmo's body as Strands tools: look, emotions, dances, stop, face tracking.

Emotions and dances are Pollen's recorded-move libraries, played by the daemon. The
tool descriptions list exactly what's installed on this robot.
"""

from __future__ import annotations

import difflib
import logging
import math
import random
import re
import threading
import time

import requests
from strands import tool

from gizmo.direction import aim, plan_turn

logger = logging.getLogger("gizmo.body")


def create_head_pose(**kwargs):
    """The Reachy Mini SDK's head pose. Imported when a move needs it, so the brain (which only needs this
    module's names) also loads on a laptop without the SDK: python -m gizmo.desk."""
    from reachy_mini.utils import create_head_pose as make

    return make(**kwargs)

EMOTIONS = "pollen-robotics/reachy-mini-emotions-library"
DANCES = "pollen-robotics/reachy-mini-dances-library"
# Moves that would put Gizmo to sleep or are too long for a conversation.
SKIP_EMOTIONS = {"mini-deep-sleep", "sleep1", "dying1", "wake-mini-up", "toc-toc-toc"}
# Quiet time after a recorded move before the next one may start. 2026-10-26 19:21: three emotions in 3 s
# (two for "Oh" and "Yeah.") were followed by a motor communication error and a power-line shutdown,
# like the brownouts the day before, which also came during speech plus moves.
MOVE_REST_S = 4.0

LOOK = {  # yaw, pitch in degrees (+yaw = Gizmo's left, -pitch = up)
    "center": (0, 0), "front": (0, 0), "left": (35, 0), "right": (-35, 0),
    "up": (0, -20), "down": (0, 15), "up left": (30, -15), "up right": (-30, -15),
    "down left": (30, 12), "down right": (-30, 12),
}

ROBOT_PROMPT = """You have a small expressive body. Use it the way a lively presenter would, without narrating it:
- express(emotion) for genuine moments: greeting someone (welcoming), a joke (laughing), good news (success, proud), \
surprise, thinking it over, agreeing (yes) or disagreeing (no). Roughly one gesture every few turns, never every sentence.
- look(direction) when you mention something to the side, or to look back at the person.
- dance(name) only when someone asks you to dance.
- stop_moving() if someone asks you to stop or hold still.
Your head moves on its own while you talk, so you don't need gestures for that."""


class Body:
    def __init__(self, mini, daemon_url: str) -> None:
        self.mini = mini
        self.url = daemon_url
        self.http = requests.Session()
        self.last_move: str | None = None
        self.tracking = False
        self.emotions = self._list(EMOTIONS, skip=SKIP_EMOTIONS)
        self.dances = self._list(DANCES)
        self._moving = False  # a recorded move (emotion or dance) is playing
        self._rest_until = 0.0  # and none starts before this (monotonic)
        self._gesture_lock = threading.Lock()  # replies' gesture threads can overlap: one move at a time
        self.want_asleep = False  # hidden under a box; set before calling hide()/reveal()
        self._pose_lock = threading.Lock()
        # Where Gizmo has turned to face whoever it's talking with (yaw in degrees, 0 = straight ahead). The
        # brain's looks are relative to it, so "look left" is left of the person, not of the room.
        self.facing = 0.0

    def _list(self, dataset: str, skip: set[str] = frozenset()) -> list[str]:
        try:
            names = self.http.get(f"{self.url}/api/move/recorded-move-datasets/list/{dataset}", timeout=30).json()
            return sorted(n for n in names if n not in skip)
        except (requests.RequestException, ValueError) as e:
            logger.warning(f"Couldn't list {dataset}: {e}")
            return []

    def busy(self) -> bool:
        """A recorded move is playing or finished less than MOVE_REST_S ago."""
        return self._moving or time.monotonic() < self._rest_until

    def play(self, dataset: str, name: str, interrupt: bool = False) -> str | None:
        """Play a recorded move, or skip it (None) while Gizmo is still busy with the last one.

        interrupt: a move someone asked for (a dance) stops the current one instead of being skipped.
        """
        with self._gesture_lock:
            if self.busy():
                if not interrupt:
                    logger.info(f"Skipped {name}: still busy with the last gesture")
                    return None
                if self._moving and self.last_move:
                    self.http.post(f"{self.url}/api/move/stop", json={"uuid": self.last_move}, timeout=5)
                    self._wait_for_moves(2)  # the daemon ignores a new move while one still runs
            r = self.http.post(f"{self.url}/api/move/play/recorded-move-dataset/{dataset}/{name}", timeout=10)
            r.raise_for_status()
            self.last_move = r.json().get("uuid")
            self._moving = True
        threading.Thread(target=self._quiet_wobble_during, args=(self.last_move,), daemon=True).start()
        return name

    def _quiet_wobble_during(self, move_id: str | None) -> None:
        """Speech wobble on top of a recorded move pushes poses past the head's limits (daemon IK
        warnings, jerky motion), so pause it until the move finishes. Then start the rest period."""
        if not move_id:
            self._moving, self._rest_until = False, time.monotonic() + MOVE_REST_S
            return
        try:
            self.mini.disable_wobbling()
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                running = self.http.get(f"{self.url}/api/move/running", timeout=3).json()
                if not any(m.get("uuid") == move_id for m in running):
                    break
                time.sleep(0.3)
        except Exception as e:
            logger.debug(f"wobble pause: {e!r}")
        finally:
            # A newer move has its own waiter that will turn wobble back on when it ends. Under the
            # lock, so a dance replacing this move can't be marked finished by this waiter.
            with self._gesture_lock:
                ours = self.last_move in (move_id, None)
                if ours:
                    self._moving, self._rest_until = False, time.monotonic() + MOVE_REST_S
            if ours:
                try:
                    self.mini.enable_wobbling()
                    if self.facing and not self.want_asleep:  # a recorded move ends facing the room: turn back
                        self.look(0.0, 0.0)
                except Exception:
                    pass

    def stop(self) -> None:
        self.facing = 0.0
        if self.last_move:
            try:
                self.http.post(f"{self.url}/api/move/stop", json={"uuid": self.last_move}, timeout=5)
            except requests.RequestException:
                pass
            self.last_move = None
        self.mini.goto_target(head=create_head_pose(), antennas=[-0.17, 0.17], body_yaw=0.0, duration=0.8)

    def look(self, yaw: float, pitch: float) -> bool:
        """False (skipped) while a recorded move owns the head: the daemon ignores the target, and the
        SDK call then times out ("Task did not complete in time", 2026-10-26)."""
        if self._moving:
            return False
        head, body = aim(self.facing + yaw)  # relative to whoever Gizmo is facing
        self.mini.goto_target(head=create_head_pose(yaw=head, pitch=pitch, degrees=True),
                              body_yaw=math.radians(body), duration=0.7)
        return True

    def face(self, offset: float) -> float | None:
        """Turn toward a voice `offset` degrees from where the head points now (positive: to Gizmo's left).
        Returns the yaw it now faces, or None when it didn't turn: the voice was already ahead, a recorded
        move owns the head, or Gizmo is hidden."""
        if self._moving or self.want_asleep:
            return None
        rot = self.mini.get_current_head_pose()[:3, :3]
        plan = plan_turn(math.degrees(math.atan2(rot[1, 0], rot[0, 0])), offset)
        if plan is None:
            return None
        head, body = plan
        following = self.tracking
        if following:  # the face tracker would pull the head straight back
            self.track_faces(False)
        self.mini.goto_target(head=create_head_pose(yaw=head, degrees=True), body_yaw=math.radians(body), duration=0.6)
        self.facing = head
        if following:
            self.track_faces(True)
        return head

    def track_faces(self, on: bool) -> bool:
        try:
            r = self.http.post(f"{self.url}/api/media/tracking/{'enable' if on else 'disable'}", timeout=5)
            self.tracking = bool(r.json().get("enabled", False)) if on else False
        except (requests.RequestException, ValueError) as e:
            logger.warning(f"Face tracking toggle failed: {e}")
        return self.tracking

    def set_volume(self, level: int) -> int:
        level = max(0, min(100, int(level)))
        self.http.post(f"{self.url}/api/volume/set", json={"volume": level}, timeout=5).raise_for_status()
        return level

    def hide(self) -> None:
        """The power-down pose, resting there with the motors off: for hiding Gizmo under a box.

        Pose changes take turns (a reveal waits for a hide still under way), and a hide that's no
        longer wanted when its turn comes is skipped: a hide re-run at app start once finished after
        a reveal and left the motors off (2026-10-26).
        """
        with self._pose_lock:
            if self.want_asleep:
                self._hide()

    def _hide(self) -> None:
        self.track_faces(False)
        try:
            self.mini.disable_wobbling()
        except Exception:
            pass
        self.http.post(f"{self.url}/api/move/play/goto_sleep", timeout=10).raise_for_status()
        self._wait_for_moves(10)
        self.http.post(f"{self.url}/api/motors/set_mode/disabled", timeout=10).raise_for_status()

    def reveal(self) -> None:
        """Motors on (holding the sleep pose), then the wake-up move, as `./gizmo wake` does."""
        with self._pose_lock:
            if not self.want_asleep:
                self._reveal()

    def _reveal(self) -> None:
        self.facing = 0.0  # the wake-up move ends facing ahead
        self.http.post(f"{self.url}/api/motors/set_mode/enabled", timeout=10).raise_for_status()
        self.http.post(f"{self.url}/api/move/play/wake_up", timeout=10).raise_for_status()
        self._wait_for_moves(10)
        try:
            self.mini.enable_wobbling()
        except Exception:
            pass

    def _wait_for_moves(self, timeout_s: float) -> None:
        deadline = time.monotonic() + timeout_s
        time.sleep(0.3)  # let the daemon register the move
        while time.monotonic() < deadline:
            if not self.http.get(f"{self.url}/api/move/running", timeout=3).json():
                return
            time.sleep(0.2)

    def ack(self) -> None:
        """A quick antenna flick the moment someone finishes talking: "heard you", while the brain works."""
        if self.busy():
            return

        def run() -> None:
            try:
                self.mini.goto_target(antennas=[-0.6, 0.6], duration=0.15)
                self.mini.goto_target(antennas=[-0.17, 0.17], duration=0.3)
            except Exception as e:
                logger.debug(f"ack: {e!r}")

        threading.Thread(target=run, name="ack", daemon=True).start()

    def thinking(self, _question: str = "") -> None:
        """Called when the expert is asked: a 'thinking' gesture covers the 2-3 s wait."""
        if self.busy() or "thoughtful1" not in self.emotions:
            return
        threading.Thread(target=self._safe_play, args=(EMOTIONS, random.choice(["thoughtful1", "thoughtful2"])),
                         daemon=True).start()

    def _safe_play(self, dataset: str, name: str) -> None:
        try:
            self.play(dataset, name)
        except requests.RequestException as e:
            logger.warning(f"Couldn't play {name}: {e}")


# Everyday words the model reaches for, mapped to the library's names.
SYNONYMS = {
    "happy": "cheerful", "joy": "cheerful", "excited": "enthusiastic", "hello": "welcoming", "hi": "welcoming",
    "greeting": "welcoming", "welcome": "welcoming", "agree": "yes", "nod": "yes", "disagree": "no",
    "think": "thoughtful", "thinking": "thoughtful", "laugh": "laughing", "funny": "laughing", "love": "loving",
    "angry": "furious", "annoyed": "irritated", "bored": "boredom", "thanks": "grateful", "thank_you": "grateful",
    "surprise": "surprised", "wow": "amazed", "unsure": "uncertain", "sorry": "oops", "listening": "attentive",
    # Words with no move of their own, which the fuzzy match got wrong ("amused" played exhausted1, 2026-10-26).
    "amused": "laughing", "amusement": "laughing", "playful": "cheerful", "glad": "cheerful", "pleased": "cheerful",
    "interested": "curious", "intrigued": "curious", "concerned": "uncertain", "worried": "anxiety",
    "skeptical": "inquiring", "determined": "proud", "serious": "attentive", "stern": "reprimand",
}


def match(name: str, options: list[str]) -> str | None:
    """'laughing' -> 'laughing1'; 'happy' -> a 'cheerful' move. None if nothing is close."""
    if not options:
        return None
    key = re.sub(r"[^a-z_-]", "", name.lower().strip().replace(" ", "_"))
    key = SYNONYMS.get(key, key)
    family = sorted(o for o in options if re.sub(r"\d+$", "", o) == key)
    if family:
        return random.choice(family)
    close = difflib.get_close_matches(key, options, n=1, cutoff=0.5)
    return close[0] if close else None


def make_body_tools(body: Body) -> list:
    families = sorted({re.sub(r"\d+$", "", e) for e in body.emotions})

    @tool(description=(
        "Play an expressive emotion with your head and antennas. "
        f"emotion is one of: {', '.join(families)}."
    ))
    def express(emotion: str) -> str:
        name = match(emotion, body.emotions)
        if not name:
            return f"No emotion like '{emotion}'. Options: {', '.join(families)}"
        if body.play(EMOTIONS, name) is None:
            return "Skipped: still finishing the last gesture"
        return f"Playing {name}"

    @tool(description=(
        "Dance. name is one of: " + ", ".join(body.dances) + ". Leave it empty to pick one at random. "
        "Only dance when someone asks."
    ))
    def dance(name: str = "") -> str:
        chosen = match(name, body.dances) if name else None
        chosen = chosen or (random.choice(body.dances) if body.dances else None)
        if not chosen:
            return "No dances are installed"
        body.play(DANCES, chosen, interrupt=True)  # asked for: replaces a gesture still playing
        return f"Dancing {chosen}"

    @tool
    def look(direction: str) -> str:
        """Turn your head to look somewhere.

        Args:
            direction: center, left, right, up, down, up left, up right, down left or down right.
        """
        yaw, pitch = LOOK.get(direction.strip().lower(), LOOK["center"])
        if not body.look(yaw, pitch):
            return "Skipped: a gesture is still playing"
        return f"Looking {direction}"

    @tool
    def stop_moving() -> str:
        """Stop any emotion or dance and return to a neutral pose."""
        body.stop()
        return "Stopped and back to neutral"

    @tool
    def track_faces(enabled: bool) -> str:
        """Turn face tracking on or off (when on, you keep looking at the person in front of you).

        Args:
            enabled: true to follow faces, false to stop.
        """
        on = body.track_faces(enabled)
        return "Following faces" if on else ("Face tracking unavailable" if enabled else "Stopped following faces")

    return [express, dance, look, stop_moving, track_faces]

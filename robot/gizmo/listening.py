"""Free talk or wake word: which heard turns reach the brain.

- Free talk: everything heard is a turn, as before.
- Wake word: a turn needs one of the names ("Gizmo") somewhere in it, unless it follows Gizmo's last
  words within follow_up_s (a conversation in progress: "yeah", "what about Mars?"). People talking
  among themselves don't reach the model, and don't stop Gizmo mid-sentence either.
Typed console lines and stage directions always go through. On the cloud pipeline Nova passes them to
the brain like speech, so they're remembered for TYPED_S and let through when they come back. The match
is on the transcript (Whisper on the box, or Nova's), fuzzily, so "Gismo" or "Gizmo's" count.

Short reactions ("oh", "yeah") are dropped as not-a-turn (local_session.REACTION) unless Gizmo has
just asked a question: then "yeah" is the answer.
"""

from __future__ import annotations

import difflib
import re
import time

MODES = ("free", "wake")
DEFAULT_NAMES = ["Gizmo"]
FOLLOW_UP_S = 20.0
TYPED_S = 30.0  # how long a typed line waits for Nova to hand it to the brain


def _norm(text: str) -> str:
    return re.sub(r"[^a-z ]+", " ", text.lower())


class Listening:
    def __init__(self, mode: str = "free", names: list[str] | None = None, follow_up_s: float = FOLLOW_UP_S) -> None:
        self.mode = mode if mode in MODES else "free"
        self.names = [n for n in (names or DEFAULT_NAMES) if n.strip()] or list(DEFAULT_NAMES)
        self.follow_up_s = follow_up_s
        self.speaking_until = lambda: 0.0  # monotonic time Gizmo's last words finish; set by the app
        self.asked = False  # Gizmo's last reply ended with a question
        self._typed: list[tuple[float, str]] = []  # (monotonic time, normalised text)

    def replied(self, say: str) -> None:
        self.asked = say.rstrip().rstrip("\"')").endswith("?")

    def follow_up_open(self) -> float:
        """Seconds left in which a turn needs no name (0 when closed). It opens when Gizmo stops talking:
        while it talks, only its name interrupts it."""
        now, until = time.monotonic(), self.speaking_until()
        return 0.0 if until > now else max(0.0, until + self.follow_up_s - now)

    def names_in(self, text: str) -> bool:
        """Is one of the names in the transcript? One- and two-word spans are compared to each name,
        spaces ignored, so "Giz mo" and "Gismo" match "Gizmo"."""
        words = _norm(text).split()
        spans = words + ["".join(words[i: i + 2]) for i in range(len(words) - 1)]
        for name in self.names:
            target = _norm(name).replace(" ", "")
            for span in spans:
                span = span[:-1] if span.endswith("s") and not target.endswith("s") else span  # Gizmo's
                if span == target or (len(target) >= 4 and difflib.SequenceMatcher(None, span, target).ratio() >= 0.8):
                    return True
        return False

    def typed(self, text: str) -> None:
        """A typed console line or a stage direction: it goes through whatever the mode."""
        now = time.monotonic()
        self._typed = [(t, x) for t, x in self._typed if now - t < TYPED_S] + [(now, " ".join(_norm(text).split()))]

    def was_typed(self, text: str) -> bool:
        """Not used up on a match: Nova's transcript of the line (checked by the speaker) and its call to the
        brain both see it. It expires after TYPED_S."""
        heard, now = " ".join(_norm(text).split()), time.monotonic()
        # Nova runs a typed line into whatever it was hearing at that moment ("...in houseGizmo, say hello"), so
        # a line of any length also counts when it's inside what came back, spaces aside.
        squeezed = heard.replace(" ", "")
        return any(now - t < TYPED_S and (difflib.SequenceMatcher(None, heard, typed).ratio() >= 0.8
                                          or (len(typed) >= 8 and typed.replace(" ", "") in squeezed))
                   for t, typed in self._typed)

    def addressed(self, text: str) -> bool:
        """Is this heard turn for Gizmo?"""
        return self.mode == "free" or self.was_typed(text) or self.follow_up_open() > 0 or self.names_in(text)

    def state(self) -> dict:
        return {"mode": self.mode, "names": self.names, "follow_up_s": self.follow_up_s,
                "follow_up_left": round(self.follow_up_open(), 1) if self.mode == "wake" else None}
